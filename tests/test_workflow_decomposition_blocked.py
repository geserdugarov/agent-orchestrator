# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from typing import Optional

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


class HandleBlockedTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seed_parent_with_children(
        self,
        *,
        parent_number: int,
        child_labels: list[Optional[str]],
        dep_graph: Optional[dict] = None,
    ) -> tuple[FakeGitHubClient, FakeIssue, list[FakeIssue]]:
        gh = FakeGitHubClient()
        parent = make_issue(parent_number, label="blocked")
        gh.add_issue(parent)
        children: list[FakeIssue] = []
        for i, lbl in enumerate(child_labels):
            child = make_issue(parent_number * 10 + i + 1, label=lbl)
            gh.add_issue(child)
            children.append(child)
        seed = {
            "children": [c.number for c in children],
            "decomposer_agent": "claude",
            "decomposer_session_id": "dec-sess",
        }
        if dep_graph is not None:
            seed["dep_graph"] = dep_graph
        gh.seed_state(parent_number, **seed)
        return gh, parent, children

    def test_all_children_done_flips_parent_to_ready(self) -> None:
        gh, parent, children = self._seed_parent_with_children(
            parent_number=30, child_labels=["done", "done"],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((30, "ready"), gh.label_history)
        self.assertTrue(any(
            "all children resolved" in body
            for _, body in gh.posted_comments
        ))

    def test_some_children_in_progress_no_op(self) -> None:
        gh, parent, children = self._seed_parent_with_children(
            parent_number=31,
            child_labels=["done", "implementing"],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # No label flip on parent and no comment posted on the parent.
        self.assertNotIn((31, "ready"), gh.label_history)
        self.assertEqual(
            [b for n, b in gh.posted_comments if n == 31], [],
        )

    def test_rejected_child_parks_parent(self) -> None:
        gh, parent, children = self._seed_parent_with_children(
            parent_number=32,
            child_labels=["done", "rejected"],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(32)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("rejected", last_comment)
        self.assertIn(f"#{children[1].number}", last_comment)

    def test_manually_closed_child_parks_parent(self) -> None:
        # A child closed manually (e.g. via the GitHub UI) before
        # reaching `in_review` is invisible to `list_pollable_issues`
        # (which only sweeps closed issues for `in_review`). Its
        # workflow label stays frozen, so without this branch the
        # parent reads the stale label, neither the rejected nor the
        # all-done branch fires, and the parent waits forever for a
        # child that is gone. Park it for human adjudication, exactly
        # like a rejected child.
        gh = FakeGitHubClient()
        parent = make_issue(40, label="blocked")
        gh.add_issue(parent)
        # children[0]: properly done -- closed with label `done`.
        done_child = make_issue(401, label="done")
        done_child.closed = True
        gh.add_issue(done_child)
        # children[1]: manually closed mid-implementation. Label stays
        # `implementing` because no orchestrator transition closed it.
        closed_child = make_issue(402, label="implementing")
        closed_child.closed = True
        gh.add_issue(closed_child)
        gh.seed_state(40, children=[401, 402])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(40)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("closed without reaching", last_comment)
        self.assertIn("#402", last_comment)
        # Crucially: the parent must NOT have flipped to `ready`. With
        # only the all-done branch, the manually-closed child carrying
        # a non-"done" label correctly fails the `all(lbl == "done")`
        # check; but if a future change lowered that bar (e.g. "all
        # closed"), this assertion would catch the regression.
        self.assertNotIn((40, "ready"), gh.label_history)

    def test_closed_in_review_child_does_not_falsely_park_parent(
        self,
    ) -> None:
        # state=closed + label=in_review is the externally-merged
        # transient: the closed-in_review sweep in
        # `list_pollable_issues` picks the child up next tick and
        # `_handle_in_review` finalizes it to done/rejected. The
        # blocked parent must NOT pre-empt that finalization with a
        # manual-close park -- treating this as a manual override
        # would strand legitimately externally-merged children.
        gh = FakeGitHubClient()
        parent = make_issue(41, label="blocked")
        gh.add_issue(parent)
        in_review_child = make_issue(411, label="in_review")
        in_review_child.closed = True
        gh.add_issue(in_review_child)
        other_child = make_issue(412, label="implementing")
        gh.add_issue(other_child)
        gh.seed_state(41, children=[411, 412])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(41)
        self.assertFalse(data.get("awaiting_human"))
        # Parent stays `blocked`: no `ready` flip while other_child is
        # still implementing, and no manual-close park comment posted.
        self.assertNotIn((41, "ready"), gh.label_history)
        self.assertFalse(any(
            "closed without reaching" in body
            for n, body in gh.posted_comments if n == 41
        ))

    def test_manually_closed_child_with_no_label_parks_parent(self) -> None:
        # Defensive corner: a child with no workflow label at all
        # (e.g. a label was manually stripped before the issue was
        # closed) is also invisible to the closed-in_review sweep.
        # The "manually closed" branch must catch it -- otherwise the
        # parent would still wait forever.
        gh = FakeGitHubClient()
        parent = make_issue(42, label="blocked")
        gh.add_issue(parent)
        unlabeled_closed = make_issue(421, label=None)
        unlabeled_closed.closed = True
        gh.add_issue(unlabeled_closed)
        gh.seed_state(42, children=[421])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(42)
        self.assertTrue(data.get("awaiting_human"))
        self.assertTrue(any(
            "closed without reaching" in body and "#421" in body
            for _, body in gh.posted_comments
        ))

    def test_unblocks_middle_child_when_dep_done(self) -> None:
        # children[0] is done; children[1] depends on [0] and is currently
        # blocked. Next blocked tick must relabel children[1] to `ready`.
        gh, parent, children = self._seed_parent_with_children(
            parent_number=33,
            child_labels=["done", "blocked"],
            dep_graph={"1": [0]},
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # children[1] flipped to ready by the dep-graph walk; parent
        # stays blocked because children[1] is not yet done.
        flipped = [
            new for issue_n, new in gh.label_history
            if issue_n == children[1].number
        ]
        self.assertEqual(flipped, ["ready"])
        self.assertNotIn((33, "ready"), gh.label_history)

    def test_held_children_are_logged_with_pending_deps(self) -> None:
        # Visibility feature: a child still `blocked` on an unfinished
        # sibling is "held". `_handle_blocked` must surface it -- and the
        # exact dependency gating it -- on the tick log so an operator can
        # see why a decomposed parent is not advancing. children[0] is
        # in-flight (not done), so children[1] (depends on [0]) stays held.
        gh, parent, children = self._seed_parent_with_children(
            parent_number=34,
            child_labels=["implementing", "blocked"],
            dep_graph={"1": [0]},
        )

        with self.assertLogs("orchestrator.workflow", level="INFO") as cm:
            self._run(
                lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
                run_agent=_agent(),
            )

        self.assertTrue(
            any(
                "blocked parent" in m
                and "1 held" in m
                and f"#{children[1].number} waits on #{children[0].number}" in m
                for m in cm.output
            ),
            cm.output,
        )
        # Held means genuinely still gated -- no relabel to `ready`.
        self.assertNotIn((children[1].number, "ready"), gh.label_history)

    def test_no_held_children_emits_no_log(self) -> None:
        # When every child is either done or already running (none still
        # `blocked` on a sibling), nothing is held and the visibility log
        # stays silent -- a healthy parent must not spam the tick log.
        gh, parent, _children = self._seed_parent_with_children(
            parent_number=35,
            child_labels=["done", "implementing"],
        )

        with self.assertNoLogs("orchestrator.workflow", level="INFO"):
            self._run(
                lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
                run_agent=_agent(),
            )

    def test_blocked_with_no_recorded_children_parks(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(34, label="blocked")
        gh.add_issue(parent)
        # No children pinned.
        gh.seed_state(34, decomposer_agent="claude")

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(34)
        self.assertTrue(data.get("awaiting_human"))

    def test_blocked_child_with_parent_number_is_noop(self) -> None:
        # A dependency-blocked child created by the decomposer carries
        # `parent_number` in its pinned state but no `children` of its
        # own. Polling routes it through `_handle_blocked`, which must
        # leave it alone -- the parent's dep-graph walk is what
        # eventually relabels it `ready`. Without the parent_number
        # branch this would park the child as "manual relabel suspected"
        # and leave `awaiting_human=True` behind, which would then
        # corrupt the implementation phase once the parent unblocks it.
        gh = FakeGitHubClient()
        child = make_issue(35, label="blocked")
        gh.add_issue(child)
        gh.seed_state(35, parent_number=30)

        before_comments = list(gh.posted_comments)
        before_labels = list(gh.label_history)

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, child),
            run_agent=_agent(),
        )

        data = gh.pinned_data(35)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(gh.posted_comments, before_comments)
        self.assertEqual(gh.label_history, before_labels)

    def test_no_dep_blocked_child_flipped_to_ready_by_walk(self) -> None:
        # Activation-recovery path: a no-dep child got stuck as `blocked`
        # because the decomposer's same-tick activation step crashed
        # (network blip etc.). The parent's `_handle_blocked` walk must
        # treat empty deps as deps-satisfied and flip the child to
        # `ready` so implementation can start.
        gh, parent, children = self._seed_parent_with_children(
            parent_number=36,
            child_labels=["blocked", "blocked"],
            # No dep_graph -- both children have no recorded deps.
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Both children flipped to `ready`. Parent stays `blocked`
        # because no children are `done` yet.
        for child in children:
            flipped = [
                new for issue_n, new in gh.label_history
                if issue_n == child.number
            ]
            self.assertEqual(flipped, ["ready"])
        self.assertNotIn((36, "ready"), gh.label_history)

    def test_blocked_clears_awaiting_human_after_all_done(self) -> None:
        # A prior tick parked the parent on `awaiting_human=True` because
        # one child was `rejected`. The operator fixed the rejection
        # off-band; eventually all children become `done`. The parent
        # flip to `ready` MUST clear the stale park so
        # `_handle_implementing` (next tick) starts a fresh implementer
        # run rather than routing through `_resume_developer_on_human_reply`
        # and either replaying long-stale comments or sitting silent.
        gh = FakeGitHubClient()
        parent = make_issue(38, label="blocked")
        gh.add_issue(parent)
        child_a = make_issue(381, label="done")
        child_b = make_issue(382, label="done")
        gh.add_issue(child_a)
        gh.add_issue(child_b)
        gh.seed_state(
            38,
            children=[381, 382],
            awaiting_human=True,
            park_reason="rejected_child",
            last_action_comment_id=999,
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((38, "ready"), gh.label_history)
        data = gh.pinned_data(38)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
