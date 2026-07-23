# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.decomposition_test_support import _comment_with_marker
from tests.fakes import (
    FakeGitHubClient,
    make_issue,
)
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    KEY_AWAITING_HUMAN,
)
from tests.workflow_helpers import (
    LABEL_DECOMPOSING,
    LABEL_READY,
    _TEST_SPEC,
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


class DecompositionWorktreeTest(
    unittest.TestCase,
    _DecomposingWorkflowMixin,
):
    def test_uses_separate_implementer_worktree(self) -> None:
        # The decomposer must NOT taint the implementer's per-issue branch.
        # If it shared `_ensure_worktree`, a `split` decision would leave
        # the local `orchestrator/geserdugarov__agent-orchestrator/issue-<n>` branch anchored at the
        # origin/main snapshot the decomposer saw, and the parent's
        # eventual implementer (after children merged to main) would
        # commit on a stale base.
        gh = FakeGitHubClient()
        issue = make_issue(WORKTREE_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest(SINGLE_MANIFEST_PAYLOAD)

        mocks = self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
        )

        mocks["_ensure_decompose_worktree"].assert_called_with(
            _TEST_SPEC,
            WORKTREE_ISSUE_NUMBER,
        )
        mocks["_ensure_worktree"].assert_not_called()
        # Cleanup runs at function exit so the next consumer of issue 70
        # (here _handle_ready -> _handle_implementing on the next tick)
        # starts from a fresh checkout.
        mocks[CLEANUP_DECOMPOSE_WORKTREE].assert_called_with(
            _TEST_SPEC,
            WORKTREE_ISSUE_NUMBER,
        )

    def test_decompose_skips_cleanup_on_dirty_park(self) -> None:
        # Operator inspection requires the decomposer's worktree to
        # outlive the dirty/commits park.
        gh = FakeGitHubClient()
        issue = make_issue(DIRTY_WORKTREE_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest(SINGLE_MANIFEST_PAYLOAD)

        mocks = self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
            has_new_commits=True,
        )

        self.assertTrue(gh.pinned_data(DIRTY_WORKTREE_ISSUE_NUMBER).get(KEY_AWAITING_HUMAN))
        mocks[CLEANUP_DECOMPOSE_WORKTREE].assert_not_called()

    def test_awaiting_human_skips_cleanup(self) -> None:
        # On the tick AFTER a dirty/commits park, awaiting_human is True
        # and no human reply has arrived yet. The handler must not clean
        # up the decomposer worktree -- the HITL message asks the operator
        # to inspect and reset it, and a subsequent-tick cleanup would
        # silently delete that state out from under them.
        gh = FakeGitHubClient()
        issue = make_issue(AWAITING_WORKTREE_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        gh.seed_state(
            AWAITING_WORKTREE_ISSUE_NUMBER,
            awaiting_human=True,
            last_action_comment_id=STALE_PARK_COMMENT_ID,
            decomposer_agent=BACKEND_CLAUDE,
            decomposer_session_id=DECOMPOSER_SESSION,
        )

        mocks = self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        mocks[CLEANUP_DECOMPOSE_WORKTREE].assert_not_called()

    def test_decompose_handles_non_string_rationale(self) -> None:
        # JSON-valid manifest with a non-string rationale (`[1,2,3]`,
        # `{}`, `42`) must not crash the handler at `.strip()` after
        # the agent already ran. Coerce to the placeholder.
        gh = FakeGitHubClient()
        issue = make_issue(
            NON_STRING_RATIONALE_ISSUE_NUMBER,
            label=LABEL_DECOMPOSING,
        )
        gh.add_issue(issue)
        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(
                session_id=DECOMPOSER_SESSION,
                last_message=_manifest('{"decision": "single", "rationale": [1, 2, 3]}'),
            ),
        )

        self.assertIn(
            (NON_STRING_RATIONALE_ISSUE_NUMBER, LABEL_READY),
            gh.label_history,
        )
        self.assertFalse(gh.pinned_data(NON_STRING_RATIONALE_ISSUE_NUMBER).get(KEY_AWAITING_HUMAN))
        rationale_comment = _comment_with_marker(
            gh,
            NON_STRING_RATIONALE_ISSUE_NUMBER,
            ":mag:",
        )
        self.assertIn("(no rationale provided)", rationale_comment)
