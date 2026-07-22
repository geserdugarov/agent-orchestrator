# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating handler."""
from __future__ import annotations

from orchestrator.stages import _validating_state as _state
from orchestrator.stages import validating as _owner

_ReviewerDecision = _owner._ReviewerDecision
_ReviewerRun = _owner._ReviewerRun
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
config = _owner.config
_OUTCOME_RETURN = _state._OUTCOME_RETURN
_PARK_REASON = _state._PARK_REASON
_REASON_REVIEWER_TIMEOUT = _state._REASON_REVIEWER_TIMEOUT
_REVIEW_ROUND = _state._REVIEW_ROUND


def _run_reviewer_round(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    pr_number,
) -> Optional[_ReviewerRun]:
    from orchestrator import workflow as _wf

    round_n = int(state.get(_REVIEW_ROUND) or 0)
    if round_n >= config.MAX_REVIEW_ROUNDS:
        _owner._park_review_cap(gh, issue, state, round_n)
        return None

    wt = _wf._ensure_worktree(
        spec, issue.number,
        branch=_wf._resolve_branch_name(state, spec, issue.number),
    )
    _, dev_backend_for_prompt, _, _ = _wf._read_dev_session(state)
    review_prompt = _wf._build_review_prompt(
        spec, issue, _wf._recent_comments_text(issue),
        config.default_repo_specs(), dev_backend_for_prompt,
    )
    # Persist the full configured spec BEFORE the spawn so a reviewer
    # backend hiccup that yields no session id still leaves a durable
    # role-identity record. The trace reflects the reviewer's CLI args
    # and a config flip mid-flight cannot retroactively rewrite which
    # spec ran each round. The reviewer is spawned fresh each round
    # (no resume), so always overwriting the field with the current
    # config spec is the right behavior here.
    state.set("review_agent", config.REVIEW_AGENT_SPEC)
    review = _wf._run_agent_tracked(
        gh, issue.number,
        agent_role="reviewer",
        stage="validating",
        backend=config.REVIEW_AGENT,
        prompt=review_prompt,
        cwd=wt,
        agent_spec=config.REVIEW_AGENT_SPEC,
        timeout=config.REVIEW_TIMEOUT,
        extra_args=config.REVIEW_AGENT_ARGS,
        review_round=round_n,
        retry_count=state.get("retry_count"),
    )
    # Live pause: an operator applied `paused` / `backlog` while the reviewer
    # ran. Dispatch only saw the pre-run labels, so re-check a freshly fetched
    # issue and return WITHOUT folding usage, recording the review session,
    # parking, or relabeling -- durable GitHub state stays exactly as the prior
    # tick left it and the next tick re-spawns a fresh reviewer once the label
    # is removed. Nothing is stranded: the reviewer is read-only and spawns
    # fresh each round.
    if _wf._paused_during_agent_run(gh, issue):
        return None
    _wf._accumulate_issue_usage(state, review.usage)
    if review.session_id:
        state.set("last_review_session_id", review.session_id)
    state.set("last_review_at", _wf._now_iso())

    # Shutdown-sweep interruption: a reviewer run the orchestrator killed
    # mid-flight has no trustworthy verdict. Its empty output would otherwise
    # fall through to the `unknown` -> `reviewer_failed` park below and, on
    # the ensuing `write_pinned_state`, persist the usage counters just folded
    # above (and the session / `last_review_at` mutations). Ignore it and
    # return WITHOUT writing so those in-memory mutations are discarded and the
    # next process re-spawns the reviewer. Must precede the timeout/verdict
    # branches.
    if _wf._ignore_if_interrupted(issue, review):
        return None

    return _ReviewerRun(
        wt=wt,
        round_n=round_n,
        pr_number=pr_number,
        agent_result=review,
    )


def _dispatch_reviewer_result(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> None:
    from orchestrator import workflow as _wf

    review = reviewer_run.agent_result
    if review.timed_out:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} reviewer timed out after "
            f"{config.REVIEW_TIMEOUT}s; manual intervention needed.",
            reason=_REASON_REVIEWER_TIMEOUT,
        )
        # Tag as transient so the next tick re-spawns the reviewer instead
        # of waiting for a human comment that the timeout itself does not
        # produce.
        state.set(_PARK_REASON, _REASON_REVIEWER_TIMEOUT)
        gh.write_pinned_state(issue, state)
        return

    verdict, body = _wf._parse_review_verdict(review.last_message)
    decision = _ReviewerDecision(reviewer_run, verdict, body)
    gh.emit_event(
        "review_verdict",
        issue_number=issue.number,
        stage="validating",
        verdict=verdict,
        review_round=reviewer_run.round_n,
        pr_number=(
            None if reviewer_run.pr_number is None
            else int(reviewer_run.pr_number)
        ),
        session_id=review.session_id,
    )

    if decision.verdict == "approved":
        _owner._finalize_validating_approval(
            gh, spec, issue, state, reviewer_run,
        )
        return

    if decision.verdict == "unknown":
        _owner._park_reviewer_no_verdict(gh, issue, state, review)
        return

    # CHANGES_REQUESTED: post the reviewer feedback, flip to `fixing`, and
    # resume the dev. On a pushed fix the handler bumps `review_round` and
    # relabels back to `validating` so the reviewer re-evaluates the new head;
    # on any park the issue stays on `fixing` and the fixing handler owns the
    # awaiting-human rescan.
    _owner._handle_validating_changes_requested(
        gh, spec, issue, state, decision,
    )


def _handle_validating(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if _owner._finalize_validating_terminal(gh, spec, issue, state):
        return

    # User-content drift resume runs before the awaiting-human and reviewer
    # branches: a body edit mid-review must resume the dev on the new body
    # rather than re-review stale work. Returns True when it fully handled the
    # tick; a reviewer-side (`reviewer_timeout` / `reviewer_failed`) or
    # `review_cap` park defers to the awaiting-human branch below (that branch
    # owns the human's "retry" / `/orchestrator add-review-rounds` comment).
    if _owner._resume_dev_on_validating_drift(gh, spec, issue, state):
        return

    # Awaiting-human path: human replied after a park (or a transient
    # condition self-resolved). The helper resumes the dev on their feedback,
    # recovers transient parks silently, or clears a reviewer-side / review-cap
    # park into a reviewer re-run. "return" -> the tick is fully handled;
    # "spawn_reviewer" -> fall through to the round-cap check and reviewer
    # spawn below.
    if state.get("awaiting_human"):
        if _owner._handle_validating_awaiting_human(
            gh, spec, issue, state
        ) == _OUTCOME_RETURN:
            return

    reviewer_run = _owner._run_reviewer_round(gh, spec, issue, state, pr_number)
    if reviewer_run is None:
        return

    _owner._dispatch_reviewer_result(gh, spec, issue, state, reviewer_run)
