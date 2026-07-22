# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing parks."""
from __future__ import annotations

from orchestrator.stages import _implement_state as _state
from orchestrator.stages import implementing as _owner

AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID
_PARK_REASON = _state._PARK_REASON
_SILENT_PARK_COUNT = _state._SILENT_PARK_COUNT


def _mark_agent_silent_park(state: PinnedState) -> None:
    """Flag a retryable `agent_silent` park and advance the silent-park streak.

    Shared by the session-limit and empty-output parks: both are retryable
    `agent_silent` failures, not real questions. `_resume_dev_with_text` reads
    the streak (via `_dev_session_retirement_reason`) to rotate a poisoned
    session to a fresh spawn once it reaches `_SILENT_PARKS_BEFORE_FRESH_SESSION`.
    """
    count = int(state.get(_SILENT_PARK_COUNT) or 0)
    state.set(_AWAITING_HUMAN, True)
    state.set(_PARK_REASON, "agent_silent")
    state.set(_SILENT_PARK_COUNT, count + 1)


def _park_session_limit(
    gh: GitHubClient, issue: Issue, state: PinnedState, raw: str
) -> str:
    """Park a session/usage-quota notice as a RETRYABLE session failure.

    A known quota notice ("You've hit your session limit ...") is non-empty but
    is NOT a real agent question: the session is healthy, the account quota is
    exhausted, and the only recovery is to wait for the reset and retry.
    Parking it as `agent_silent` (the same reason a silent poisoned resume
    uses) lets an operator's `/orchestrator continue` after the reset drop the
    session and re-ground a fresh one; classifying it as a real question
    (`park_reason=None`) would refuse that continue as "needs your actual
    guidance". The silent-park streak is incremented so a session that keeps
    returning the quota notice is eventually rotated, mirroring the
    empty-message branch. Returns the distinct EVENT reason
    (`agent_session_limit`) for observability -- the pinned `park_reason` stays
    `agent_silent` (the control field `/orchestrator continue` keys off).
    """
    from orchestrator import workflow as _wf

    quoted = _owner._as_blockquote(raw)
    _wf._post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent hit a session/usage limit and "
        "stopped; retry with `/orchestrator continue` once it "
        f"resets:\n\n{quoted}",
    )
    _owner._mark_agent_silent_park(state)
    return "agent_session_limit"


def _park_real_question(
    gh: GitHubClient, issue: Issue, state: PinnedState, raw: str
) -> str:
    """Park a genuine agent clarification question awaiting a human reply."""
    from orchestrator import workflow as _wf

    quoted = _owner._as_blockquote(raw)
    _wf._post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent needs your input to proceed:\n\n{quoted}",
    )
    state.set(_AWAITING_HUMAN, True)
    # Real question parks are not transient: they need a human reply before the
    # in_review ready-ping gates should run again. Clear any stale
    # `park_reason` left behind by a prior in_review unmergeable park, and reset
    # the silent-park streak.
    state.set(_PARK_REASON, None)
    state.set(_SILENT_PARK_COUNT, 0)
    return "agent_question"


def _park_silent_failure(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    agent_result: AgentResult,
) -> str:
    """Park a run that produced no commit AND no message as a silent failure.

    Callers only invoke `_on_question` when the worktree has no new commits, so
    an empty `last_message` is a silent failure, not a content question -- most
    often a poisoned resume of a session killed mid-stream (e.g. by a Claude
    rate limit). Tag the park `agent_silent` so `_resume_dev_with_text` can
    drop the dev session id after enough consecutive silent parks, and surface
    the situation accurately instead of impersonating a real question park.
    """
    from orchestrator import workflow as _wf

    diag = _wf._format_stderr_diagnostics(agent_result, "Agent")
    _wf._post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent produced no output (likely a "
        f"session-resume failure); manual intervention needed.{diag}",
    )
    _wf.log.warning(
        "issue=#%s agent produced no output; exit_code=%d "
        "timed_out=%s stderr_tail=%r",
        issue.number, agent_result.exit_code, agent_result.timed_out,
        _wf._stderr_log_tail(agent_result),
    )
    _owner._mark_agent_silent_park(state)
    return "agent_silent"


def _on_question(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    agent_result: AgentResult,
) -> None:
    raw = agent_result.last_message.strip()
    if raw and _owner._is_session_limit_message(agent_result):
        park_reason = _owner._park_session_limit(gh, issue, state, raw)
    elif raw:
        park_reason = _owner._park_real_question(gh, issue, state, raw)
    else:
        park_reason = _owner._park_silent_failure(gh, issue, state, agent_result)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set(_LAST_ACTION_COMMENT_ID, latest)
    gh.emit_event(
        "park_awaiting_human",
        issue_number=issue.number,
        stage=gh.workflow_label(issue),
        reason=park_reason,
    )


def _on_dirty_worktree(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    agent_result: AgentResult,
    dirty: list[str],
) -> None:
    """Park instead of pushing when the agent left uncommitted changes.

    Pushing here would publish a branch that omits the dirty files, so the PR
    would not match what the agent actually produced. We surface the situation
    to the human and resume the codex session on their reply, identical to the
    question path.
    """
    from orchestrator import workflow as _wf

    _wf._post_issue_comment(
        gh, issue, state, _owner._dirty_worktree_message(agent_result, dirty),
    )
    state.set(_AWAITING_HUMAN, True)
    # Mirror `_on_question`: this needs human input, so stale transient state
    # must not auto-recover over it.
    state.set(_PARK_REASON, None)
    state.set(_SILENT_PARK_COUNT, 0)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set(_LAST_ACTION_COMMENT_ID, latest)
    gh.emit_event(
        "park_awaiting_human",
        issue_number=issue.number,
        stage=gh.workflow_label(issue),
        reason="dirty_worktree",
        dirty_files=len(dirty),
    )


def _dirty_worktree_message(
    agent_result: AgentResult, dirty: list[str],
) -> str:
    shown = dirty[:10]
    files_md = "\n".join(f"- `{file_path}`" for file_path in shown)
    if len(dirty) > len(shown):
        elided = len(dirty) - len(shown)
        files_md = f"{files_md}\n- … ({elided} more)"
    last_msg = agent_result.last_message.strip()
    tail = ""
    if last_msg:
        tail = f"\n\n_Last agent message:_\n\n{_owner._as_blockquote(last_msg)}"
    return (
        f"{config.HITL_MENTIONS} agent committed but left {len(dirty)} "
        f"uncommitted change(s); refusing to push an incomplete branch. "
        f"Reply with guidance and the orchestrator will resume the session.\n\n"
        f"{files_md}{tail}"
    )
