# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import config
from orchestrator.worktrees import VerifyResult

from tests import validating_verify_test_support as verify_support
from tests.workflow_helpers import (
    LABEL_DOCUMENTING,
    LABEL_IN_REVIEW,
    REVIEW_APPROVED_MESSAGE,
    REVIEW_CHANGES_REQUESTED_MESSAGE,
    _agent,
)

ISSUE = 7
DEV_SESSION = "dev-sess"
REVIEW_SHA = "rev-sha"
VERIFY_PYTEST = "pytest -q"
VERIFY_FAILED = "failed"
VERIFY_HEAD_CHANGED = "head_changed"
VERIFY_DIRTY = "dirty"
PARK_VERIFY_FAILED = "verify_failed"
PARK_VERIFY_TIMEOUT = "verify_timeout"
PARK_VERIFY_HEAD_CHANGED = "verify_head_changed"
PARK_VERIFY_DIRTY = "verify_dirty"
RUN_VERIFY_COMMANDS = "_run_verify_commands"
AWAITING_HUMAN = "awaiting_human"
PARK_REASON = "park_reason"
VERIFY_COMMANDS_SETTING = "VERIFY_COMMANDS"


class HandleValidatingVerifyRefusalTest(
    unittest.TestCase,
    verify_support.VerifyGateFixtureMixin,
):
    """Park when verification mutates HEAD, dirties the tree, or is premature."""

    def test_verify_head_changed_parks(self) -> None:
        # End-to-end: a verify command that moved HEAD must NOT flow
        # through to `in_review` -- otherwise squash-on-approval would
        # push the unreviewed commit. The handler parks the issue with a
        # distinct `verify_head_changed` reason so the operator can
        # adjudicate whether the auto-commit belongs in the PR.
        gh, issue = self._seeded()
        with patch.object(config, VERIFY_COMMANDS_SETTING, ("sh -c 'git commit -am autofix'",)):
            self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEW_SHA,),
                verify_result=VerifyResult(
                    status=VERIFY_HEAD_CHANGED,
                    command="sh -c 'git commit -am autofix'",
                    exit_code=0,
                    output="",
                    head_before="aaaa1111",
                    head_after="bbbb2222",
                ),
            )

        state = gh.pinned_data(ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_VERIFY_HEAD_CHANGED)
        # No in_review / documenting handoff and no approval / squash
        # side effects.
        self.assertNotIn((ISSUE, LABEL_IN_REVIEW), gh.label_history)
        self.assertNotIn((ISSUE, LABEL_DOCUMENTING), gh.label_history)
        self.assertFalse(
            any(
                ":white_check_mark:" in body
                for _, body in gh.posted_pr_comments
            )
        )
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("moved HEAD", last_comment)
        # Short SHAs are surfaced so the operator can identify the commit.
        self.assertIn("aaaa1111", last_comment)
        self.assertIn("bbbb2222", last_comment)

    def test_verify_dirty_worktree_parks(self) -> None:
        gh, issue = self._seeded()
        run = VerifyResult(
            status=VERIFY_DIRTY,
            command=VERIFY_PYTEST,
            exit_code=0,
            dirty_files=("build/artifact.bin", "tests/cache"),
        )
        with patch.object(config, VERIFY_COMMANDS_SETTING, (VERIFY_PYTEST,)):
            self._run_validating(
                gh,
                issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=(REVIEW_SHA,),
                verify_result=run,
            )

        state = gh.pinned_data(ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_VERIFY_DIRTY)
        self.assertNotIn((ISSUE, LABEL_IN_REVIEW), gh.label_history)
        self.assertNotIn((ISSUE, LABEL_DOCUMENTING), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("build/artifact.bin", last_comment)

    def test_changes_requested_does_not_run_verify(self) -> None:
        gh, issue = self._seeded()
        # The verify mock should not be called -- assert by setting a
        # failing result that would otherwise park the issue.
        with patch.object(config, VERIFY_COMMANDS_SETTING, (VERIFY_PYTEST,)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=[
                    _agent(
                        session_id="rev-sess",
                        last_message=REVIEW_CHANGES_REQUESTED_MESSAGE,
                    ),
                    _agent(session_id=DEV_SESSION, last_message="fixed"),
                ],
                dirty_files=(),
                push_branch=True,
                head_shas=["aaa", "bbb"],
                verify_result=VerifyResult(
                    status=VERIFY_FAILED,
                    command=VERIFY_PYTEST,
                    exit_code=1,
                    output="bad",
                ),
            )

        mocks[RUN_VERIFY_COMMANDS].assert_not_called()
        # Standard CHANGES_REQUESTED handling: PR review comment + dev resume.
        self.assertEqual(mocks["run_agent"].call_count, 2)
        self.assertEqual(gh.pinned_data(ISSUE).get("review_round"), 1)
        state = gh.pinned_data(ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))

    def test_unknown_verdict_does_not_run_verify(self) -> None:
        gh, issue = self._seeded()
        verify_fail = VerifyResult(
            status=VERIFY_FAILED,
            command=VERIFY_PYTEST,
            exit_code=1,
            output="bad",
        )
        with patch.object(config, VERIFY_COMMANDS_SETTING, (VERIFY_PYTEST,)):
            mocks = self._run_validating(
                gh,
                issue,
                run_agent=_agent(
                    last_message="I'm not sure what to think.",
                ),
                verify_result=verify_fail,
            )

        mocks[RUN_VERIFY_COMMANDS].assert_not_called()
        state = gh.pinned_data(ISSUE)
        # Park comes from the unknown-verdict path, NOT the verify gate;
        # confirm by checking the comment text (the unknown-verdict park
        # does not persist `park_reason` to pinned state for the
        # non-silent-crash case).
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertNotIn(
            state.get(PARK_REASON),
            (PARK_VERIFY_FAILED, PARK_VERIFY_TIMEOUT, PARK_VERIFY_DIRTY),
        )
        self.assertIn("did not emit a VERDICT line", gh.posted_comments[-1][1])
