# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""One reviewer round: the cap that guards it, and the verdict it produces.

The round-cap check comes before the worktree and the spawn, because the cap
is what stops a review loop that cannot converge from spending agent runs
forever; parking on it leaves the PR and worktree intact so an operator can
grant more rounds instead of restarting the issue.

The configured reviewer spec is persisted BEFORE the spawn. A backend hiccup
that yields no session id still leaves a durable record of which spec ran that
round, and a config flip mid-flight cannot retroactively rewrite the history.
Overwriting it every round is correct here precisely because the reviewer is
spawned fresh each time rather than resumed.

After the run, two refusals stand between the reviewer and any disposition,
and their order is the contract. The live-pause check runs first and returns
before usage is folded or the session recorded, so an operator who pauses
mid-review leaves durable state exactly as the prior tick wrote it. The
interruption check runs after that fold but before the timeout and verdict
branches, because a shutdown-killed reviewer emits nothing: read as a verdict
it would park the issue as a reviewer failure AND persist the counters just
folded. Both return without writing, and nothing is stranded -- the reviewer is
read-only and starts over next tick.

The verdict itself fans out to three owners: approved goes to the approval
arc, a missing VERDICT line to the no-verdict park, and CHANGES_REQUESTED to
the fix route. The event is emitted for all of them, before the fan-out, so
the analytics record exists even for the paths that park.
"""
from __future__ import annotations

from typing import Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator.git.worktrees import creation as _worktree_creation
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.engine import messages as _messages
from orchestrator.workflow.engine import prompts as _prompts
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.stages.implementing import session_read as _dev_session_read
from orchestrator.workflow.stages.validating import approval as _approval
from orchestrator.workflow.stages.validating import models as _models
from orchestrator.workflow.stages.validating import requested_changes as _requested_changes
from orchestrator.workflow.stages.validating import state as _state


def _run_reviewer_round(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    pr_number,
) -> Optional[_models._ReviewerRun]:
    round_n = int(state.get(_state._REVIEW_ROUND) or 0)
    if round_n >= config.MAX_REVIEW_ROUNDS:
        _requested_changes._park_review_cap(gh, issue, state, round_n)
        return None

    wt = _worktree_creation._ensure_worktree(
        spec, issue.number,
        branch=_worktree_paths._resolve_branch_name(state, spec, issue.number),
    )
    _, dev_backend_for_prompt, _, _ = _dev_session_read._read_dev_session(state)
    review_prompt = _prompts._build_review_prompt(
        spec, issue, _comments._recent_comments_text(issue),
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
    review = _usage._run_agent_tracked(
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
    if _guards._paused_during_agent_run(gh, issue):
        return None
    _usage._accumulate_issue_usage(state, review.usage)
    if review.session_id:
        state.set("last_review_session_id", review.session_id)
    state.set("last_review_at", _usage._now_iso())

    # Shutdown-sweep interruption: a reviewer run the orchestrator killed
    # mid-flight has no trustworthy verdict. Its empty output would otherwise
    # fall through to the `unknown` -> `reviewer_failed` park below and, on
    # the ensuing `write_pinned_state`, persist the usage counters just folded
    # above (and the session / `last_review_at` mutations). Ignore it and
    # return WITHOUT writing so those in-memory mutations are discarded and the
    # next process re-spawns the reviewer. Must precede the timeout/verdict
    # branches.
    if _guards._ignore_if_interrupted(issue, review):
        return None

    return _models._ReviewerRun(
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
    reviewer_run: _models._ReviewerRun,
) -> None:
    review = reviewer_run.agent_result
    if review.timed_out:
        _guards._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} reviewer timed out after "
            f"{config.REVIEW_TIMEOUT}s; manual intervention needed.",
            reason=_state._REASON_REVIEWER_TIMEOUT,
        )
        # Tag as transient so the next tick re-spawns the reviewer instead
        # of waiting for a human comment that the timeout itself does not
        # produce.
        state.set(_state._PARK_REASON, _state._REASON_REVIEWER_TIMEOUT)
        gh.write_pinned_state(issue, state)
        return

    verdict, body = _messages._parse_review_verdict(review.last_message)
    decision = _models._ReviewerDecision(reviewer_run, verdict, body)
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
        _approval._finalize_validating_approval(
            gh, spec, issue, state, reviewer_run,
        )
        return

    if decision.verdict == "unknown":
        _requested_changes._park_reviewer_no_verdict(gh, issue, state, review)
        return

    # CHANGES_REQUESTED: post the reviewer feedback, flip to `fixing`, and
    # resume the dev. On a pushed fix the handler bumps `review_round` and
    # relabels back to `validating` so the reviewer re-evaluates the new head;
    # on any park the issue stays on `fixing` and the fixing handler owns the
    # awaiting-human rescan.
    _requested_changes._handle_validating_changes_requested(
        gh, spec, issue, state, decision,
    )
