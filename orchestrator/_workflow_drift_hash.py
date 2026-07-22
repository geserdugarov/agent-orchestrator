# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow drift hash."""
from __future__ import annotations

from orchestrator import _workflow_drift_state as _state
from orchestrator import workflow_drift as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
IssueComment = _owner.IssueComment
Optional = _owner.Optional
PINNED_STATE_MARKER = _owner.PINNED_STATE_MARKER
PinnedState = _owner.PinnedState
hashlib = _owner.hashlib
is_trusted_author = _owner.is_trusted_author
_USER_CONTENT_HASH = _state._USER_CONTENT_HASH


def _is_hidden_comment(
    issue_comment: IssueComment, orchestrator_ids: set[int],
) -> bool:
    """True for comments that never count as user-authored requirements:
    orchestrator markers, orchestrator-authored IDs, bots, and untrusted
    authors."""
    body = issue_comment.body or ""
    if PINNED_STATE_MARKER in body or _owner._ORCH_COMMENT_MARKER in body:
        return True
    comment_id = getattr(issue_comment, "id", None)
    if comment_id is not None and int(comment_id) in orchestrator_ids:
        return True
    user = getattr(issue_comment, "user", None)
    if user is not None and getattr(user, "type", None) == "Bot":
        return True
    return not is_trusted_author(user)


def _comment_body_for_hash(
    issue_comment: IssueComment,
    orchestrator_ids: set[int],
    *,
    include_bare_continue: bool,
) -> Optional[str]:
    """Return user-authored requirements text, or None for filtered content."""
    if _owner._is_hidden_comment(issue_comment, orchestrator_ids):
        return None
    body = issue_comment.body or ""
    if include_bare_continue:
        return body
    if _owner._is_bare_orchestrator_continue(issue_comment):
        return None
    return body


def _compute_user_content_hash(
    issue: Issue, orchestrator_ids: set[int],
    *, include_bare_continue: bool = False,
) -> str:
    """SHA-256 over title + body + human-authored comments.

    Used by `_detect_user_content_change` so the orchestrator can react
    when a human edits the issue body or adds acceptance criteria after
    the workflow has already picked it up.

    `include_bare_continue` is the legacy-compat escape hatch: with it True the
    bare `/orchestrator continue` filter (below) is skipped, reproducing the
    pre-issue-#729 algorithm. `_detect_user_content_change` uses it to recognize
    a baseline written by the old algorithm and absorb the one-time delta instead
    of firing false drift. Default False (the current algorithm).

    Non-human content is filtered six ways:

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
    * untrusted authors by `is_trusted_author` when `ALLOWED_ISSUE_AUTHORS`
      is set. This keeps an outsider's comment from shifting the hash and
      re-triggering drift (and the re-decompose / dev-resume it drives) on
      a public repo. With no allowlist configured everyone is trusted, so
      the default deployment's hash is unchanged.
    * a bare `/orchestrator continue` operator command by
      `_is_bare_orchestrator_continue`. The command is an operator control,
      not requirements content: counting it would shift the hash and route
      the nudge through generic "issue body/content changed" drift handling
      instead of the stage's intentional session-limit retry (issue #729).
      A comment carrying the command ALONGSIDE genuine guidance is NOT bare,
      so it still shifts the hash and drives the normal drift/resume path.

    The orchestrator's OWN comments are dropped by marker/id (above),
    never by login, so a PAT shared with a human reviewer's account does
    not swallow that reviewer's real comments as bot noise. The allowlist
    filter is a separate, opt-in login gate: an operator who enables it is
    expected to list the reviewer login they post under.
    """
    parts = [issue.title or "", issue.body or ""]
    for issue_comment in issue.get_comments():
        comment_body = _owner._comment_body_for_hash(
            issue_comment,
            orchestrator_ids,
            include_bare_continue=include_bare_continue,
        )
        if comment_body is not None:
            parts.append(comment_body)
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

    Legacy-hash normalization: an issue whose baseline was written by the
    pre-issue-#729 algorithm (which counted a bare `/orchestrator continue`
    comment) compares unequal to the new `current` even when the requirements
    did not change. Before reporting drift, recompute with the OLD algorithm
    (`include_bare_continue=True`); if that reproduces the stored baseline the
    delta is purely the algorithm change, so persist the new baseline and report
    no drift. This keeps a bare continue outstanding at deploy time from firing
    one false "issue body/content changed" route.
    """
    orchestrator_ids = _owner._orchestrator_ids(state)
    current = _owner._compute_user_content_hash(issue, orchestrator_ids)
    prior = state.get(_USER_CONTENT_HASH)
    if not isinstance(prior, str):
        state.set(_USER_CONTENT_HASH, current)
        gh.write_pinned_state(issue, state)
        return None
    if current == prior:
        return None
    legacy = _owner._compute_user_content_hash(
        issue, orchestrator_ids, include_bare_continue=True,
    )
    if legacy == prior:
        state.set(_USER_CONTENT_HASH, current)
        gh.write_pinned_state(issue, state)
        return None
    return current
