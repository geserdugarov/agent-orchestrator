# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a park is waiting for, and who the reply belongs to.

An awaiting-human `validating` issue is not one state but several, and the
`park_reason` is what says which. Each decision here answers for exactly one
of them and returns None otherwise, so the caller can ask them in order and
fall through to the plain dev resume only when none of them claims the reply.

`review_cap` is the reason a plain resume cannot serve: every round is spent,
so waking the dev would bump the round straight back into the cap next tick.
Only `/orchestrator add-review-rounds N` gets past it, and a reply that is not
that command leaves the issue parked silently rather than spending an agent
run on a do-nothing prompt. The parse walks newest-first so a corrected
command supersedes a stale one in the same batch, and an invalid argument is
answered on the issue instead of guessed at.

The transient reasons are the opposite shape: they fire only when NO comment
arrived, because they exist for conditions that resolve on their own and the
recovery has to stay silent. A reviewer timeout or crash with a reply is the
third: the failure left no review output for the dev to act on, so the comment
buys a fresh REVIEWER rather than a dev resume.

`_run_awaiting_dev` is the fall-through the router uses when none of those
match. It reads HEAD before the resume because that is the only watermark that
can tell a commit this run produced from one already on the branch, and it
splits `retry` from a plain reply -- a retry re-issues the orchestrator's own
continue prompt and consumes the comment, a reply hands the human's words to
the dev.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import creation as _worktree_creation
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.engine import prompts as _prompts
from orchestrator.workflow.stages.implementing import resume as _dev_resume
from orchestrator.workflow.stages.validating import models as _models
from orchestrator.workflow.stages.validating import recovery as _recovery
from orchestrator.workflow.stages.validating import state as _state


def _parse_add_review_rounds(
    comments: list,
) -> Optional[_state._ReviewRoundsCommand]:
    """Find the latest `/orchestrator add-review-rounds N` command across
    `comments`.

    Returns ``(n, None)`` for a valid positive `N`; ``(n, reason)`` when
    the latest match has an invalid argument (caller posts `reason` and
    stays parked); ``None`` when no comment carries the command. Walks
    newest-first so a corrected command supersedes a stale one posted
    earlier in the same batch.
    """
    for comment in reversed(comments):
        body = comment.body or ""
        command_match = _state._ADD_REVIEW_ROUNDS_RE.search(body)
        if not command_match:
            continue
        additional_rounds = int(command_match.group(1))
        if additional_rounds <= 0:
            return (
                additional_rounds,
                f"expected a positive integer (got `{additional_rounds}`)",
            )
        return (additional_rounds, None)
    return None


def _review_cap_awaiting_action(
    context: _models._AwaitingValidation,
) -> Optional[str]:
    if context.park_reason != _state._REASON_REVIEW_CAP:
        return None
    if not context.comments:
        return _state._OUTCOME_RETURN
    command = _parse_add_review_rounds(context.comments)
    if command is None:
        return _state._OUTCOME_RETURN
    context.consume_comments()
    additional_rounds, error = command
    if error is not None:
        _comments._post_issue_comment(
            context.gh,
            context.issue,
            context.state,
            f":warning: `/orchestrator add-review-rounds` ignored: {error}.",
        )
        context.gh.write_pinned_state(context.issue, context.state)
        return _state._OUTCOME_RETURN
    new_round = max(0, config.MAX_REVIEW_ROUNDS - additional_rounds)
    context.state.set(_state._REVIEW_ROUND, new_round)
    context.clear_park()
    _comments._post_issue_comment(
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
    context: _models._AwaitingValidation,
) -> Optional[str]:
    if (
        context.comments
        or context.park_reason not in _state._VALIDATING_TRANSIENT_PARK_REASONS
    ):
        return None
    recovery = _recovery._try_recover_validating_transient_park(
        context.spec, context.issue, context.state,
    )
    if recovery != _state._OUTCOME_STUCK:
        context.clear_park()
        context.gh.write_pinned_state(context.issue, context.state)
    return _state._OUTCOME_RETURN


def _reviewer_retry_awaiting_action(
    context: _models._AwaitingValidation,
) -> Optional[str]:
    if not context.comments or context.park_reason not in (
        _state._REASON_REVIEWER_TIMEOUT, _state._REASON_REVIEWER_FAILED,
    ):
        return None
    context.consume_comments()
    context.clear_park()
    return "spawn_reviewer"


def _resume_awaiting_dev_agent(
    context: _models._AwaitingValidation, continue_action: str,
) -> Optional[tuple[Path, AgentResult, bool]]:
    if continue_action != "retry":
        return _dev_resume._resume_developer_on_human_reply(
            context.gh,
            context.spec,
            context.issue,
            context.state,
            pause_guard=True,
        )
    context.consume_comments()
    followup = f"{_prompts._CONTINUE_RETRY_PROMPT}\n\n{_prompts._FOREGROUND_ONLY_NOTE}"
    return _dev_resume._resume_dev_with_text(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        followup,
        pause_guard=True,
    )


def _run_awaiting_dev(
    context: _models._AwaitingValidation, continue_action: str,
) -> Optional[_models._AwaitingDevAttempt]:
    worktree = _worktree_paths._worktree_path(context.spec, context.issue.number)
    if not worktree.exists():
        worktree = _worktree_creation._ensure_worktree(
            context.spec,
            context.issue.number,
            branch=_worktree_paths._resolve_branch_name(
                context.state, context.spec, context.issue.number,
            ),
        )
    before_sha = _verification_probes._head_sha(worktree)
    resumed = _resume_awaiting_dev_agent(context, continue_action)
    if resumed is None:
        return None
    return _models._AwaitingDevAttempt(
        _models._DevFixRun(resumed[0], resumed[1], before_sha), resumed[2],
    )
