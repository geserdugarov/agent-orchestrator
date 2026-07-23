# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import config, workflow

from tests.decomposition_test_support import (
    _comment_with_marker,
    _comments_for_issue,
    _labels_for_issue,
)
from tests.fakes import (
    FakeGitHubClient,
    make_issue,
)
from tests.workflow_helpers import (
    KEY_PARENT_NUMBER,
)
from tests.workflow_helpers import (
    LABEL_BLOCKED,
    LABEL_DECOMPOSING,
    LABEL_READY,
    LABEL_UMBRELLA,
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


class HandleDecomposingDecisionTest(
    unittest.TestCase,
    _DecomposingWorkflowMixin,
):
    """The decomposer drives the (no-label / `decomposing`) -> ready/blocked
    transitions. Single decision routes the parent to `ready`; split creates
    children with `ready`/`blocked` labels and parks the parent on `blocked`.
    Malformed or absent manifests park awaiting human.
    """

    def test_pickup_routes_to_decomposing(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(PICKUP_ISSUE_NUMBER)
        gh.add_issue(issue)
        manifest = _manifest('{"decision": "single", "rationale": "trivial"}')

        with patch.object(config, CONFIG_DECOMPOSE, True):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
            )

        # First label flip is to decomposing; the single-decision path then
        # flips it to ready on the same tick.
        self.assertEqual(
            gh.label_history[0],
            (PICKUP_ISSUE_NUMBER, LABEL_DECOMPOSING),
        )
        self.assertIn((PICKUP_ISSUE_NUMBER, LABEL_READY), gh.label_history)
        self.assertIn(
            LABEL_DECOMPOSING,
            "\n".join(_comments_for_issue(gh, PICKUP_ISSUE_NUMBER)),
        )

    def test_decompose_decision_single_flips_to_ready(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(SINGLE_DECISION_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest('{"decision": "single", "rationale": "fits in one context"}')

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
        )

        self.assertIn(
            (SINGLE_DECISION_ISSUE_NUMBER, LABEL_READY),
            gh.label_history,
        )
        # No children created.
        self.assertEqual(gh.created_child_issues, [])
        state = gh.pinned_data(SINGLE_DECISION_ISSUE_NUMBER)
        self.assertEqual(state.get(KEY_DECOMPOSER_AGENT), config.DECOMPOSE_AGENT)
        self.assertEqual(state.get(KEY_DECOMPOSER_SESSION_ID), DECOMPOSER_SESSION)
        self.assertIn("decomposed_at", state)
        # Rationale surfaced in a comment.
        self.assertTrue(
            any(
                "fits in one context" in body
                for body in _comments_for_issue(
                    gh,
                    SINGLE_DECISION_ISSUE_NUMBER,
                )
            )
        )

    def test_single_hands_off_collected_context(self) -> None:
        # A single decision must carry the decomposer's gathered context
        # (affected files + notes) into the issue thread so the implementer
        # inherits it via `_recent_comments_text` at spawn.
        gh = FakeGitHubClient()
        issue = make_issue(CONTEXT_HANDOFF_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "single", "rationale": "fits", '
            '"affected_files": ["orchestrator/config.py", "tests/fakes.py"], '
            '"notes": "Bump the default and cover it in fakes."}'
        )

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
        )

        self.assertIn(
            (CONTEXT_HANDOFF_ISSUE_NUMBER, LABEL_READY),
            gh.label_history,
        )
        context_comment = _comment_with_marker(
            gh,
            CONTEXT_HANDOFF_ISSUE_NUMBER,
            ":mag:",
        )
        self.assertIn("orchestrator/config.py", context_comment)
        self.assertIn("tests/fakes.py", context_comment)
        self.assertIn("Bump the default and cover it in fakes.", context_comment)

    def test_split_decision_creates_children(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(SPLIT_DECISION_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "rationale": "two pieces", "children": ['
            '{"title": "Add status subcommand", "body": "implement status", '
            '"depends_on": []},'
            '{"title": "Add pause subcommand", "body": "implement pause", '
            '"depends_on": []}'
            "]}"
        )

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
        )

        # Parent is now blocked; both children created with `ready`.
        self.assertIn(
            (SPLIT_DECISION_ISSUE_NUMBER, LABEL_BLOCKED),
            gh.label_history,
        )
        self.assertEqual(len(gh.created_child_issues), 2)
        for child in gh.created_child_issues:
            self.assertEqual(
                [label.name for label in child.labels],
                [LABEL_READY],
            )
            self.assertIn(f"Parent: #{SPLIT_DECISION_ISSUE_NUMBER}", child.body)

        self.assertEqual(
            gh.pinned_data(SPLIT_DECISION_ISSUE_NUMBER).get(KEY_CHILDREN),
            list(
                map(
                    lambda created_child: created_child.number,
                    gh.created_child_issues,
                )
            ),
        )
        # No deps -> dep_graph not persisted.
        self.assertNotIn(
            "dep_graph",
            gh.pinned_data(SPLIT_DECISION_ISSUE_NUMBER),
        )
        # Summary comment lists both child numbers.
        last_comment = _comment_with_marker(
            gh,
            SPLIT_DECISION_ISSUE_NUMBER,
            ":bookmark_tabs:",
        )
        for child in gh.created_child_issues:
            self.assertIn(f"#{child.number}", last_comment)

    def test_umbrella_split_marks_parent(self) -> None:
        # `umbrella: true` on a split decision means the parent has no
        # implementation work of its own; instead of `blocked` (which
        # would re-enter implementation after children resolve), it gets
        # the `umbrella` label and `_handle_umbrella` will close it once
        # every child reaches `done`.
        gh = FakeGitHubClient()
        issue = make_issue(UMBRELLA_SPLIT_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "umbrella": true, '
            '"rationale": "parent is just a tracker", "children": ['
            '{"title": "A", "body": "a"},'
            '{"title": "B", "body": "b"}'
            "]}"
        )

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
        )

        # Parent reached `umbrella`, NOT `blocked`.
        self.assertIn(
            LABEL_UMBRELLA,
            _labels_for_issue(gh, UMBRELLA_SPLIT_ISSUE_NUMBER),
        )
        self.assertNotIn(
            LABEL_BLOCKED,
            _labels_for_issue(gh, UMBRELLA_SPLIT_ISSUE_NUMBER),
        )
        # Children created normally, with no-dep activation -> `ready`.
        self.assertEqual(len(gh.created_child_issues), 2)
        for child in gh.created_child_issues:
            self.assertEqual(
                [label.name for label in child.labels],
                [LABEL_READY],
            )
        # `umbrella` flag persisted on parent state so the
        # half-finished recovery path can read it back after a SIGKILL.
        self.assertTrue(gh.pinned_data(UMBRELLA_SPLIT_ISSUE_NUMBER).get(KEY_UMBRELLA))
        # Summary comment mentions umbrella so a human glancing at the
        # thread sees what label the parent landed on.
        last_comment = _comment_with_marker(
            gh,
            UMBRELLA_SPLIT_ISSUE_NUMBER,
            ":bookmark_tabs:",
        )
        self.assertIn(LABEL_UMBRELLA, last_comment)

    def test_non_umbrella_split_defaults_blocked(
        self,
    ) -> None:
        # Default for the umbrella flag is False -- a split manifest
        # without `umbrella` must still go through `blocked` so the
        # parent re-enters implementation after children resolve, the
        # legacy behavior.
        gh = FakeGitHubClient()
        issue = make_issue(
            NON_UMBRELLA_SPLIT_ISSUE_NUMBER,
            label=LABEL_DECOMPOSING,
        )
        gh.add_issue(issue)
        manifest = _manifest('{"decision": "split", "children": [{"title": "A", "body": "a"}]}')

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
        )

        self.assertIn(
            LABEL_BLOCKED,
            _labels_for_issue(gh, NON_UMBRELLA_SPLIT_ISSUE_NUMBER),
        )
        self.assertNotIn(
            LABEL_UMBRELLA,
            _labels_for_issue(gh, NON_UMBRELLA_SPLIT_ISSUE_NUMBER),
        )
        # State records umbrella=False explicitly so a stale True from a
        # prior aborted decomposition cannot survive into recovery.
        self.assertEqual(
            gh.pinned_data(NON_UMBRELLA_SPLIT_ISSUE_NUMBER).get(KEY_UMBRELLA),
            False,
        )

    def test_split_with_deps_persists_graph(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(DEPENDENCY_SPLIT_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "First", "body": "do first", "depends_on": []},'
            '{"title": "Second", "body": "needs first", "depends_on": [0]}'
            "]}"
        )

        self._run_decomposing(
            gh,
            issue,
            run_agent=_agent(session_id=DECOMPOSER_SESSION, last_message=manifest),
        )

        children = gh.created_child_issues
        self.assertEqual(len(children), 2)
        # child[0] has no deps -> ready; child[1] depends on [0] -> blocked.
        self.assertEqual(
            [label.name for label in children[0].labels],
            [LABEL_READY],
        )
        self.assertEqual(
            [label.name for label in children[1].labels],
            [LABEL_BLOCKED],
        )

        self.assertEqual(
            gh.pinned_data(DEPENDENCY_SPLIT_ISSUE_NUMBER).get("dep_graph"),
            {"1": [0]},
        )
        # Each child's pinned state records the parent so the polling
        # loop's blocked-issue dispatch can recognize it as a child
        # rather than as an unattributed `blocked` parent.
        for child in children:
            self.assertEqual(
                gh.pinned_data(child.number).get(KEY_PARENT_NUMBER),
                DEPENDENCY_SPLIT_ISSUE_NUMBER,
            )
