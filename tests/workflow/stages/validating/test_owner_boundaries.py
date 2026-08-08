# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The owners validating borrows from, and the patch boundary each one pins.

This stage runs a developer between review rounds but owns no dev machinery of
its own: the resume, the session read, and the question / dirty-tree parks
belong to `workflow/stages/implementing/`, and the squash, the checkout the
reviewer reads, and the fetch / ahead-behind pair behind the stranded-fix probe
belong to `git/`. Each is imported from that owner rather than read off the
`orchestrator.workflow` facade, so a patch that has to intercept one lands on
the owner. Every case patches BOTH -- the owner mock has to answer and the
facade guard has to stay untouched -- which is what fails if a call site drifts
back to `_wf`.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import config
from orchestrator.git.publication import squash as _squash
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.stages.implementing import (
    parks as _dev_parks,
    resume as _dev_resume,
    session_read as _dev_session_read,
)
from orchestrator.workflow.stages.validating import (
    approval as _approval,
    awaiting as _awaiting,
    dev_fix as _dev_fix,
    drift_outcomes as _drift_outcomes,
    models as _models,
    reviewer as _reviewer,
)

from tests.workflow.stages.validating.validating_boundary_test_support import (
    BOUNDARY_BRANCH,
    BOUNDARY_PR,
    HUMAN_REPLY_ID,
    _dev_run,
    _seeded,
)
from tests.workflow_helpers import _FAKE_WT, _TEST_SPEC, _agent
from tests.workflow_owner_boundaries import OwnerBoundaryMixin

SQUASHED_SHA = "squashedBB"
DEV_BACKEND = "codex"
SQUASHED_COUNT = 3
OUTCOME_PARKED = "parked"
LAST_ACTION_COMMENT_ID = "last_action_comment_id"

ON_QUESTION = "_on_question"
ON_DIRTY_WORKTREE = "_on_dirty_worktree"
RESUME_ON_HUMAN_REPLY = "_resume_developer_on_human_reply"
RESUME_WITH_TEXT = "_resume_dev_with_text"
READ_DEV_SESSION = "_read_dev_session"
SQUASH_AND_FORCE_PUSH = "_squash_and_force_push"
STRAY_FILE = "stray.txt"
FETCH_OK = 0
AHEAD_ONLY = (1, 0)


class ImplementingParkBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """Both no-commit parks land on the implementing parks owner."""

    def test_dev_fix_question_park_lands_on_owner(self) -> None:
        scenario = _seeded()
        with (
            self.facade_out_of_the_path(ON_QUESTION),
            patch.object(_dev_fix, "_dev_fix_is_publishable", return_value=False),
            patch.object(_dev_parks, ON_QUESTION) as on_question,
        ):
            published = _dev_fix._dispose_dev_fix_result(
                scenario.gh, _TEST_SPEC, scenario.issue, scenario.state, _dev_run(),
            )
            on_question.assert_called_once()
        self.assertFalse(published)

    def test_drift_question_park_lands_on_owner(self) -> None:
        # The drift disposition reaches the same park through its own
        # binding, so it earns its own boundary case.
        scenario = _seeded()
        with (
            self.facade_out_of_the_path(ON_QUESTION),
            patch.object(_dev_fix, "_dev_fix_is_publishable", return_value=False),
            patch.object(_dev_parks, ON_QUESTION) as on_question,
        ):
            outcome = _drift_outcomes._dispose_user_content_change_result(
                scenario.gh, _TEST_SPEC, scenario.issue, scenario.state, _dev_run(),
            )
            on_question.assert_called_once()
        self.assertEqual(outcome, OUTCOME_PARKED)

    def test_dirty_worktree_park_lands_on_owner(self) -> None:
        scenario = _seeded()
        with (
            self.facade_out_of_the_path(ON_DIRTY_WORKTREE),
            self.git_seams_on_owners(
                _worktree_dirty_files=MagicMock(return_value=[STRAY_FILE]),
            ),
            patch.object(_dev_parks, ON_DIRTY_WORKTREE) as on_dirty,
        ):
            published = _dev_fix._publish_dev_fix(
                scenario.gh, _TEST_SPEC, scenario.issue, scenario.state, _dev_run(),
            )
            on_dirty.assert_called_once()
        self.assertFalse(published)


class ImplementingResumeBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """Both awaiting-human resumes land on the implementing resume owner."""

    def test_human_reply_resume_lands_on_owner(self) -> None:
        scenario = _seeded()
        context = _models._AwaitingValidation.build(
            scenario.gh, _TEST_SPEC, scenario.issue, scenario.state,
        )
        with (
            self.facade_out_of_the_path(RESUME_ON_HUMAN_REPLY),
            patch.object(
                _dev_resume, RESUME_ON_HUMAN_REPLY, return_value=None,
            ) as resume,
        ):
            self.assertIsNone(
                _awaiting._resume_awaiting_dev_agent(context, "resume"),
            )
            resume.assert_called_once()

    def test_retry_resume_lands_on_owner(self) -> None:
        scenario = _seeded()
        context = _models._AwaitingValidation.build(
            scenario.gh, _TEST_SPEC, scenario.issue, scenario.state,
        )
        with (
            self.facade_out_of_the_path(
                RESUME_WITH_TEXT, returns=(_FAKE_WT, _agent(), False),
            ),
            patch.object(
                _dev_resume,
                RESUME_WITH_TEXT,
                return_value=(_FAKE_WT, _agent(), False),
            ) as resume,
        ):
            self.assertIsNotNone(
                _awaiting._resume_awaiting_dev_agent(context, "retry"),
            )
            resume.assert_called_once()
        # The retry branch consumes the reply it is answering.
        self.assertEqual(
            scenario.state.get(LAST_ACTION_COMMENT_ID), HUMAN_REPLY_ID,
        )


class ImplementingSessionBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The reviewer prompt reads the dev session off the implementing owner."""

    def test_reviewer_reads_session_off_owner(self) -> None:
        scenario = _seeded(dev_agent=DEV_BACKEND)
        with (
            self.facade_out_of_the_path(
                READ_DEV_SESSION, returns=(None, DEV_BACKEND, None, None),
            ),
            self.git_seams_on_owners(
                _ensure_worktree=MagicMock(return_value=_FAKE_WT),
                _resolve_branch_name=MagicMock(return_value=BOUNDARY_BRANCH),
            ),
            patch.object(
                _dev_session_read,
                READ_DEV_SESSION,
                return_value=(None, DEV_BACKEND, None, None),
            ) as read_session,
            patch.object(_usage, "_run_agent_tracked", return_value=_agent()),
        ):
            reviewer_run = _reviewer._run_reviewer_round(
                scenario.gh, _TEST_SPEC, scenario.issue, scenario.state, BOUNDARY_PR,
            )
            read_session.assert_called_once_with(scenario.state)
        self.assertIsNotNone(reviewer_run)


class StrandedFixProbeBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The stranded-commit probe reads all four of its git seams on owners."""

    def test_probe_reads_every_seam_off_its_owner(self) -> None:
        # This gate is what keeps a commit an earlier parked run left behind
        # from ping-ponging forever, and it is only safe because each read is
        # the real one: a seam answered off the facade would let a stale mock
        # vouch for a branch nobody reconciled.
        scenario = _seeded()
        with self.git_seams_on_owners(
            _worktree_dirty_files=MagicMock(return_value=[]),
            _resolve_branch_name=MagicMock(return_value=BOUNDARY_BRANCH),
            _authed_fetch=MagicMock(return_value=MagicMock(returncode=FETCH_OK)),
            _branch_ahead_behind=MagicMock(return_value=AHEAD_ONLY),
        ):
            stranded = _dev_fix._stranded_fix_unpushed(
                _TEST_SPEC, _FAKE_WT, scenario.state, scenario.issue,
            )
        self.assertTrue(stranded)


class PublicationSquashBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The squash on approval lands on the publication owner."""

    def test_squash_lands_on_owner(self) -> None:
        scenario = _seeded()
        reviewer_run = _models._ReviewerRun(_FAKE_WT, 0, BOUNDARY_PR, _agent())
        squash_result = (True, SQUASHED_SHA, SQUASHED_COUNT, None)
        with (
            self.facade_out_of_the_path(
                SQUASH_AND_FORCE_PUSH, returns=squash_result,
            ),
            patch.object(config, "SQUASH_ON_APPROVAL", True),
            self.git_seams_on_owners(
                _resolve_branch_name=MagicMock(return_value=BOUNDARY_BRANCH),
            ),
            patch.object(
                _squash, SQUASH_AND_FORCE_PUSH, return_value=squash_result,
            ) as squash_call,
        ):
            squashed = _approval._squash_approved_work(
                scenario.gh, _TEST_SPEC, scenario.issue, scenario.state, reviewer_run,
            )
            squash_call.assert_called_once()
        self.assertEqual(squashed, SQUASHED_COUNT)


if __name__ == "__main__":
    unittest.main()
