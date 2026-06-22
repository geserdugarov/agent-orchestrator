# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""User-content drift helpers shared by stage handlers.

The orchestrator keeps a SHA-256 hash of the issue's user-visible content
(title + body + human-authored comments) in pinned state so future ticks
can detect a human edit mid-flight and react -- either by re-routing the
issue back to `decomposing` (pre-implementation stages) or by resuming
the in-flight dev session with the updated context.

This module exposes:

* `_compute_user_content_hash` -- the canonical hash over title/body/comments.
* `_detect_user_content_change` -- diff against the stored baseline, with
  durable persistence on first encounter.
* `_build_user_content_change_prompt` -- the dev-resume prompt for the
  in-flight-edit path.
* `_mark_drift_comments_consumed` -- watermark advance after the dev has
  been fed the full thread.
* `_route_drift_to_decomposing` -- destructive re-route used by the
  pre-implementation drift path.

Stage handlers live under `orchestrator/stages/` (decomposition.py,
implementing.py, documenting.py, validating.py, in_review.py, fixing.py,
conflicts.py, question.py); they reach these helpers through the
compatibility facade in `workflow.py`, which re-exports each name
above for backward compatibility with direct test references and
`patch.object(workflow, ...)` patches.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from github.Issue import Issue

from .state_machine import WorkflowLabel
from .github import PINNED_STATE_MARKER, GitHubClient, PinnedState
from .workflow_messages import (
    _COMMIT_STYLE_NOTE,
    _FOREGROUND_ONLY_NOTE,
    _ORCH_COMMENT_MARKER,
    _orchestrator_ids,
    _post_issue_comment,
)


def _compute_user_content_hash(
    issue: Issue, orchestrator_ids: set[int]
) -> str:
    """SHA-256 over title + body + human-authored comments.

    Used by `_detect_user_content_change` so the orchestrator can react
    when a human edits the issue body or adds acceptance criteria after
    the workflow has already picked it up. Non-human content is filtered
    four ways:

    * pinned-state comment by `PINNED_STATE_MARKER`;
    * orchestrator-posted comments by `_ORCH_COMMENT_MARKER` embedded in
      the body (id-cap-resistant -- the marker stays on the GitHub side
      forever even after the comment's id has been evicted from
      `orchestrator_comment_ids`);
    * legacy orchestrator comments (posted before the marker was
      introduced) by id from `orchestrator_comment_ids`;
    * third-party Bot / App accounts (Dependabot, Renovate, CI bots, ...)
      by GitHub's `user.type == "Bot"` flag. These accounts cannot be
      filtered by the id-list or marker because we never post them, and
      they post structurally (e.g. weekly Dependabot bumps) which would
      otherwise re-trigger drift detection on every tick they post.

    Author-login matching is deliberately avoided because the orchestrator
    PAT is often shared with a human reviewer's GitHub account; a login
    filter would falsely drop the human's real review comments. The
    `user.type` flag is a structural GitHub-account property and does not
    conflict with that constraint.
    """
    parts = [issue.title or "", issue.body or ""]
    for c in issue.get_comments():
        body = c.body or ""
        if PINNED_STATE_MARKER in body:
            continue
        if _ORCH_COMMENT_MARKER in body:
            continue
        cid = getattr(c, "id", None)
        if cid is not None and int(cid) in orchestrator_ids:
            continue
        user = getattr(c, "user", None)
        if user is not None and getattr(user, "type", None) == "Bot":
            continue
        parts.append(body)
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _detect_user_content_change(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> Optional[str]:
    """Return the new hash if the user-visible content drifted since the
    prior stored value, or None when unchanged.

    On the FIRST call for an issue (no prior hash in pinned state), persist
    the current value via `gh.write_pinned_state` immediately. Doing it
    in-memory only would lose the baseline whenever the calling handler's
    early-return path (awaiting-human-with-no-new-comments, debounce,
    child-waiting-on-deps, …) skips its own state write; the very next
    edit would then be classified as the new baseline and silently
    absorbed. The cost is one extra write per legacy issue still missing
    the field on first encounter; in steady state the hash is already set
    and this branch never fires.
    """
    orchestrator_ids = _orchestrator_ids(state)
    current = _compute_user_content_hash(issue, orchestrator_ids)
    prior = state.get("user_content_hash")
    if not isinstance(prior, str):
        state.set("user_content_hash", current)
        gh.write_pinned_state(issue, state)
        return None
    if current == prior:
        return None
    return current


def _build_user_content_change_prompt(
    issue: Issue, comments_text: str,
) -> str:
    """Resume prompt that quotes the updated title, body, AND the current
    conversation so the dev session can re-evaluate against the new
    requirements.

    Used by handlers that detect a user content drift mid-implementation:
    the dev session is locked to whichever backend wrote `dev_session_id`,
    so we cannot re-decompose, but we CAN feed the new context to the
    existing session and let it commit any additional work. Including the
    comments thread matters because the hash also drifts when the human
    adds acceptance criteria as a NEW comment (not just a body edit), and
    quoting only title/body would leave the dev unaware of the new comment
    it's supposed to react to.
    """
    title = (issue.title or "").strip() or f"#{issue.number}"
    body = (issue.body or "").strip() or "(no body)"
    quoted = "> " + body.replace("\n", "\n> ")
    convo = comments_text or "(no prior comments)"
    return (
        "The human edited the issue while you were working on it. Re-read the "
        "updated title, body, and conversation below, decide whether your "
        "existing work still satisfies the new requirements, and COMMIT any "
        "additional changes needed in your current worktree. Do NOT push -- "
        "the orchestrator pushes and re-runs the reviewer.\n\n"
        f"Updated issue title: {title!r}\n\n"
        f"Updated issue body:\n\n{quoted}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        f"{_COMMIT_STYLE_NOTE}\n\n"
        "If your existing commits already satisfy the new requirements and "
        "no further code change is needed, end your final message with "
        "EXACTLY this marker, alone on its own line:\n\n"
        "  ACK: <one-line justification>\n\n"
        "Use `ACK:` ONLY when you are certain the existing work covers the "
        "edit -- the orchestrator treats it as an explicit acknowledgement "
        "and stays on the current label without parking. If you have a "
        "clarification question or are unsure, do NOT use `ACK:`; reply "
        "with the question and the orchestrator will park awaiting a human "
        "reply (same as a regular agent question).\n\n"
        f"{_FOREGROUND_ONLY_NOTE}"
    )


def _mark_drift_comments_consumed(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Advance `last_action_comment_id` past every comment visible on the
    issue thread right now.

    Used by the user-content-drift paths after they resume the dev session
    with `_recent_comments_text(issue)` quoted in the prompt: the dev has
    been fed the full conversation, so the next validating->in_review
    handoff (via `_seed_watermark_past_self`) must NOT classify those same
    comments as fresh, unconsumed feedback and replay them as a duplicate
    dev resume on the next in_review tick. Mirrors the pre-resume bump in
    `_resume_developer_on_human_reply`; the post here uses
    `latest_comment_id` rather than the `comments_after` walk because the
    drift prompt feeds the full thread (`_recent_comments_text`), not just
    a single new-comments slice. One-way ratchet so a higher prior value
    (e.g. a recent park comment id) is never lowered.
    """
    latest = gh.latest_comment_id(issue)
    if not isinstance(latest, int):
        return
    prior = state.get("last_action_comment_id")
    if not isinstance(prior, int) or latest > prior:
        state.set("last_action_comment_id", latest)


def _route_drift_to_decomposing(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    new_hash: str,
    orphan_children: list,
) -> None:
    """Route an issue back to `decomposing` after a pre-implementation
    user-content drift, clearing the locked decomposer session and any
    in-flight manifest state so the next tick spawns a fresh decomposer
    against the updated body.

    `orphan_children` is the parent's previously-tracked children list
    (empty for `ready` / blocked-child cases): existing children are NOT
    closed on GitHub by this helper, but their record is dropped from the
    parent's pinned state so the new manifest does not collide with them.
    The notice posted on the issue lists the orphan numbers explicitly so
    the operator can close any that no longer apply.

    Caller writes pinned state (`gh.write_pinned_state`) after returning.
    """
    if orphan_children:
        orphan_list = ", ".join(f"#{n}" for n in orphan_children)
        notice = (
            ":pencil2: issue content changed; re-running decomposer "
            "against the updated body. The previously-tracked children "
            f"({orphan_list}) will be ORPHANED -- the orchestrator no "
            "longer tracks them; please close any that no longer apply to "
            "the updated requirements."
        )
    else:
        notice = (
            ":pencil2: issue content changed; re-running decomposer "
            "against the updated body."
        )
    _post_issue_comment(gh, issue, state, notice)
    state.set("user_content_hash", new_hash)
    # Clear `decomposer_session_id` so the next tick spawns a FRESH
    # decomposer session (deriving a new manifest against the updated
    # body, not resuming the prior session with only the human's reply).
    # Deliberately PRESERVE `decomposer_agent`: once the role's spec has
    # been recorded on this issue it is locked for the rest of its
    # lifecycle, even across drift events. A config flip (e.g.
    # `DECOMPOSE_AGENT=codex -> claude`) between ticks must not retarget
    # an in-flight issue at a different backend; `_read_decomposer_session`
    # uses the recorded spec, so the fresh spawn picks up where the old
    # one left off and the FullSpecPersistenceTest contract holds.
    state.set("decomposer_session_id", None)
    # Wipe the manifest tracking so `_handle_decomposing`'s half-finished
    # recovery branch does not fire on the next tick (it keys on
    # `expected_children_count` or a non-empty `children` list).
    state.set("children", [])
    state.set("dep_graph", {})
    state.set("expected_children_count", None)
    state.set("umbrella", None)
    state.set("awaiting_human", False)
    state.set("park_reason", None)
    gh.set_workflow_label(issue, WorkflowLabel.DECOMPOSING)
