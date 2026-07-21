# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests.fakes import (
    FakeGitHubClient,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)

STALE_USER_CONTENT_HASH = "stale-hash"
READY_DRIFT_PARENT_NUMBER = 800
READY_DRIFT_CHILD_NUMBERS = (801, 802)
RECOVERY_PARENT_NUMBER = 1100
RECOVERY_CHILD_NUMBER = 1101


class ReadyDriftClearsStaleManifestStateTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 1: a non-umbrella parent reaches `ready` after all
    its children finish (`_handle_blocked`'s all-done branch flips
    `blocked` -> `ready`), so the parent still carries `children` /
    `dep_graph` from the prior manifest. The drift branch in
    `_handle_ready` must clear that manifest state, otherwise the next
    `_handle_decomposing` tick's half-finished recovery would fire and
    flip back to `blocked` WITHOUT re-running the decomposer."""

    def test_drift_clears_and_orphans_children(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(
            READY_DRIFT_PARENT_NUMBER,
            label="ready",
            body="updated parent body",
        )
        gh.add_issue(parent)
        gh.seed_state(
            READY_DRIFT_PARENT_NUMBER,
            user_content_hash=STALE_USER_CONTENT_HASH,
            # Children list survived from blocked->ready transition; the
            # children are all in `done` (which is how the parent
            # reached `ready` in the first place).
            children=list(READY_DRIFT_CHILD_NUMBERS),
            dep_graph={"1": [0]},
            expected_children_count=2,
            pickup_comment_id=100,
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Routed back to decomposing AND manifest state cleared so
        # `_handle_decomposing`'s recovery branch (which keys on
        # `expected_children_count is not None OR children is non-empty`)
        # cannot fire and short-circuit the re-decompose.
        self.assertIn(
            (READY_DRIFT_PARENT_NUMBER, "decomposing"),
            gh.label_history,
        )
        state = gh.pinned_data(READY_DRIFT_PARENT_NUMBER)
        self.assertEqual(state.get("children"), [])
        self.assertIsNone(state.get("expected_children_count"))
        self.assertEqual(state.get("dep_graph"), {})
        self.assertNotEqual(
            state.get("user_content_hash"),
            STALE_USER_CONTENT_HASH,
        )
        # Orphaned children listed in the notice so the operator can
        # close any that no longer apply.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertIn("#801", notice)
        self.assertIn("#802", notice)
        self.assertIn("ORPHANED", notice)


class DriftBeforeHalfFinishedRecoveryTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `_handle_decomposing` checks half-finished
    recovery before user-content drift. If the issue was edited while
    `expected_children_count` / `children` are present, the recovery
    branch finalizes to `blocked` / `umbrella` against the stale
    manifest. The drift check must run FIRST so the manifest gets
    re-derived against the new body."""

    def test_clears_manifest_and_reruns_decomposer(
        self,
    ) -> None:
        # Simulate the recovery shape: parent label is still
        # `decomposing` and `children` is non-empty (a crash between
        # child creation and the parent label flip), but the human has
        # since edited the body. Without the fix, the recovery branch
        # would finalize to `blocked` against the stale manifest.
        gh = FakeGitHubClient()
        parent = make_issue(
            RECOVERY_PARENT_NUMBER,
            label="decomposing",
            body="updated body",
        )
        gh.add_issue(parent)
        # A real child issue so the orphan listing has something to
        # reference.
        child = make_issue(RECOVERY_CHILD_NUMBER, label="blocked")
        gh.add_issue(child)
        gh.seed_state(
            RECOVERY_PARENT_NUMBER,
            user_content_hash=STALE_USER_CONTENT_HASH,
            children=[RECOVERY_CHILD_NUMBER],
            expected_children_count=1,
            decomposer_session_id="old-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, parent),
            run_agent=_agent(
                session_id="new-sess",
                last_message=(
                    "fits one\n\n```orchestrator-manifest\n"
                    '{"decision": "single", "rationale": "small"}\n'
                    "```"
                ),
            ),
            has_new_commits=False,
        )

        # The decomposer ran fresh against the new body (the recovery
        # branch did NOT short-circuit to `blocked`).
        mocks["run_agent"].assert_called_once()
        # Manifest tracking cleared so the recovery branch cannot
        # fire on subsequent ticks against the stale state.
        state = gh.pinned_data(RECOVERY_PARENT_NUMBER)
        self.assertEqual(state.get("children"), [])
        self.assertIsNone(state.get("expected_children_count"))
        self.assertEqual(state.get("dep_graph"), {})
        # New hash baseline persisted.
        self.assertNotEqual(
            state.get("user_content_hash"),
            STALE_USER_CONTENT_HASH,
        )
        # Parent did NOT finalize to `blocked` against the stale
        # manifest; instead the fresh decomposer voted `single` -> `ready`.
        self.assertNotIn(
            (RECOVERY_PARENT_NUMBER, "blocked"),
            gh.label_history,
        )
        self.assertIn(
            (RECOVERY_PARENT_NUMBER, "ready"),
            gh.label_history,
        )
        # Orphans listed in the notice.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertIn("#1101", notice)
        self.assertIn("ORPHANED", notice)
