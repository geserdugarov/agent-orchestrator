# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing continue."""
from __future__ import annotations

from orchestrator.stages import _implement_state as _state
from orchestrator.stages import implementing as _owner

_PreparedDevRun = _owner._PreparedDevRun
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass
filter_trusted = _owner.filter_trusted
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_BRANCH = _state._BRANCH
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID
_PARK_REASON = _state._PARK_REASON


def _retry_parked_dev_session(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    new_comments: list,
) -> None:
    """Resume the locked dev session as an intentional `/orchestrator continue`
    retry of a session-failure park (`agent_silent` / `agent_timeout`), then
    dispose the result exactly like the awaiting-human resume path.

    Unlike the generic human-reply resume this does NOT feed the bare command
    text to the dev (`_wf._CONTINUE_RETRY_PROMPT` instead): the poisoned session
    already carries the issue context in its transcript, or `_resume_dev_with_text`
    rotates it to a re-grounded fresh spawn. The command comment(s) are marked
    consumed up front so the retry does not re-fire next tick -- every fresh
    comment is a bare continue here (the classifier's retry precondition), so
    this drops no guidance. `user_content_hash` is deliberately NOT refreshed:
    a bare continue never shifts it, and masking it here would swallow a real
    body edit that landed in the same window before the dev could see it.
    """
    from orchestrator import workflow as _wf

    state.set(
        _LAST_ACTION_COMMENT_ID,
        max(comment.id for comment in new_comments),
    )
    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _wf._ensure_worktree(
            spec, issue.number,
            branch=_wf._resolve_branch_name(state, spec, issue.number),
        )
    before_sha = _wf._head_sha(wt)
    followup = f"{_wf._CONTINUE_RETRY_PROMPT}\n\n{_wf._FOREGROUND_ONLY_NOTE}"
    wt, agent_result, paused = _owner._resume_dev_with_text(
        gh, spec, issue, state, followup, pause_guard=True,
    )
    state.set("last_agent_action_at", _wf._now_iso())
    state.set(_BRANCH, _wf._resolve_branch_name(state, spec, issue.number))
    # A shutdown-killed or live-paused resume leaves durable state untouched so
    # the next process re-detects and re-runs the retry (mirrors the drift and
    # fresh-spawn dispositions).
    if _wf._ignore_if_interrupted(issue, agent_result):
        return
    if paused:
        return
    _owner._dispose_agent_result(
        gh, spec, issue, state,
        _PreparedDevRun(agent_result, before_sha, False, wt),
    )


def _handle_parked_continue_command(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    """Handle an operator `/orchestrator continue` on a parked `implementing`
    issue BEFORE generic user-content-drift / resume processing.

    `/orchestrator continue` is the recovery signal for a dev session that hit
    a session/usage limit or a silent failure (`_park_session_limit` /
    `_park_silent_failure` tag both `agent_silent`; an implementer timeout tags
    `agent_timeout`). Counting the bare command as an ordinary comment routed
    it through "issue body/content changed" drift handling and resumed the dev
    for the wrong reason (issue #729); a bare continue no longer shifts
    `user_content_hash`, and this handler routes it deliberately instead.

    Returns True when the command was fully handled this tick (an intentional
    retry ran, or a refusal was posted) and the caller must return. Returns
    False to fall through to the normal flow: the issue is not parked, the park
    belongs to the refresh-time rebase loop, there is no new comment, no
    continue command is present, or the command arrived alongside genuine
    guidance (which the normal resume / drift path feeds to the dev).
    """
    decision = _owner._parked_continue_decision(gh, issue, state)
    if decision is None:
        return False
    if decision.action == "refuse":
        from orchestrator import workflow as _wf

        _wf._refuse_parked_continue(gh, issue, state)
        gh.write_pinned_state(issue, state)
    else:
        _owner._retry_parked_dev_session(
            gh, spec, issue, state, decision.comments,
        )
    return True


@dataclass(frozen=True)
class _ParkedContinueDecision:
    action: str
    comments: list


def _parked_continue_decision(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> Optional[_ParkedContinueDecision]:
    from orchestrator import workflow as _wf

    if not state.get(_AWAITING_HUMAN):
        return None
    park_reason = state.get(_PARK_REASON)
    # Refresh-time auto-rebase parks own their operator retry comment.
    if park_reason in _wf._AUTO_REBASE_PARK_REASONS:
        return None
    comments = filter_trusted(
        gh.comments_after(issue, state.get(_LAST_ACTION_COMMENT_ID))
    )
    if not comments:
        return None
    action = _wf._continue_command_action(comments, park_reason)
    if action == "passthrough":
        return None
    return _ParkedContinueDecision(action, comments)
