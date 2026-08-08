# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The owners in_review borrows from, and the patch boundary each one pins.

The drift route runs a developer and reads what it left behind, but owns
neither half: the resume belongs to `workflow/stages/implementing/`, the
disposition of a body-edit run -- the `ACK:` reply that must not park -- to
`workflow/stages/validating/`, and the checkout it runs in plus the HEAD
watermark it compares against to `git/`. The park vocabulary that tells an
auto-rebase park apart is base-sync's. Each is imported from that owner rather
than read off the `orchestrator.workflow` facade, so a patch that has to
intercept one lands on the owner. Every case patches BOTH -- the owner mock has
to answer and the facade guard has to stay untouched -- which is what fails if a
call site drifts back to `_wf`.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator.git.base_sync import state as _base_sync_state
from orchestrator.workflow.stages.implementing import resume as _dev_resume
from orchestrator.workflow.stages.in_review import (
    drift as _drift,
    feedback as _feedback,
    models as _models,
)
from orchestrator.workflow.stages.validating import (
    drift_outcomes as _drift_outcomes,
)

from tests.fakes import FakeGitHubClient, FakePR, make_issue
from tests.workflow_helpers import _FAKE_WT, _TEST_SPEC, _agent
from tests.workflow_owner_boundaries import OwnerBoundaryMixin

BOUNDARY_ISSUE = 780
BOUNDARY_PR = 781
BOUNDARY_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-780"
BEFORE_SHA = "beforeAA"
OUTCOME_ACK = "ack"
CUSTOM_REBASE_PARK = "auto_base_rebase_custom"

RESUME_WITH_TEXT = "_resume_dev_with_text"
POST_DRIFT_RESULT = "_post_user_content_change_result"
AUTO_REBASE_PARK_REASONS = "_AUTO_REBASE_PARK_REASONS"


def _context(**state_fields) -> _models._InReviewContext:
    """An `in_review` issue with an open PR, bundled as one tick's handles."""
    gh = FakeGitHubClient()
    issue = make_issue(BOUNDARY_ISSUE, label="in_review", body="new acceptance")
    gh.add_issue(issue)
    pr = FakePR(number=BOUNDARY_PR, head_branch=BOUNDARY_BRANCH)
    gh.add_pr(pr)
    gh.seed_state(BOUNDARY_ISSUE, pr_number=BOUNDARY_PR, **state_fields)
    return _models._InReviewContext(
        gh, _TEST_SPEC, issue, gh.read_pinned_state(issue), pr, BOUNDARY_PR,
    )


class ImplementingResumeBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The drift resume and the checkout it runs in land on their owners."""

    def test_drift_resume_lands_on_owner(self) -> None:
        ctx = _context()
        resume_result = (_FAKE_WT, _agent(), False)
        with (
            self.facade_out_of_the_path(
                RESUME_WITH_TEXT, returns=resume_result,
            ),
            self.git_seams_on_owners(
                _worktree_path=MagicMock(return_value=_FAKE_WT),
                _ensure_worktree=MagicMock(return_value=_FAKE_WT),
                _resolve_branch_name=MagicMock(return_value=BOUNDARY_BRANCH),
                _head_sha=MagicMock(return_value=BEFORE_SHA),
            ),
            patch.object(
                _dev_resume, RESUME_WITH_TEXT, return_value=resume_result,
            ) as resume,
        ):
            drift_resume = _drift._resume_dev_for_drift(ctx, [])
            resume.assert_called_once()
        # The pre-resume watermark comes from the probe owner, which is what
        # tells a pushed fix from a no-commit ack downstream.
        self.assertEqual(drift_resume.before_sha, BEFORE_SHA)


class ValidatingDispositionBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The body-edit disposition lands on the validating drift-outcome owner."""

    def test_drift_disposition_lands_on_owner(self) -> None:
        ctx = _context()
        resume = _models._DriftResume(_FAKE_WT, _agent(), False, BEFORE_SHA)
        with (
            self.facade_out_of_the_path(POST_DRIFT_RESULT, returns=OUTCOME_ACK),
            patch.object(
                _drift_outcomes, POST_DRIFT_RESULT, return_value=OUTCOME_ACK,
            ) as dispose,
        ):
            _drift._dispose_drift_result(ctx, [], resume)
            dispose.assert_called_once()
        # An ack invalidates the approval that carried the issue here.
        self.assertEqual(ctx.state.get("review_round"), 0)


class BaseSyncParkReasonBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The auto-rebase park reasons are read off the base-sync owner."""

    def test_auto_rebase_reason_lands_on_owner(self) -> None:
        # A park the base-sync retry loop owns keeps the stage silent even
        # with a fresh comment waiting: that comment is the "retry the rebase"
        # signal, not PR feedback to route to `fixing`.
        state = _context(
            awaiting_human=True, park_reason=CUSTOM_REBASE_PARK,
        ).state
        # The facade answer is held empty: reading it there would let the
        # comment fall through and wake the dev.
        with (
            self.facade_park_reasons_empty(),
            patch.object(
                _base_sync_state,
                AUTO_REBASE_PARK_REASONS,
                frozenset((CUSTOM_REBASE_PARK,)),
            ),
        ):
            self.assertTrue(
                _feedback._stay_parked(state, [object()]),
            )


if __name__ == "__main__":
    unittest.main()
