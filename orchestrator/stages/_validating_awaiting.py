# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating awaiting."""
from __future__ import annotations

from orchestrator.stages import _validating_state as _state
from orchestrator.stages import validating as _owner

_DevFixRun = _owner._DevFixRun
AgentResult = _owner.AgentResult
Any = _owner.Any
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass
filter_trusted = _owner.filter_trusted
_OUTCOME_RETURN = _state._OUTCOME_RETURN
_OUTCOME_STUCK = _state._OUTCOME_STUCK
_PARK_REASON = _state._PARK_REASON
_REASON_REVIEWER_FAILED = _state._REASON_REVIEWER_FAILED
_REASON_REVIEWER_TIMEOUT = _state._REASON_REVIEWER_TIMEOUT
_REASON_REVIEW_CAP = _state._REASON_REVIEW_CAP
_REVIEW_ROUND = _state._REVIEW_ROUND
_VALIDATING_TRANSIENT_PARK_REASONS = _state._VALIDATING_TRANSIENT_PARK_REASONS


@dataclass(frozen=True)
class _AwaitingValidation:
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    park_reason: Any
    comments: list

    @classmethod
    def build(
        cls, gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
    ) -> _AwaitingValidation:
        return cls(
            gh,
            spec,
            issue,
            state,
            state.get(_PARK_REASON),
            filter_trusted(
                gh.comments_after(issue, state.get("last_action_comment_id")),
            ),
        )

    def clear_park(self) -> None:
        self.state.set("awaiting_human", False)
        self.state.set(_PARK_REASON, None)

    def consume_comments(self) -> None:
        self.state.set(
            "last_action_comment_id",
            max(comment.id for comment in self.comments),
        )


def _review_cap_awaiting_action(
    context: _AwaitingValidation,
) -> Optional[str]:
    from orchestrator import workflow as _wf

    if context.park_reason != _REASON_REVIEW_CAP:
        return None
    if not context.comments:
        return _OUTCOME_RETURN
    command = _owner._parse_add_review_rounds(context.comments)
    if command is None:
        return _OUTCOME_RETURN
    context.consume_comments()
    additional_rounds, error = command
    if error is not None:
        _wf._post_issue_comment(
            context.gh,
            context.issue,
            context.state,
            f":warning: `/orchestrator add-review-rounds` ignored: {error}.",
        )
        context.gh.write_pinned_state(context.issue, context.state)
        return _OUTCOME_RETURN
    new_round = max(0, config.MAX_REVIEW_ROUNDS - additional_rounds)
    context.state.set(_REVIEW_ROUND, new_round)
    context.clear_park()
    _wf._post_issue_comment(
        context.gh,
        context.issue,
        context.state,
        f":arrows_counterclockwise: review-cap reset: granting "
        f"{additional_rounds} more round(s) "
        f"(`review_round`={new_round}/{config.MAX_REVIEW_ROUNDS}); "
        "rerunning reviewer.",
    )
    return "spawn_reviewer"


def _transient_awaiting_action(
    context: _AwaitingValidation,
) -> Optional[str]:
    if (
        context.comments
        or context.park_reason not in _VALIDATING_TRANSIENT_PARK_REASONS
    ):
        return None
    recovery = _owner._try_recover_validating_transient_park(
        context.spec, context.issue, context.state,
    )
    if recovery != _OUTCOME_STUCK:
        context.clear_park()
        context.gh.write_pinned_state(context.issue, context.state)
    return _OUTCOME_RETURN


def _reviewer_retry_awaiting_action(
    context: _AwaitingValidation,
) -> Optional[str]:
    if not context.comments or context.park_reason not in (
        _REASON_REVIEWER_TIMEOUT, _REASON_REVIEWER_FAILED,
    ):
        return None
    context.consume_comments()
    context.clear_park()
    return "spawn_reviewer"


@dataclass(frozen=True)
class _AwaitingDevAttempt:
    run: _DevFixRun
    paused: bool


def _resume_awaiting_dev_agent(
    context: _AwaitingValidation, continue_action: str,
) -> Optional[tuple[Path, AgentResult, bool]]:
    from orchestrator import workflow as _wf

    if continue_action != "retry":
        return _wf._resume_developer_on_human_reply(
            context.gh,
            context.spec,
            context.issue,
            context.state,
            pause_guard=True,
        )
    context.consume_comments()
    followup = f"{_wf._CONTINUE_RETRY_PROMPT}\n\n{_wf._FOREGROUND_ONLY_NOTE}"
    return _wf._resume_dev_with_text(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        followup,
        pause_guard=True,
    )


def _run_awaiting_dev(
    context: _AwaitingValidation, continue_action: str,
) -> Optional[_AwaitingDevAttempt]:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(context.spec, context.issue.number)
    if not worktree.exists():
        worktree = _wf._ensure_worktree(
            context.spec,
            context.issue.number,
            branch=_wf._resolve_branch_name(
                context.state, context.spec, context.issue.number,
            ),
        )
    before_sha = _wf._head_sha(worktree)
    resumed = _owner._resume_awaiting_dev_agent(context, continue_action)
    if resumed is None:
        return None
    return _AwaitingDevAttempt(
        _DevFixRun(resumed[0], resumed[1], before_sha), resumed[2],
    )
