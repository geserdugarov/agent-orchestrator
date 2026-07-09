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

    def test_ready_drift_clears_children_and_orphans_them(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(800, label="ready", body="updated parent body")
        gh.add_issue(parent)
        gh.seed_state(
            800,
            user_content_hash="stale-hash",
            # Children list survived from blocked->ready transition; the
            # children are all in `done` (which is how the parent
            # reached `ready` in the first place).
            children=[801, 802],
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
        self.assertIn((800, "decomposing"), gh.label_history)
        state = gh.pinned_data(800)
        self.assertEqual(state.get("children"), [])
        self.assertIsNone(state.get("expected_children_count"))
        self.assertEqual(state.get("dep_graph"), {})
        self.assertNotEqual(state.get("user_content_hash"), "stale-hash")
        # Orphaned children listed in the notice so the operator can
        # close any that no longer apply.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertIn("#801", notice)
        self.assertIn("#802", notice)
        self.assertIn("ORPHANED", notice)


class DecomposingDriftBeforeHalfFinishedRecoveryTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `_handle_decomposing` checks half-finished
    recovery before user-content drift. If the issue was edited while
    `expected_children_count` / `children` are present, the recovery
    branch finalizes to `blocked` / `umbrella` against the stale
    manifest. The drift check must run FIRST so the manifest gets
    re-derived against the new body."""

    def test_children_clear_manifest_and_rerun_decomposer(
        self,
    ) -> None:
        # Simulate the recovery shape: parent label is still
        # `decomposing` and `children` is non-empty (a crash between
        # child creation and the parent label flip), but the human has
        # since edited the body. Without the fix, the recovery branch
        # would finalize to `blocked` against the stale manifest.
        gh = FakeGitHubClient()
        parent = make_issue(
            1100, label="decomposing", body="updated body",
        )
        gh.add_issue(parent)
        # A real child issue so the orphan listing has something to
        # reference.
        child = make_issue(1101, label="blocked")
        gh.add_issue(child)
        gh.seed_state(
            1100,
            user_content_hash="stale-hash",
            children=[1101],
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
        state = gh.pinned_data(1100)
        self.assertEqual(state.get("children"), [])
        self.assertIsNone(state.get("expected_children_count"))
        self.assertEqual(state.get("dep_graph"), {})
        # New hash baseline persisted.
        self.assertNotEqual(state.get("user_content_hash"), "stale-hash")
        # Parent did NOT finalize to `blocked` against the stale
        # manifest; instead the fresh decomposer voted `single` -> `ready`.
        self.assertNotIn((1100, "blocked"), gh.label_history)
        self.assertIn((1100, "ready"), gh.label_history)
        # Orphans listed in the notice.
        notice = next(
            body for _, body in gh.posted_comments
            if "re-running decomposer" in body
        )
        self.assertIn("#1101", notice)
        self.assertIn("ORPHANED", notice)
