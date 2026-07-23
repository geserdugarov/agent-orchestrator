# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import config, workflow

from tests.decomposition_test_support import (
    _comments_for_issue,
    _labels_for_issue,
    _seed_blocked_children,
)
from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    BACKEND_CLAUDE,
    KEY_AWAITING_HUMAN,
    KEY_LAST_ACTION_COMMENT_ID,
)
from tests.workflow_helpers import (
    LABEL_BLOCKED,
    LABEL_DECOMPOSING,
    LABEL_IMPLEMENTING,
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


def _disabled_labeled_fixture():
    github = FakeGitHubClient()
    issue = make_issue(
        DISABLED_LABELED_ISSUE_NUMBER,
        label=LABEL_DECOMPOSING,
    )
    github.add_issue(issue)
    github.seed_state(
        DISABLED_LABELED_ISSUE_NUMBER,
        awaiting_human=True,
        park_reason="(test) decomposer asked a question",
        decomposer_agent=BACKEND_CLAUDE,
        decomposer_session_id=DECOMPOSER_SESSION,
        last_action_comment_id=PRIOR_ACTION_COMMENT_ID,
        pickup_comment_id=100,
    )
    return github, issue


class DecompositionDisabledTest(
    unittest.TestCase,
    _DecomposingWorkflowMixin,
):
    def test_off_falls_back_to_legacy_pickup(self) -> None:
        # End-to-end: with DECOMPOSE=off, the unlabeled issue must skip
        # the decomposer entirely and route straight to implementing
        # exactly as the bootstrap-milestone path did. No `decomposing`
        # label and no decomposer pinned-state keys are written.
        gh = FakeGitHubClient()
        issue = make_issue(DISABLED_PICKUP_ISSUE_NUMBER)
        gh.add_issue(issue)

        with patch.object(config, CONFIG_DECOMPOSE, False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id=DEV_SESSION, last_message="done"),
                has_new_commits=[False, True],
                push_branch=True,
            )

        self.assertNotIn(
            LABEL_DECOMPOSING,
            [lbl for _, lbl in gh.label_history],
        )
        self.assertIn(
            (DISABLED_PICKUP_ISSUE_NUMBER, LABEL_IMPLEMENTING),
            gh.label_history,
        )
        self.assertEqual(gh.created_child_issues, [])
        state = gh.pinned_data(DISABLED_PICKUP_ISSUE_NUMBER)
        self.assertNotIn(KEY_DECOMPOSER_AGENT, state)
        self.assertNotIn(KEY_DECOMPOSER_SESSION_ID, state)

    def test_off_routes_label_to_implementing(
        self,
    ) -> None:
        # The DECOMPOSE kill switch must apply to issues that were
        # already labeled `decomposing` (or parked there awaiting a
        # human) when the operator restarts with the flag off.
        # Without this, `_process_issue` still calls `_handle_decomposing`
        # for that label and the disabled rollout keeps spawning the
        # decomposer, producing manifests and child issues that the
        # operator explicitly disabled.
        gh, issue = _disabled_labeled_fixture()

        with patch.object(config, CONFIG_DECOMPOSE, False):
            mocks = self._run_decomposing(
                gh,
                issue,
                run_agent=_agent(session_id=DEV_SESSION, last_message=IMPLEMENTED_MESSAGE),
                has_new_commits=[False, True],
                push_branch=True,
            )

        # The agent that did run was the dev agent (legacy implementing
        # took over), not the decomposer.
        mocks[RUN_AGENT].assert_called_once()
        self.assertEqual(
            mocks[RUN_AGENT].call_args.args[0],
            config.DEV_AGENT,
            "kill switch must route to the dev backend, not decomposer",
        )

        # Label transitioned to implementing. Must never have routed
        # through `blocked` (that would have implied children created).
        self.assertIn(
            LABEL_IMPLEMENTING,
            _labels_for_issue(gh, DISABLED_LABELED_ISSUE_NUMBER),
        )
        self.assertNotIn(
            LABEL_BLOCKED,
            _labels_for_issue(gh, DISABLED_LABELED_ISSUE_NUMBER),
        )

        # Decomposer-side park state cleared so `_handle_implementing`'s
        # awaiting_human resume branch doesn't fire on stale state.
        self.assertFalse(gh.pinned_data(DISABLED_LABELED_ISSUE_NUMBER).get(KEY_AWAITING_HUMAN))
        self.assertIsNone(gh.pinned_data(DISABLED_LABELED_ISSUE_NUMBER).get("park_reason"))

        # Routing comment posted; no children created.
        self.assertTrue(
            any(
                "decomposition is disabled" in body
                for body in _comments_for_issue(
                    gh,
                    DISABLED_LABELED_ISSUE_NUMBER,
                )
            )
        )
        self.assertEqual(gh.created_child_issues, [])

    def test_off_ratchets_past_stage_comments(
        self,
    ) -> None:
        # When DECOMPOSE flips off mid-flight, decomposing-era human
        # comments newer than `last_action_comment_id` must be marked
        # consumed before falling into `_handle_implementing`. The
        # implementer reads the full thread via `_recent_comments_text`
        # at spawn, so the dev sees those comments at implementation
        # time. Without the ratchet, the validating->in_review
        # watermark seed later treats those same comments as fresh PR
        # feedback and bounces the dev unnecessarily -- exactly the
        # replay `_handle_ready` already prevents on the single-decision
        # happy path.
        gh = FakeGitHubClient()
        issue = make_issue(DISABLED_RATCHET_ISSUE_NUMBER, label=LABEL_DECOMPOSING)
        # Decomposer-era HITL comments newer than the parked
        # last_action_comment_id (which is anchored on the original
        # pickup or an earlier decomposer round).
        issue.comments.append(
            FakeComment(
                id=RATCHET_FIRST_COMMENT_ID,
                body="please reconsider",
                user=FakeUser(TRUSTED_AUTHOR),
            )
        )
        issue.comments.append(
            FakeComment(
                id=RATCHET_LATEST_COMMENT_ID,
                body="the title is wrong",
                user=FakeUser("bob"),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            DISABLED_RATCHET_ISSUE_NUMBER,
            awaiting_human=True,
            park_reason="(test) decomposer asked a question",
            decomposer_agent=BACKEND_CLAUDE,
            decomposer_session_id=DECOMPOSER_SESSION,
            last_action_comment_id=PRIOR_ACTION_COMMENT_ID,
            pickup_comment_id=100,
        )

        with patch.object(config, CONFIG_DECOMPOSE, False):
            self._run_decomposing(
                gh,
                issue,
                run_agent=_agent(session_id=DEV_SESSION, last_message=IMPLEMENTED_MESSAGE),
                has_new_commits=[False, True],
                push_branch=True,
            )

        state = gh.pinned_data(DISABLED_RATCHET_ISSUE_NUMBER)
        last_action = state.get(KEY_LAST_ACTION_COMMENT_ID)
        # Must be past the highest decomposing-era comment so the
        # in_review watermark seed treats them as already-consumed.
        self.assertIsInstance(last_action, int)
        self.assertGreaterEqual(last_action, RATCHET_LATEST_COMMENT_ID)

    def test_off_keeps_last_action_monotonic(self) -> None:
        # The ratchet is one-way. If `last_action_comment_id` is
        # already past the latest visible comment (e.g. a prior tick
        # consumed everything and a later high-id comment hasn't been
        # posted yet), the kill-switch path must NOT lower it.
        gh = FakeGitHubClient()
        issue = make_issue(
            DISABLED_MONOTONIC_ISSUE_NUMBER,
            label=LABEL_DECOMPOSING,
        )
        # One older comment; latest visible id is 500.
        issue.comments.append(
            FakeComment(
                id=OLDER_COMMENT_ID,
                body="early note",
                user=FakeUser(TRUSTED_AUTHOR),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            DISABLED_MONOTONIC_ISSUE_NUMBER,
            awaiting_human=True,
            last_action_comment_id=PRESERVED_HIGH_WATERMARK,
            pickup_comment_id=100,
        )

        with patch.object(config, CONFIG_DECOMPOSE, False):
            self._run_decomposing(
                gh,
                issue,
                run_agent=_agent(session_id=DEV_SESSION, last_message=IMPLEMENTED_MESSAGE),
                has_new_commits=[False, True],
                push_branch=True,
            )

        # Must not regress below the previously persisted high water mark.
        self.assertGreaterEqual(
            gh.pinned_data(DISABLED_MONOTONIC_ISSUE_NUMBER).get(KEY_LAST_ACTION_COMMENT_ID),
            PRESERVED_HIGH_WATERMARK,
        )

    def test_off_finishes_half_complete_split(self) -> None:
        # If a SIGKILL crashed a split between the parent's last
        # incremental `children` write and the parent label flip,
        # turning the kill switch on must NOT abandon the orphan
        # children -- they already exist on GitHub. Half-finished
        # recovery sits ABOVE the kill-switch bailout precisely so a
        # disabled rollout can still finalize the in-flight state to
        # `blocked` without spawning the decomposer.
        gh = FakeGitHubClient()
        parent = make_issue(
            HALF_COMPLETE_DISABLED_PARENT_NUMBER,
            label=LABEL_DECOMPOSING,
        )
        gh.add_issue(parent)
        _seed_blocked_children(
            gh,
            HALF_COMPLETE_DISABLED_PARENT_NUMBER,
            RECOVERY_CHILD_NUMBERS,
        )
        gh.seed_state(
            HALF_COMPLETE_DISABLED_PARENT_NUMBER,
            children=list(RECOVERY_CHILD_NUMBERS),
            decomposer_agent=BACKEND_CLAUDE,
            decomposer_session_id=DECOMPOSER_SESSION,
        )

        with patch.object(config, CONFIG_DECOMPOSE, False):
            mocks = self._run_decomposing(
                gh,
                parent,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertIn(
            LABEL_BLOCKED,
            _labels_for_issue(gh, HALF_COMPLETE_DISABLED_PARENT_NUMBER),
        )
        self.assertNotIn(
            LABEL_IMPLEMENTING,
            _labels_for_issue(gh, HALF_COMPLETE_DISABLED_PARENT_NUMBER),
        )
        self.assertEqual(gh.created_child_issues, [])
