# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from typing import Optional
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import (
    FakeGitHubClient,
    FakeIssue,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleUmbrellaTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Umbrella parents have no implementation of their own; the only
    terminal path is "every child resolved -> close the umbrella as
    `done`". The rejected/manually-closed/dep-graph-walk branches mirror
    `_handle_blocked`."""

    def _seed_umbrella_with_children(
        self,
        *,
        parent_number: int,
        child_labels: list[Optional[str]],
        dep_graph: Optional[dict] = None,
    ) -> tuple[FakeGitHubClient, FakeIssue, list[FakeIssue]]:
        gh = FakeGitHubClient()
        parent = make_issue(parent_number, label="umbrella")
        gh.add_issue(parent)
        children: list[FakeIssue] = []
        for i, lbl in enumerate(child_labels):
            child = make_issue(parent_number * 10 + i + 1, label=lbl)
            gh.add_issue(child)
            children.append(child)
        seed = {
            "children": [c.number for c in children],
            "umbrella": True,
        }
        if dep_graph is not None:
            seed["dep_graph"] = dep_graph
        gh.seed_state(parent_number, **seed)
        return gh, parent, children

    def test_dispatcher_routes_umbrella_to_handler(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(60, label="umbrella")
        gh.add_issue(issue)

        with patch.object(workflow, "_handle_umbrella") as handler:
            workflow._process_issue(gh, _TEST_SPEC, issue)

        handler.assert_called_once_with(gh, _TEST_SPEC, issue)

    def test_all_children_done_closes_umbrella_as_done(self) -> None:
        gh, parent, children = self._seed_umbrella_with_children(
            parent_number=61, child_labels=["done", "done"],
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Terminal `done` label and the issue is closed -- mirrors how
        # the merged path finalizes a regular issue.
        self.assertIn((61, "done"), gh.label_history)
        self.assertTrue(parent.closed)
        # `umbrella_resolved_at` stamp recorded so a future audit can
        # tell automatic-resolution apart from a manual close.
        self.assertIn("umbrella_resolved_at", gh.pinned_data(61))
        self.assertTrue(any(
            "all children resolved" in body and "closing umbrella" in body
            for n, body in gh.posted_comments if n == 61
        ))

    def test_some_children_in_progress_no_op(self) -> None:
        gh, parent, children = self._seed_umbrella_with_children(
            parent_number=62, child_labels=["done", "implementing"],
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertNotIn((62, "done"), gh.label_history)
        self.assertFalse(parent.closed)
        self.assertEqual(
            [b for n, b in gh.posted_comments if n == 62], [],
        )

    def test_rejected_child_parks_umbrella(self) -> None:
        gh, parent, children = self._seed_umbrella_with_children(
            parent_number=63, child_labels=["done", "rejected"],
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(63)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((63, "done"), gh.label_history)
        self.assertFalse(parent.closed)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("rejected", last_comment)
        self.assertIn(f"#{children[1].number}", last_comment)

    def test_manually_closed_child_parks_umbrella(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(64, label="umbrella")
        gh.add_issue(parent)
        done_child = make_issue(641, label="done")
        done_child.closed = True
        gh.add_issue(done_child)
        closed_child = make_issue(642, label="implementing")
        closed_child.closed = True
        gh.add_issue(closed_child)
        gh.seed_state(64, children=[641, 642], umbrella=True)

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(64)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((64, "done"), gh.label_history)
        self.assertFalse(parent.closed)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("closed without reaching", last_comment)
        self.assertIn("#642", last_comment)

    def test_unblocks_middle_child_when_dep_done(self) -> None:
        # A child stuck `blocked` on a dep that's now `done` should be
        # flipped to `ready` exactly as `_handle_blocked` does -- an
        # umbrella's children can still depend on each other.
        gh, parent, children = self._seed_umbrella_with_children(
            parent_number=65,
            child_labels=["done", "blocked"],
            dep_graph={"1": [0]},
        )

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        flipped = [
            new for issue_n, new in gh.label_history
            if issue_n == children[1].number
        ]
        self.assertEqual(flipped, ["ready"])
        self.assertNotIn((65, "done"), gh.label_history)
        self.assertFalse(parent.closed)

    def test_held_children_are_logged_with_pending_deps(self) -> None:
        # Visibility feature mirrored from `_handle_blocked`: a child still
        # `blocked` on an unfinished sibling is "held". `_handle_umbrella`
        # must surface it -- and the exact dependency gating it -- on the
        # tick log so an operator can see why the umbrella is not yet
        # closing. children[0] is in-flight (not done), so children[1]
        # (depends on [0]) stays held.
        gh, parent, children = self._seed_umbrella_with_children(
            parent_number=66,
            child_labels=["implementing", "blocked"],
            dep_graph={"1": [0]},
        )

        with self.assertLogs("orchestrator.workflow", level="INFO") as cm:
            self._run(
                lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
                run_agent=_agent(),
            )

        self.assertTrue(
            any(
                "umbrella parent" in m
                and "1 held" in m
                and f"#{children[1].number} waits on #{children[0].number}" in m
                for m in cm.output
            ),
            cm.output,
        )
        self.assertNotIn((children[1].number, "ready"), gh.label_history)
        self.assertFalse(parent.closed)

    def test_no_held_children_emits_no_log(self) -> None:
        # When every child is either done or already running (none still
        # `blocked` on a sibling), nothing is held and the visibility log
        # stays silent -- a healthy umbrella must not spam the tick log.
        gh, parent, _children = self._seed_umbrella_with_children(
            parent_number=67,
            child_labels=["done", "implementing"],
        )

        with self.assertNoLogs("orchestrator.workflow", level="INFO"):
            self._run(
                lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
                run_agent=_agent(),
            )

    def test_umbrella_with_no_recorded_children_parks(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(66, label="umbrella")
        gh.add_issue(parent)
        gh.seed_state(66, umbrella=True)

        self._run(
            lambda: workflow._handle_umbrella(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(66)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((66, "done"), gh.label_history)
        self.assertFalse(parent.closed)
