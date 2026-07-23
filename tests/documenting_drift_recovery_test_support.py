# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from unittest.mock import MagicMock

from orchestrator import workflow

from tests.documenting_drift_test_support import (
    DriftRunCapture,
    _run_with_git,
)
from tests.documenting_test_support import _branch
from tests.fakes import make_issue

DOCUMENTING = "documenting"
VALIDATING = "validating"
IN_REVIEW = "in_review"
ORIGINAL_BODY = "original body"
DEV_AGENT = "codex"
DEV_SESSION = "dev-sess"
PENDING_UNWIND_COMMENT_ID = 999
GIT_FAILURE_EXIT_CODE = 128
GIT_REV_LIST = "rev-list"
GIT_RESET = "reset"
GIT_HARD_RESET = "--hard"
GIT_CLEAN = "clean"
GIT_CLEAN_FLAGS = "-fd"
RUN_AGENT = "run_agent"
PUSH_BRANCH = "_push_branch"
AWAITING_HUMAN = "awaiting_human"
PARK_REASON = "park_reason"
PARK_RESET_FAILED = "worktree_reset_failed"
REVIEW_ROUND = "review_round"
DRIFT_UNWIND_PENDING = "docs_drift_unwind_pending"


def _failure_git_mock(failure_step: str) -> MagicMock:
    probe_success = MagicMock(returncode=0, stdout="0\t1\n", stderr="")
    reset_success = MagicMock(returncode=0, stdout="", stderr="")
    failure = MagicMock(
        returncode=GIT_FAILURE_EXIT_CODE,
        stdout="",
        stderr="fatal: simulated failure",
    )
    if failure_step == GIT_REV_LIST:
        return MagicMock(return_value=failure)
    if failure_step == GIT_RESET:
        return MagicMock(side_effect=[probe_success, failure])
    return MagicMock(side_effect=[probe_success, reset_success, failure])


def _run_drift_failure(
    case,
    github,
    issue,
    failure_step: str,
) -> DriftRunCapture:
    return _run_with_git(
        case,
        github,
        issue,
        _failure_git_mock(failure_step),
    )


def _assert_reset_failure_park(
    case,
    capture: DriftRunCapture,
    github,
) -> None:
    case.assertNotIn((case.issue_number, VALIDATING), github.label_history)
    case.assertNotIn((case.issue_number, IN_REVIEW), github.label_history)
    capture.mocks[RUN_AGENT].assert_not_called()
    capture.mocks[PUSH_BRANCH].assert_not_called()
    _assert_pending_state(case, github, review_round=0)


def _pending_state(case, issue, parked: bool) -> dict:
    state = {
        "review_round": 0,
        "docs_drift_unwind_pending": True,
        "user_content_hash": workflow._compute_user_content_hash(issue, set()),
        "pr_number": case.pr_number,
        "branch": _branch(case.issue_number),
        "dev_agent": DEV_AGENT,
        "dev_session_id": DEV_SESSION,
    }
    if parked:
        state.update(
            {
                "awaiting_human": True,
                "park_reason": PARK_RESET_FAILED,
                "last_action_comment_id": PENDING_UNWIND_COMMENT_ID,
            },
        )
    return state


def _seed_pending_unwind(case, *, parked: bool):
    original_issue = make_issue(
        case.issue_number,
        label=DOCUMENTING,
        body=ORIGINAL_BODY,
    )
    seed_options = {
        "docs_drift_unwind_pending": True,
        "user_content_hash": workflow._compute_user_content_hash(
            original_issue,
            set(),
        ),
    }
    if parked:
        seed_options.update(
            {
                "awaiting_human": True,
                "park_reason": PARK_RESET_FAILED,
                "last_action_comment_id": PENDING_UNWIND_COMMENT_ID,
            },
        )
    github, issue = case._seeded(**seed_options)
    issue.body = ORIGINAL_BODY
    github.seed_state(case.issue_number, **_pending_state(case, issue, parked))
    return github, issue


def _assert_silent_pending(
    case,
    capture: DriftRunCapture,
    github,
) -> None:
    capture.mocks["_authed_fetch"].assert_not_called()
    capture.git_hardened.assert_not_called()
    capture.mocks[RUN_AGENT].assert_not_called()
    capture.mocks[PUSH_BRANCH].assert_not_called()
    case.assertEqual(github.posted_comments, [])
    case.assertEqual(github.posted_pr_comments, [])
    case.assertNotIn((case.issue_number, VALIDATING), github.label_history)
    case.assertNotIn((case.issue_number, IN_REVIEW), github.label_history)


def _assert_pending_state(
    case,
    github,
    *,
    review_round: int | None = None,
) -> None:
    state = github.pinned_data(case.issue_number)
    case.assertTrue(state.get(DRIFT_UNWIND_PENDING))
    case.assertTrue(state.get(AWAITING_HUMAN))
    case.assertEqual(state.get(PARK_REASON), PARK_RESET_FAILED)
    if review_round is not None:
        case.assertEqual(state.get(REVIEW_ROUND), review_round)
