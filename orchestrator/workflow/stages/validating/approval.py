# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Everything an approved review still has to survive before it hands off.

The reviewer's verdict is not the last gate. The local verify run comes first
so an obviously-broken branch never reaches `in_review`, where the next reader
is a human deciding whether to merge; a default-empty `VERIFY_COMMANDS`
short-circuits to ok, and a failure parks in `validating` with a durable
reason rather than advancing. The squash follows, and its failure parks
WITHOUT relabeling on purpose -- the original commits are still on the branch,
and only a human can decide whether to keep the history or force it flat.

The ordering inside the handoff matters too. The squash notice is posted
BEFORE the watermarks are seeded so that its own id lands in the recorded
orchestrator set and the seed walk steps past it; the reverse order would hand
in_review an informational post as fresh human PR feedback and wake the dev on
it. A `get_pr` failure is not fatal here -- in_review still has its legacy
watermark to fall back on -- so it logs and skips the seed rather than
stranding an approved branch.

The relabel goes to `documenting`, not straight to `in_review`: the final docs
pass runs against the approved head, and everything seeded here survives that
hop.
"""
from __future__ import annotations

from typing import Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator._workflow_state import log
from orchestrator.git.publication import squash as _squash
from orchestrator.git.verification import runner as _verify_runner
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.stages.validating import models as _models
from orchestrator.workflow.stages.validating import verify as _verify
from orchestrator.workflow.stages.validating import watermarks as _watermarks
from orchestrator.workflow.state import WorkflowLabel


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
    if pr_number is None:
        return
    try:
        pr = gh.get_pr(int(pr_number))
    except Exception as error:
        # Surface the failure but skip the traceback -- it adds no signal.
        log.warning(
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
            _comments._post_pr_comment(
                gh, int(pr_number), state,
                f":package: squashed {squashed_count} commits "
                "to 1 after approval",
            )
        except Exception:
            log.exception(
                "issue=#%s could not post squash notice to "
                "PR #%s", issue.number, pr_number,
            )
    _seed_in_review_pr_watermarks(gh, issue, state, pr)


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
    issue_wm, review_wm = _watermarks._latest_pr_comment_ids(gh, issue, pr, state)
    state.set(
        "pr_last_comment_id",
        _watermarks._ratchet_watermark(state.get("pr_last_comment_id"), issue_wm),
    )
    state.set(
        "pr_last_review_comment_id",
        _watermarks._ratchet_watermark(state.get("pr_last_review_comment_id"), review_wm),
    )
    state.set(
        "pr_last_review_summary_id",
        _watermarks._ratchet_watermark(state.get("pr_last_review_summary_id"), None),
    )


def _approved_work_verifies(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _models._ReviewerRun,
) -> bool:
    verify = _verify_runner._run_verify_commands(
        reviewer_run.wt, config.VERIFY_COMMANDS, config.VERIFY_TIMEOUT,
    )
    if verify.status == "ok":
        return True
    _verify._park_verify_failure(gh, issue, state, verify)
    gh.write_pinned_state(issue, state)
    return False


def _post_approval_comment(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _models._ReviewerRun,
) -> None:
    if reviewer_run.pr_number is None:
        return
    try:
        _comments._post_pr_comment(
            gh,
            int(reviewer_run.pr_number),
            state,
            f":white_check_mark: {config.REVIEW_AGENT} review approved.",
        )
    except Exception:
        log.exception(
            "issue=#%s could not post approval to PR #%s",
            issue.number,
            reviewer_run.pr_number,
        )


def _park_squash_failure(
    gh: GitHubClient, issue: Issue, state: PinnedState, error,
) -> None:
    _guards._park_awaiting_human(
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
    reviewer_run: _models._ReviewerRun,
) -> Optional[int]:
    if not config.SQUASH_ON_APPROVAL:
        return 0
    squash_result = _squash._squash_and_force_push(
        spec,
        reviewer_run.wt,
        _worktree_paths._resolve_branch_name(state, spec, issue.number),
        issue,
    )
    if squash_result[0]:
        return squash_result[2]
    _park_squash_failure(gh, issue, state, squash_result[3])
    return None


def _finalize_validating_approval(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    reviewer_run: _models._ReviewerRun,
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
    if not _approved_work_verifies(gh, issue, state, reviewer_run):
        return
    _post_approval_comment(gh, issue, state, reviewer_run)
    squashed_count = _squash_approved_work(
        gh, spec, issue, state, reviewer_run,
    )
    if squashed_count is None:
        return
    _seed_in_review_handoff_watermarks(
        gh, issue, state, reviewer_run.pr_number, squashed_count,
    )
    gh.set_workflow_label(issue, WorkflowLabel.DOCUMENTING)
    gh.write_pinned_state(issue, state)
