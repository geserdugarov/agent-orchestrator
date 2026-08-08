# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The two verdicts that are not an approval.

CHANGES_REQUESTED runs its dev fix under the `fixing` label rather than
`validating`, so the active job reads as what it is instead of as reviewer
work. The relabel happens BEFORE the spawn on purpose: a crash inside the
spawn then leaves the issue on `fixing` with `awaiting_human` still false,
which the next tick's fixing handler reads as "no feedback" and bounces back
to `validating` -- whereas a crash after a spawn under the old label would
leave an issue nobody re-enters. A pushed fix bumps the round and relabels
back; any park leaves the issue on `fixing`, whose handler owns the
awaiting-human rescan from there.

The reviewer-feedback comment's id is recorded because a session-failure park
on this route has to be retryable by `/orchestrator continue`, and the fixing
handler replays that exact comment to reconstruct the batch. It is a
standalone key rather than part of the in_review bookmark pair, since
`pending_fix_at` is what tells that route's round RESET from this route's
bump.

A reviewer that emitted no VERDICT line is the other verdict, and it splits
by whether there was any output at all. An empty last message with a non-zero
exit is a crash, tagged transient so the next tick re-spawns the reviewer --
waking the dev on a human "retry" would hand the wrong agent a prompt with no
review in it. Real text that merely omitted the marker is left for a human,
and the stderr tail is suppressed there because the human is already reading
model output.
"""
from __future__ import annotations

from github.Issue import Issue

from orchestrator import config
from orchestrator._workflow_state import log
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.engine import messages as _messages
from orchestrator.workflow.engine import prompts as _prompts
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.stages.implementing import resume as _dev_resume
from orchestrator.workflow.stages.validating import dev_fix as _dev_fix
from orchestrator.workflow.stages.validating import models as _models
from orchestrator.workflow.stages.validating import state as _state
from orchestrator.workflow.state import WorkflowLabel


def _park_reviewer_no_verdict(
    gh: GitHubClient, issue: Issue, state: PinnedState, review
) -> None:
    """Park `validating` when the reviewer produced no VERDICT line.

    A silent crash (empty last message + non-zero exit -- codex-side error,
    network blip) is tagged transient (`reviewer_failed`) so the next tick
    re-spawns the reviewer instead of waking the dev on a human "Retry" comment;
    there is no review output the dev could act on, and
    `_resume_developer_on_human_reply` would otherwise hand the wrong agent a
    do-nothing prompt. A reviewer that emitted text but merely omitted the
    VERDICT line is left as `reviewer_no_verdict` for human adjudication, and
    stderr diagnostics are suppressed (the human is reading real model output).
    """
    raw = (review.last_message or "").strip() or "(reviewer produced no final message)"
    quoted = _messages._as_blockquote(raw)
    silent_crash = (
        not (review.last_message or "").strip() and review.exit_code != 0
    )
    diag = (
        ""
        if (review.last_message or "").strip()
        else _messages._format_stderr_diagnostics(review, "Reviewer")
    )
    _guards._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} reviewer did not emit a VERDICT line; "
        f"manual adjudication needed.\n\n_Last reviewer message:_\n\n"
        f"{quoted}{diag}",
        reason=_state._REASON_REVIEWER_FAILED if silent_crash else "reviewer_no_verdict",
    )
    if silent_crash:
        state.set(_state._PARK_REASON, _state._REASON_REVIEWER_FAILED)
    log.warning(
        "issue=#%s reviewer emitted no VERDICT; exit_code=%d "
        "timed_out=%s stderr_tail=%r",
        issue.number, review.exit_code, review.timed_out,
        _messages._stderr_log_tail(review),
    )
    gh.write_pinned_state(issue, state)


def _post_reviewer_feedback(context: _models._RequestedChanges) -> None:
    reviewer_run = context.decision.run
    if reviewer_run.pr_number is None:
        return
    round_display = reviewer_run.round_n + 1
    feedback = context.decision.feedback
    try:
        reviewer_comment = _comments._post_pr_comment(
            context.gh,
            int(reviewer_run.pr_number),
            context.state,
            f":eyes: {config.REVIEW_AGENT} review "
            f"(round {round_display}/"
            f"{config.MAX_REVIEW_ROUNDS}) requested changes:\n\n"
            f"{feedback}",
        )
    except Exception:
        log.exception(
            "issue=#%s could not post review to PR #%s",
            context.issue.number,
            reviewer_run.pr_number,
        )
        return
    anchor_id = getattr(reviewer_comment, "id", None)
    if anchor_id is not None:
        context.state.set("pending_fix_reviewer_comment_id", int(anchor_id))


def _run_requested_fix(context: _models._RequestedChanges) -> _models._AwaitingDevAttempt:
    before_sha = _verification_probes._head_sha(context.decision.run.wt)
    # The caller flipped the label validating -> fixing on the SAME `issue`
    # object; PyGithub does not refresh its cached `labels` after
    # `set_labels`, so pass `fixing` explicitly rather than let the resume
    # helper read the stale `validating` back off the issue and attribute this
    # developer run to the reviewer's stage.
    worktree, agent_result, paused = _dev_resume._resume_dev_with_text(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        _prompts._build_fix_prompt(context.decision.feedback),
        stage=WorkflowLabel.FIXING,
        pause_guard=True,
    )
    context.state.set("last_agent_action_at", _usage._now_iso())
    return _models._AwaitingDevAttempt(
        _models._DevFixRun(worktree, agent_result, before_sha), paused,
    )


def _finish_requested_fix(
    context: _models._RequestedChanges, attempt: _models._AwaitingDevAttempt,
) -> None:
    if attempt.paused:
        return
    pushed = _dev_fix._handle_dev_fix_result(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        attempt.run.worktree,
        attempt.run.agent_result,
        attempt.run.before_sha,
    )
    if not pushed:
        if not attempt.run.agent_result.interrupted:
            context.gh.write_pinned_state(context.issue, context.state)
        return
    context.state.set(_state._REVIEW_ROUND, context.decision.run.round_n + 1)
    context.state.set("pending_fix_reviewer_comment_id", None)
    context.gh.set_workflow_label(context.issue, WorkflowLabel.VALIDATING)
    context.gh.write_pinned_state(context.issue, context.state)


def _handle_validating_changes_requested(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    decision: _models._ReviewerDecision,
) -> None:
    """CHANGES_REQUESTED: post the reviewer feedback on the PR, flip to
    `fixing`, and resume the dev.

    The dev-fix subphase runs under the `fixing` label so the active job is
    observably "fixing reviewer-requested changes" rather than "validating"
    (which reads as reviewer/verify work only); `fixing` thereby extends to
    automated reviewer feedback in addition to its original in_review
    human-feedback duty. The label is flipped BEFORE the dev spawn so a crash
    inside the spawn still leaves the issue on `fixing` with stale
    awaiting_human=False, which the next tick's fixing handler treats as
    no-feedback and bounces back to `validating`. On a successful pushed fix we
    bump `review_round` and relabel to `validating`; on any park the issue stays
    on `fixing` and the fixing handler owns the awaiting-human rescan.
    `review_round` accounting, `MAX_REVIEW_ROUNDS`, dev-session pinning, and the
    final-docs handoff are unchanged -- only the visible label moves with the
    active work.

    The id of the reviewer-feedback PR comment is recorded in
    `pending_fix_reviewer_comment_id` so a session-failure park on this route
    (`agent_silent` / `agent_timeout`) is retryable by `/orchestrator continue`:
    the fixing handler's `_reconstruct_pending_fix_batch` replays that exact
    comment. `pending_fix_at` is deliberately NOT set (it discriminates the
    in_review route's review-round reset from this route's bump), so the anchor
    is a standalone key cleared on the pushed-fix exit here and inside
    `_clear_pending_fix_bookmarks`.
    """
    context = _models._RequestedChanges(gh, spec, issue, state, decision)
    _post_reviewer_feedback(context)
    gh.set_workflow_label(issue, WorkflowLabel.FIXING)
    gh.write_pinned_state(issue, state)
    _finish_requested_fix(context, _run_requested_fix(context))


def _park_review_cap(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    round_n: int,
) -> None:
    _guards._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} review still has comments after "
        f"{round_n} round(s); manual intervention needed. To grant "
        "more rounds without losing the PR/worktree, reply with "
        "`/orchestrator add-review-rounds N` "
        "(N = additional rounds, e.g. `1`).",
        reason=_state._REASON_REVIEW_CAP,
    )
    # `_park_awaiting_human` clears `park_reason` by contract; the
    # awaiting-human branch needs this transient reason to route the
    # operator's `/orchestrator add-review-rounds` command.
    state.set(_state._PARK_REASON, _state._REASON_REVIEW_CAP)
    gh.write_pinned_state(issue, state)
