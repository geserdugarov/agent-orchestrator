# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating approval."""
from __future__ import annotations

from orchestrator.stages import validating as _owner

_ReviewerRun = _owner._ReviewerRun
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config


def _seed_in_review_handoff_watermarks(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    pr_number,
    squashed_count: int,
) -> None:
    """Seed the in_review comment watermarks so `_handle_in_review` does not
    replay the orchestrator's own automated comments ("picking this up",
    "PR opened", the approval just posted, the squash notice) as fresh PR
    feedback once the debounce expires.

    A get_pr failure is recoverable -- the in_review handler falls back to its
    legacy `last_action_comment_id` watermark -- so we log and return without
    seeding.
    """
    from orchestrator import workflow as _wf

    if pr_number is None:
        return
    try:
        pr = gh.get_pr(int(pr_number))
    except Exception as error:
        # Surface the failure but skip the traceback -- it adds no signal.
        _wf.log.warning(
            "issue=#%s could not snapshot PR #%s for in_review "
            "handoff: %s", issue.number, pr_number, error,
        )
        return
    # Post the squash PR comment BEFORE seeding watermarks so the seed walks
    # past it (its id lands in `orchestrator_comment_ids` via `_post_pr_comment`).
    # Without that ordering, the next in_review tick treats the squash comment
    # as fresh PR feedback once the debounce expires and resumes the dev
    # session over an informational orchestrator post.
    if squashed_count > 1:
        try:
            _wf._post_pr_comment(
                gh, int(pr_number), state,
                f":package: squashed {squashed_count} commits "
                "to 1 after approval",
            )
        except Exception:
            _wf.log.exception(
                "issue=#%s could not post squash notice to "
                "PR #%s", issue.number, pr_number,
            )
    _owner._seed_in_review_pr_watermarks(gh, issue, state, pr)


def _seed_in_review_pr_watermarks(
    gh: GitHubClient, issue: Issue, state: PinnedState, pr,
) -> None:
    """Seed the three in_review comment watermarks past the leading run of
    orchestrator-authored comments on `pr`'s surfaces.

    Used by validating's reviewer-approval handoff
    (`_seed_in_review_handoff_watermarks`) so `_handle_in_review` does not
    replay the orchestrator's own automated comments (pickup ping, "PR opened",
    approval, squash notice) as fresh PR feedback once the debounce expires.
    Concurrent human feedback posted during the prior stage is preserved:
    `_latest_pr_comment_ids` stops the seed walk at the first unread
    non-orchestrator comment, and `_ratchet_watermark` never regresses a
    watermark a prior in_review tick already advanced.

    Inline review comments and review summaries live in namespaces the
    orchestrator never posts on, so `_latest_pr_comment_ids` returns None for
    the inline surface and there is no seeded summary value; `_ratchet_watermark`
    defaults each to 0 so the in_review legacy migration treats them as already
    seeded and does NOT advance past human feedback submitted on those surfaces.
    """
    issue_wm, review_wm = _owner._latest_pr_comment_ids(gh, issue, pr, state)
    state.set(
        "pr_last_comment_id",
        _owner._ratchet_watermark(state.get("pr_last_comment_id"), issue_wm),
    )
    state.set(
        "pr_last_review_comment_id",
        _owner._ratchet_watermark(state.get("pr_last_review_comment_id"), review_wm),
    )
    state.set(
        "pr_last_review_summary_id",
        _owner._ratchet_watermark(state.get("pr_last_review_summary_id"), None),
    )


def _approved_work_verifies(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> bool:
    from orchestrator import workflow as _wf

    verify = _wf._run_verify_commands(
        reviewer_run.wt, config.VERIFY_COMMANDS, config.VERIFY_TIMEOUT,
    )
    if verify.status == "ok":
        return True
    _owner._park_verify_failure(gh, issue, state, verify)
    gh.write_pinned_state(issue, state)
    return False


def _post_approval_comment(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> None:
    from orchestrator import workflow as _wf

    if reviewer_run.pr_number is None:
        return
    try:
        _wf._post_pr_comment(
            gh,
            int(reviewer_run.pr_number),
            state,
            f":white_check_mark: {config.REVIEW_AGENT} review approved.",
        )
    except Exception:
        _wf.log.exception(
            "issue=#%s could not post approval to PR #%s",
            issue.number,
            reviewer_run.pr_number,
        )


def _park_squash_failure(
    gh: GitHubClient, issue: Issue, state: PinnedState, error,
) -> None:
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh,
        issue,
        state,
        f"{config.HITL_MENTIONS} squash-on-approval failed "
        f"({error}); the original commits are still on the "
        "branch and the PR was not relabeled. Manual "
        "intervention needed (squash + force-push by hand, "
        "or set `SQUASH_ON_APPROVAL=off` and re-run the "
        "reviewer).",
        reason="squash_failed",
    )
    gh.write_pinned_state(issue, state)


def _squash_approved_work(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> Optional[int]:
    from orchestrator import workflow as _wf

    if not config.SQUASH_ON_APPROVAL:
        return 0
    squash_result = _wf._squash_and_force_push(
        spec,
        reviewer_run.wt,
        _wf._resolve_branch_name(state, spec, issue.number),
        issue,
    )
    if squash_result[0]:
        return squash_result[2]
    _owner._park_squash_failure(gh, issue, state, squash_result[3])
    return None


def _finalize_validating_approval(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _ReviewerRun,
) -> None:
    """Finalize an approved review: verify gate, approval comment, optional
    squash, in_review handoff watermarks, then relabel to `documenting`.

    The verify gate is the first gate after the reviewer so an obviously-broken
    branch never reaches `in_review` (GitHub CI still runs against the PR for
    the human merging it). Default-empty `VERIFY_COMMANDS` short-circuits to
    "ok". A failed / timed-out command or a dirty tree left behind parks
    awaiting_human in `validating` with a stable `park_reason`. A failed
    squash / force-push also parks and STAYS in `validating` (no relabel) so
    the original commits remain on the branch for a human to adjudicate. On
    success the (possibly squashed) head routes through `documenting` for a
    final docs pass before in_review picks up; the watermarks, approval, and
    squash comment seeded here are preserved across the documenting hop.
    """
    if not _owner._approved_work_verifies(gh, issue, state, reviewer_run):
        return
    _owner._post_approval_comment(gh, issue, state, reviewer_run)
    squashed_count = _owner._squash_approved_work(
        gh, spec, issue, state, reviewer_run,
    )
    if squashed_count is None:
        return
    _owner._seed_in_review_handoff_watermarks(
        gh, issue, state, reviewer_run.pr_number, squashed_count,
    )
    gh.set_workflow_label(issue, WorkflowLabel.DOCUMENTING)
    gh.write_pinned_state(issue, state)
