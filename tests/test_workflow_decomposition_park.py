# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.decomposition_test_support import _run_with_logs
from tests.fakes import (
    FakeGitHubClient,
    make_issue,
)
from tests.workflow_helpers import (
    KEY_AWAITING_HUMAN,
)
from tests.workflow_helpers import (
    LABEL_DECOMPOSING,
    LABEL_READY,
)
from tests.workflow_helpers import (
    _agent,
    _manifest,
)

from tests.decomposition_decomposing_test_support import (
    _DecomposingWorkflowMixin,
)

KEY_DECOMPOSER_AGENT = "decomposer_agent"
KEY_DECOMPOSER_SESSION_ID = "decomposer_session_id"
KEY_CHILDREN = "children"
KEY_UMBRELLA = "umbrella"
CLEANUP_DECOMPOSE_WORKTREE = "_cleanup_decompose_worktree"
RUN_AGENT = "run_agent"
CONFIG_DECOMPOSE = "DECOMPOSE"
DECOMPOSER_SESSION = "dec-sess"
DEV_SESSION = "dev-sess"
TRUSTED_AUTHOR = "alice"
CREATED_AT = "2026-05-03T00:00:00+00:00"
PICKUP_ISSUE_NUMBER = 10
SINGLE_DECISION_ISSUE_NUMBER = 11
CONTEXT_HANDOFF_ISSUE_NUMBER = 73
SPLIT_DECISION_ISSUE_NUMBER = 12
UMBRELLA_SPLIT_ISSUE_NUMBER = 50
NON_UMBRELLA_SPLIT_ISSUE_NUMBER = 51
DEPENDENCY_SPLIT_ISSUE_NUMBER = 13
COMMITS_PARK_ISSUE_NUMBER = 40
DIRTY_PARK_ISSUE_NUMBER = 41
MALFORMED_MANIFEST_ISSUE_NUMBER = 14
QUESTION_PARK_ISSUE_NUMBER = 15
SILENT_FAILURE_ISSUE_NUMBER = 115
RESUME_ISSUE_NUMBER = 16
FILTERED_RESUME_ISSUE_NUMBER = 17
RETRY_CAP_ISSUE_NUMBER = 18
HUMAN_REPLY_COMMENT_ID = 1100
OUTSIDER_REPLY_COMMENT_ID = 1101
PRIOR_ACTION_COMMENT_ID = 900
DISABLED_PICKUP_ISSUE_NUMBER = 19
DISABLED_LABELED_ISSUE_NUMBER = 20
DISABLED_RATCHET_ISSUE_NUMBER = 21
RATCHET_FIRST_COMMENT_ID = 950
RATCHET_LATEST_COMMENT_ID = 960
DISABLED_MONOTONIC_ISSUE_NUMBER = 22
OLDER_COMMENT_ID = 500
PRESERVED_HIGH_WATERMARK = 10000
HALF_COMPLETE_DISABLED_PARENT_NUMBER = 50
RECOVERY_CHILD_NUMBERS = (101, 102)
PERSISTENCE_ISSUE_NUMBER = 80
COMPLETE_RECOVERY_PARENT_NUMBER = 50
AWAITING_RECOVERY_PARENT_NUMBER = 51
AWAITING_RECOVERY_CHILD_NUMBER = 201
PARTIAL_RECOVERY_PARENT_NUMBER = 52
ORPHAN_RECOVERY_PARENT_NUMBER = 53
ORPHAN_REPAIR_PARENT_NUMBER = 60
HEALTHY_CHILD_NUMBER = 601
ORPHAN_CHILD_NUMBER = 602
STALE_PARK_COMMENT_ID = 999
EXPECTED_COUNT_ORDER_ISSUE_NUMBER = 82
CHILD_STATE_ORDER_ISSUE_NUMBER = 83
WORKTREE_ISSUE_NUMBER = 70
DIRTY_WORKTREE_ISSUE_NUMBER = 71
AWAITING_WORKTREE_ISSUE_NUMBER = 73
NON_STRING_RATIONALE_ISSUE_NUMBER = 72
FRESH_USAGE_ISSUE_NUMBER = 620
RESUMED_USAGE_ISSUE_NUMBER = 621
NO_COMMENT_USAGE_ISSUE_NUMBER = 622
INTERRUPTED_USAGE_ISSUE_NUMBER = 623
DIRTY_INTERRUPTED_USAGE_ISSUE_NUMBER = 624

SINGLE_MANIFEST_PAYLOAD = '{"decision": "single", "rationale": "fits"}'
SPLIT_MANIFEST = _manifest(
    '{"decision": "split", "children": [{"title": "A", "body": "a"},{"title": "B", "body": "b"}]}'
)
READ_ONLY_FRAGMENT = "read-only"
IMPLEMENTED_MESSAGE = "implemented"


class HandleDecomposingParkTest(
    unittest.TestCase,
    _DecomposingWorkflowMixin,
):
    def test_commits_left_by_decomposer_park(self) -> None:
        # The decomposer is supposed to be read-only. If it commits in the
        # parent's worktree, the implementer recovery path in
        # `_handle_implementing` would later see `_has_new_commits` -> True
        # and push decomposer-authored work as if it were implementation.
        # Defensive park is the surface that catches this.
        gh = FakeGitHubClient()
        issue = make_issue(COMMITS_PARK_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest(SINGLE_MANIFEST_PAYLOAD)

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
            has_new_commits=True,
        )

        state = gh.pinned_data(COMMITS_PARK_ISSUE_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        # Did NOT advance to ready -- the operator must clean up first.
        self.assertNotIn(
            (COMMITS_PARK_ISSUE_NUMBER, LABEL_READY),
            gh.label_history,
        )
        last_comment = gh.posted_comments[-1][1]
        self.assertIn(READ_ONLY_FRAGMENT, last_comment)

    def test_dirty_files_left_by_decomposer_park(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(DIRTY_PARK_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest(SINGLE_MANIFEST_PAYLOAD)

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
            dirty_files=("foo.py",),
        )

        state = gh.pinned_data(DIRTY_PARK_ISSUE_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        self.assertNotIn(
            (DIRTY_PARK_ISSUE_NUMBER, LABEL_READY),
            gh.label_history,
        )
        last_comment = gh.posted_comments[-1][1]
        self.assertIn(READ_ONLY_FRAGMENT, last_comment)

    def test_decompose_malformed_manifest_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(MALFORMED_MANIFEST_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        bad = _manifest("{not really json")

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=bad),
        )

        state = gh.pinned_data(MALFORMED_MANIFEST_ISSUE_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("manifest invalid", last_comment)
        # Last decomposer message quoted into the HITL ping so the human
        # can see what the agent actually emitted.
        self.assertIn("not really json", last_comment)
        # Decomposer session recorded so the resume on human reply uses
        # the right backend even if DECOMPOSE_AGENT flips between ticks.
        self.assertEqual(state.get(KEY_DECOMPOSER_SESSION_ID), DECOMPOSER_SESSION)

    def test_decompose_no_manifest_question_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(QUESTION_PARK_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(
                session_id=DECOMPOSER_SESSION,
                last_message="Should the new commands accept a --json flag?",
                stderr="benign warning",
            ),
        )

        state = gh.pinned_data(QUESTION_PARK_ISSUE_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("needs your input", last_comment)
        self.assertIn("--json flag", last_comment)
        # Real decomposer text -> no stderr block (would be noise).
        self.assertNotIn("Decomposer stderr", last_comment)

    def test_decompose_silent_failure_surfaces_stderr(self) -> None:
        # No manifest AND no final message: the decomposer subprocess
        # produced literally nothing. Surface its stderr/exit_code in
        # the park so the operator can tell a CF / quota / auth failure
        # apart from a model that just had no opinion.
        gh = FakeGitHubClient()
        issue = make_issue(SILENT_FAILURE_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)

        log_lines = _run_with_logs(
            self,
            "orchestrator.workflow",
            "WARNING",
            lambda: self._run_decomposing(
                gh,
                issue,
                run_agent=_agent(
                    session_id=DECOMPOSER_SESSION,
                    last_message="",
                    stderr="rate limit exceeded; retry after 60s",
                    exit_code=3,
                ),
            ),
        )

        last_comment = gh.posted_comments[-1][1]
        self.assertIn("(decomposer produced no final message)", last_comment)
        self.assertIn("_Decomposer stderr (last 1KB):_", last_comment)
        self.assertIn("rate limit exceeded", last_comment)
        self.assertIn("_Decomposer exit code:_ 3", last_comment)
        self.assertTrue(
            any(
                "decomposer produced no final message" in log_line and "exit_code=3" in log_line
                for log_line in log_lines
            )
        )
