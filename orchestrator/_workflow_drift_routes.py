# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow drift routes."""
from __future__ import annotations

from orchestrator import _workflow_drift_state as _state
from orchestrator import workflow_drift as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
_USER_CONTENT_HASH = _state._USER_CONTENT_HASH


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
    quoted = _owner._as_blockquote(body)
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
        f"{_owner._COMMIT_STYLE_NOTE}\n\n"
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
        f"{_owner._FOREGROUND_ONLY_NOTE}"
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


def _drift_to_decomposing_notice(orphan_children: list) -> str:
    """Build the reroute notice, including any orphaned child numbers."""
    if not orphan_children:
        return (
            ":pencil2: issue content changed; re-running decomposer "
            "against the updated body."
        )
    orphan_list = ", ".join(
        f"#{child_number}" for child_number in orphan_children
    )
    return (
        ":pencil2: issue content changed; re-running decomposer "
        "against the updated body. The previously-tracked children "
        f"({orphan_list}) will be ORPHANED -- the orchestrator no "
        "longer tracks them; please close any that no longer apply to "
        "the updated requirements."
    )


def _reset_decomposition_for_drift(
    state: PinnedState, new_hash: str,
) -> None:
    """Clear manifest/session state while retaining the locked agent spec."""
    state.set(_USER_CONTENT_HASH, new_hash)
    # A fresh decomposer session must keep the pinned backend so an agent-spec
    # configuration change cannot retarget an issue already in flight.
    state.set("decomposer_session_id", None)
    # Empty manifest tracking prevents half-finished decomposition recovery
    # from treating the intentional reroute as a crashed split.
    state.set("children", [])
    state.set("dep_graph", {})
    state.set("expected_children_count", None)
    state.set("umbrella", None)
    state.set("awaiting_human", False)
    state.set("park_reason", None)


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
    notice = _owner._drift_to_decomposing_notice(orphan_children)
    _owner._post_issue_comment(gh, issue, state, notice)
    _owner._reset_decomposition_for_drift(state, new_hash)
    gh.set_workflow_label(issue, WorkflowLabel.DECOMPOSING)
