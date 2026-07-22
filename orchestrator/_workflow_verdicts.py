# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow verdicts."""
from __future__ import annotations

from orchestrator import _workflow_messages_state as _state
from orchestrator import workflow_messages as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
Tuple = _owner.Tuple
_CONTINUE_NEEDS_GUIDANCE_MSG = _state._CONTINUE_NEEDS_GUIDANCE_MSG
_CONTINUE_PARK_REASONS = _state._CONTINUE_PARK_REASONS
_DOC_VERDICT_RE = _state._DOC_VERDICT_RE
_DRIFT_ACK_RE = _state._DRIFT_ACK_RE
_ORCHESTRATOR_CONTINUE_RE = _state._ORCHESTRATOR_CONTINUE_RE
_VERDICT_RE = _state._VERDICT_RE
_VERDICT_UNKNOWN = _state._VERDICT_UNKNOWN


def _parse_review_verdict(last_message: str) -> Tuple[str, str]:
    """Find the last 'VERDICT: APPROVED|CHANGES_REQUESTED' marker.

    Returns (verdict, body_above_marker). verdict is one of "approved",
    "changes_requested", or "unknown" (no marker found). body_above_marker is
    the slice of last_message before the marker, used as PR-comment text for
    the changes-requested case.
    """
    if not last_message:
        return _VERDICT_UNKNOWN, ""
    matches = list(_VERDICT_RE.finditer(last_message))
    if not matches:
        return _VERDICT_UNKNOWN, last_message
    last = matches[-1]
    word = last.group(1).upper()
    verdict = "approved" if word == "APPROVED" else "changes_requested"
    body = last_message[: last.start()].rstrip()
    return verdict, body


def _parse_documentation_verdict(last_message: str) -> Tuple[str, str]:
    """Find a final 'DOCS: NO_CHANGE' marker in a documentation-stage message.

    Returns (verdict, body_above_marker):
      * `("no_change", body)` -- the agent emitted the explicit marker
        AS THE FINAL LINE (alone on its line, with only optional
        whitespace through end of string), confirming the branch diff
        requires no documentation update. `body` is the slice above
        the marker, suitable for surfacing the agent's one-line
        justification on the issue.
      * `("unknown", last_message)` -- no valid final marker present.
        The caller MUST park rather than treat this as success;
        deliberately rejected variants include:
          - ambiguous prose like "no changes needed";
          - inline references such as
              "I cannot conclude DOCS: NO_CHANGE because ...";
          - non-final markers followed by further content, e.g.
              "DOCS: NO_CHANGE\nBut I have a question.";
          - markers with trailing punctuation like "DOCS: NO_CHANGE.".

    The `"updated"` outcome (docs were modified) is signalled by a fresh
    commit on the branch (any subject; the prompt no longer mandates a
    `docs:` prefix) and is detected at the stage handler level rather than
    here -- this parser only resolves the no-commit branch.
    """
    if not last_message:
        return _VERDICT_UNKNOWN, ""
    match = _DOC_VERDICT_RE.search(last_message)
    if match is None:
        return _VERDICT_UNKNOWN, last_message
    body = last_message[: match.start()].rstrip()
    return "no_change", body


def _drift_ack_reason(last_message: str) -> Optional[str]:
    """Return the dev's ACK justification if `last_message` carries the
    explicit `ACK: ...` marker, or None when no marker is present.

    Takes the LAST match (matches `_parse_review_verdict`'s convention) so
    a stray reference earlier in the message loses to the concluding line.
    """
    if not last_message:
        return None
    matches = list(_DRIFT_ACK_RE.finditer(last_message))
    if not matches:
        return None
    return matches[-1].group(1).strip() or None


def _parse_orchestrator_continue(comments: list) -> list:
    """Return the comments whose body contains an exact-line
    `/orchestrator continue` operator command."""
    return [
        comment
        for comment in comments
        if _ORCHESTRATOR_CONTINUE_RE.search(comment.body or "")
    ]


def _is_bare_orchestrator_continue(comment) -> bool:
    """True when the comment's ENTIRE body is the command line and nothing
    else -- a content-free nudge whose consumption drops no guidance."""
    return (
        _ORCHESTRATOR_CONTINUE_RE.fullmatch((comment.body or "").strip())
        is not None
    )


def _continue_command_action(new_comments: list, park_reason) -> str:
    """Classify an operator `/orchestrator continue` on a parked dev-session
    stage (`implementing` / `documenting` / `validating` / `resolving_conflict`)
    whose park carries no preserved feedback batch to replay -- the counterpart
    to `fixing`'s richer `_handle_continue_command`, which can reconstruct an
    in_review batch.

    `new_comments` is the fresh trusted issue-thread comments since the last
    consumed watermark. Returns:

      * ``"retry"``       -- a retryable session-failure park
        (`agent_silent` / `agent_timeout`) whose fresh comments are ALL bare
        continues: retry the parked dev flow intentionally, without feeding
        the bare command to the dev as guidance.
      * ``"refuse"``      -- a park that needs real guidance (a genuine agent
        question, a dirty worktree, a diverged branch, ...) whose fresh
        comments are ALL bare continues: the command carries no answer, so
        refuse and stay parked.
      * ``"passthrough"`` -- no continue command is present, or the command
        arrived alongside genuine guidance: the caller's normal
        resume / drift path handles the comments (and feeds that guidance to
        the dev).
    """
    if not _owner._parse_orchestrator_continue(new_comments):
        return "passthrough"
    if not all(
        _owner._is_bare_orchestrator_continue(comment) for comment in new_comments
    ):
        return "passthrough"
    if park_reason in _CONTINUE_PARK_REASONS:
        return "retry"
    return "refuse"


def _refuse_parked_continue(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    """Consume a content-free `/orchestrator continue` and post a refusal on a
    park that needs real human guidance, leaving the issue parked.

    Shared by the dev-parking stages (`implementing`, `documenting`,
    `validating`, `resolving_conflict`): a bare continue on a non-retryable
    park carries no answer, so post a single note and advance the
    issue watermark past BOTH the command and the refusal (so neither re-fires
    next tick and the refusal is not re-posted every poll). `awaiting_human`
    stays set. Mutates in-memory state only; the caller writes pinned state.
    """
    _owner._post_issue_comment(gh, issue, state, _CONTINUE_NEEDS_GUIDANCE_MSG)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)
