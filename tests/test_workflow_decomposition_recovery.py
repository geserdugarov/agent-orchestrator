# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.decomposition_test_support import _seed_blocked_children
from tests.fakes import (
    FakeGitHubClient,
    make_issue,
)
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    KEY_AWAITING_HUMAN,
    KEY_PARENT_NUMBER,
)
from tests.workflow_helpers import (
    LABEL_BLOCKED,
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


def _orphan_recovery_fixture():
    github = FakeGitHubClient()
    parent = make_issue(
        ORPHAN_REPAIR_PARENT_NUMBER,
        label=LABEL_DECOMPOSING,
    )
    github.add_issue(parent)
    for child_number in (HEALTHY_CHILD_NUMBER, ORPHAN_CHILD_NUMBER):
        github.add_issue(make_issue(child_number, label=LABEL_BLOCKED))
    github.seed_state(
        HEALTHY_CHILD_NUMBER,
        parent_number=ORPHAN_REPAIR_PARENT_NUMBER,
        created_at=CREATED_AT,
    )
    github.seed_state(
        ORPHAN_CHILD_NUMBER,
        awaiting_human=True,
        park_reason=None,
        last_action_comment_id=STALE_PARK_COMMENT_ID,
    )
    github.seed_state(
        ORPHAN_REPAIR_PARENT_NUMBER,
        children=[HEALTHY_CHILD_NUMBER, ORPHAN_CHILD_NUMBER],
        expected_children_count=2,
        decomposed_at=CREATED_AT,
        decomposer_agent=BACKEND_CLAUDE,
        decomposer_session_id=DECOMPOSER_SESSION,
    )
    return github, parent


class DecompositionRecoveryTest(
    unittest.TestCase,
    _DecomposingWorkflowMixin,
):
    def test_half_finished_recovery_flips_to_blocked(self) -> None:
        # Simulate: a prior tick created+persisted children but crashed
        # before flipping the parent label from `decomposing` to
        # `blocked`. The next tick must NOT re-spawn the decomposer
        # (would create duplicate children); it must finalize the parent
        # transition. The parent's `_handle_blocked` activates no-dep
        # children on a subsequent tick.
        gh = FakeGitHubClient()
        issue = make_issue(COMPLETE_RECOVERY_PARENT_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        # Children already exist on GitHub with `parent_number` seeded --
        # the crash happened AFTER both child seeds, between the parent's
        # last incremental write and the parent label flip.
        _seed_blocked_children(
            gh,
            COMPLETE_RECOVERY_PARENT_NUMBER,
            RECOVERY_CHILD_NUMBERS,
        )
        gh.seed_state(
            COMPLETE_RECOVERY_PARENT_NUMBER,
            children=list(RECOVERY_CHILD_NUMBERS),
            decomposed_at=CREATED_AT,
            decomposer_agent=BACKEND_CLAUDE,
            decomposer_session_id=DECOMPOSER_SESSION,
        )

        mocks = self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(),
        )

        # Decomposer was NOT respawned; no new children were created.
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        self.assertIn(
            (COMPLETE_RECOVERY_PARENT_NUMBER, LABEL_BLOCKED),
            gh.label_history,
        )
        # Children + decomposed_at preserved.
        self.assertEqual(
            gh.pinned_data(COMPLETE_RECOVERY_PARENT_NUMBER).get(KEY_CHILDREN),
            list(RECOVERY_CHILD_NUMBERS),
        )

    def test_half_complete_awaiting_human_holds(self) -> None:
        # If the prior tick parked awaiting_human after partial child
        # creation, the recovery must NOT silently flip the parent to
        # `blocked`; the human's intervention is still required.
        gh = FakeGitHubClient()
        issue = make_issue(AWAITING_RECOVERY_PARENT_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        gh.seed_state(
            AWAITING_RECOVERY_PARENT_NUMBER,
            children=[AWAITING_RECOVERY_CHILD_NUMBER],
            awaiting_human=True,
            decomposer_agent=BACKEND_CLAUDE,
            decomposer_session_id=DECOMPOSER_SESSION,
        )

        mocks = self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        # Label NOT flipped; human still owns it.
        self.assertNotIn(
            (AWAITING_RECOVERY_PARENT_NUMBER, LABEL_BLOCKED),
            gh.label_history,
        )
        self.assertTrue(gh.pinned_data(AWAITING_RECOVERY_PARENT_NUMBER).get(KEY_AWAITING_HUMAN))

    def test_partial_children_recovery_parks(self) -> None:
        # SIGKILL between iterations leaves a partial `children` list
        # that the half-finished recovery used to silently treat as
        # complete -- stranding any un-created dependents and never
        # creating the missing children. With `expected_children_count`
        # persisted up-front, the recovery distinguishes partial from
        # complete and parks awaiting human.
        gh = FakeGitHubClient()
        issue = make_issue(PARTIAL_RECOVERY_PARENT_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        gh.seed_state(
            PARTIAL_RECOVERY_PARENT_NUMBER,
            children=[RECOVERY_CHILD_NUMBERS[0]],
            expected_children_count=3,
            decomposed_at=CREATED_AT,
            decomposer_agent=BACKEND_CLAUDE,
            decomposer_session_id=DECOMPOSER_SESSION,
        )

        mocks = self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        # Parked, not finalized to blocked.
        self.assertNotIn(
            (PARTIAL_RECOVERY_PARENT_NUMBER, LABEL_BLOCKED),
            gh.label_history,
        )
        state = gh.pinned_data(PARTIAL_RECOVERY_PARENT_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("crashed mid-way", last_comment)
        self.assertIn("1 of 3", last_comment)

    def test_orphan_recovery_parks_without_children(
        self,
    ) -> None:
        # SIGKILL between `create_child_issue` returning and the parent's
        # incremental `children` write leaves the parent with
        # `expected_children_count` set but zero recorded children, while
        # an orphan child issue exists on GitHub. The previous recovery
        # branch only fired when `state.get("children")` was truthy, so
        # this case fell through, the decomposer was respawned, and a
        # different manifest produced duplicate child issues alongside
        # the orphan.
        gh = FakeGitHubClient()
        issue = make_issue(ORPHAN_RECOVERY_PARENT_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        gh.seed_state(
            ORPHAN_RECOVERY_PARENT_NUMBER,
            expected_children_count=2,
            decomposer_agent=BACKEND_CLAUDE,
            decomposer_session_id=DECOMPOSER_SESSION,
        )

        mocks = self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        self.assertNotIn(
            (ORPHAN_RECOVERY_PARENT_NUMBER, LABEL_BLOCKED),
            gh.label_history,
        )
        state = gh.pinned_data(ORPHAN_RECOVERY_PARENT_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("crashed mid-way", last_comment)
        self.assertIn("0 of 2", last_comment)

    def test_orphan_recovery_seeds_parent_number(self) -> None:
        # SIGKILL between the parent's child-record write and the child's
        # pinned-state seed for the LAST child satisfies
        # `len(children) == expected_children_count` but leaves that child
        # orphaned (label=blocked, no `parent_number`). A prior
        # `_handle_blocked` tick may have already parked the orphan as
        # "manual relabel suspected" with `awaiting_human=True`. Without
        # repair, recovery finalizes the parent to `blocked`, the parent's
        # walk later flips the orphan to `ready`, and
        # `_handle_implementing` reads the stale park and sits waiting on
        # a human reply that never comes.
        gh, parent = _orphan_recovery_fixture()

        mocks = self._run_decomposing(
            gh,
            parent,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        self.assertIn(
            (ORPHAN_REPAIR_PARENT_NUMBER, LABEL_BLOCKED),
            gh.label_history,
        )
        # Orphan got parent_number seeded and stale park cleared.
        orphan_state = gh.pinned_data(ORPHAN_CHILD_NUMBER)
        self.assertEqual(
            orphan_state.get(KEY_PARENT_NUMBER),
            ORPHAN_REPAIR_PARENT_NUMBER,
        )
        self.assertFalse(orphan_state.get(KEY_AWAITING_HUMAN))
        # Healthy child untouched.
        self.assertEqual(
            gh.pinned_data(HEALTHY_CHILD_NUMBER).get(KEY_PARENT_NUMBER),
            ORPHAN_REPAIR_PARENT_NUMBER,
        )
