# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    KEY_AWAITING_HUMAN,
    KEY_ISSUE_AGENT_RUNS,
    KEY_ISSUE_TOTAL_TOKENS,
)
from tests.workflow_helpers import (
    LABEL_DECOMPOSING,
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


class DecomposerRunUsageAccumulationTest(
    unittest.TestCase,
    _DecomposingWorkflowMixin,
):
    """`_handle_decomposing` folds each real decomposer exit into the
    per-issue usage counters, at both the fresh-spawn and awaiting-human
    resume sites, and leaves them unpersisted when the run was interrupted
    (empty stdout parses to a `no-usage` metric: a counted run with zero
    tokens).
    """

    def test_fresh_run_persists_one_run(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(FRESH_USAGE_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest(SINGLE_MANIFEST_PAYLOAD)

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
        )

        state = gh.pinned_data(FRESH_USAGE_ISSUE_NUMBER)
        self.assertEqual(state[KEY_ISSUE_AGENT_RUNS], 1)
        self.assertEqual(state[KEY_ISSUE_TOTAL_TOKENS], 0)
        self.assertEqual(state["issue_cost_sources"], ["no-usage"])

    def test_resume_counts_one_exit(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(RESUMED_USAGE_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        issue.comments.append(
            FakeComment(
                id=HUMAN_REPLY_COMMENT_ID,
                body="please split",
                user=FakeUser(TRUSTED_AUTHOR),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            RESUMED_USAGE_ISSUE_NUMBER,
            awaiting_human=True,
            last_action_comment_id=PRIOR_ACTION_COMMENT_ID,
            decomposer_agent=BACKEND_CLAUDE,
            decomposer_session_id=DECOMPOSER_SESSION,
        )

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(
                session_id=DECOMPOSER_SESSION,
                last_message=_manifest(SINGLE_MANIFEST_PAYLOAD),
            ),
        )

        # Exactly one real resume exit folded.
        self.assertEqual(
            gh.pinned_data(RESUMED_USAGE_ISSUE_NUMBER)[KEY_ISSUE_AGENT_RUNS],
            1,
        )

    def test_no_comment_resume_keeps_counters(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(NO_COMMENT_USAGE_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        gh.seed_state(
            NO_COMMENT_USAGE_ISSUE_NUMBER,
            awaiting_human=True,
            last_action_comment_id=PRIOR_ACTION_COMMENT_ID,
            decomposer_agent=BACKEND_CLAUDE,
            decomposer_session_id=DECOMPOSER_SESSION,
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )

        mocks = self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(),
        )

        # No reply -> the resume returns before spawning, so no run is
        # counted and no counter key is created.
        mocks[RUN_AGENT].assert_not_called()
        state = gh.pinned_data(NO_COMMENT_USAGE_ISSUE_NUMBER)
        self.assertNotIn(KEY_ISSUE_AGENT_RUNS, state)
        self.assertNotIn(KEY_ISSUE_TOTAL_TOKENS, state)

    def test_interrupted_run_keeps_counters_clear(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(INTERRUPTED_USAGE_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        gh.seed_state(
            INTERRUPTED_USAGE_ISSUE_NUMBER,
            # Seed the drift baseline so `_detect_user_content_change` does
            # not itself write on first encounter -- this test asserts the
            # handler writes NOTHING once the run is interrupted.
            user_content_hash=workflow._compute_user_content_hash(issue, set()),
        )

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(
                session_id="",
                last_message="",
                exit_code=1,
                interrupted=True,
            ),
        )

        # A shutdown-killed decomposer returns before `write_pinned_state`,
        # so neither the folded counters nor a silent/invalid park reach
        # GitHub.
        state = gh.pinned_data(INTERRUPTED_USAGE_ISSUE_NUMBER)
        self.assertNotIn(KEY_ISSUE_AGENT_RUNS, state)
        self.assertNotIn(KEY_ISSUE_TOTAL_TOKENS, state)
        self.assertFalse(state.get(KEY_AWAITING_HUMAN))

    def test_dirty_interrupt_parks_without_counters(
        self,
    ) -> None:
        # An interrupted decomposer that nonetheless left changes in the
        # worktree must still hit the read-only dirty park -- the interrupted
        # guard sits AFTER that park precisely so a killed misbehaving run
        # does not slip through and lose the inspection worktree. That park
        # DOES write pinned state, so the usage fold must be skipped for the
        # interrupted run or a counter would persist despite the run being
        # killed.
        gh = FakeGitHubClient()
        issue = make_issue(
            DIRTY_INTERRUPTED_USAGE_ISSUE_NUMBER,
            label=LABEL_DECOMPOSING,
        )
        gh.add_issue(issue)

        mocks = self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(
                session_id=DECOMPOSER_SESSION,
                last_message="",
                interrupted=True,
            ),
            has_new_commits=True,
        )

        state = gh.pinned_data(DIRTY_INTERRUPTED_USAGE_ISSUE_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        self.assertIn(READ_ONLY_FRAGMENT, gh.posted_comments[-1][1])
        # Worktree kept for inspection (the dirty park's contract).
        mocks[CLEANUP_DECOMPOSE_WORKTREE].assert_not_called()
        # The park wrote pinned state, but the killed run's usage was NOT
        # folded, so no counter accrued.
        self.assertNotIn(KEY_ISSUE_AGENT_RUNS, state)
        self.assertNotIn(KEY_ISSUE_TOTAL_TOKENS, state)
