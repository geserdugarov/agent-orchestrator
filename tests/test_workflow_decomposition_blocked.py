# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from typing import Optional

from orchestrator import workflow

from tests.decomposition_test_support import (
    _comments_for_issue,
    _labels_for_issue,
    _run_with_logs,
)
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

LABEL_BLOCKED = "blocked"
LABEL_DONE = "done"
LABEL_IMPLEMENTING = "implementing"
LABEL_READY = "ready"
KEY_AWAITING_HUMAN = "awaiting_human"
ALL_DONE_PARENT_NUMBER = 30
IN_PROGRESS_PARENT_NUMBER = 31
REJECTED_CHILD_PARENT_NUMBER = 32
DEPENDENCY_PARENT_NUMBER = 33
HELD_DEPENDENCY_PARENT_NUMBER = 34
NO_HELD_CHILDREN_PARENT_NUMBER = 35
MANUALLY_CLOSED_PARENT_NUMBER = 40
MANUALLY_CLOSED_DONE_CHILD_NUMBER = 401
MANUALLY_CLOSED_CHILD_NUMBER = 402
CLOSED_REVIEW_PARENT_NUMBER = 41
CLOSED_REVIEW_CHILD_NUMBER = 411
CLOSED_REVIEW_OTHER_CHILD_NUMBER = 412
UNLABELED_CHILD_PARENT_NUMBER = 42
UNLABELED_CHILD_NUMBER = 421
MISSING_CHILDREN_PARENT_NUMBER = 34
DEPENDENCY_BLOCKED_CHILD_NUMBER = 35
DEPENDENCY_BLOCKED_PARENT_NUMBER = 30
ACTIVATION_RECOVERY_PARENT_NUMBER = 36
PREVIOUSLY_PARKED_PARENT_NUMBER = 38
PREVIOUSLY_PARKED_CHILD_NUMBERS = (381, 382)
LAST_ACTION_COMMENT_ID = 999


def _make_children(
    parent_number: int,
    child_labels: list[Optional[str]],
) -> list[FakeIssue]:
    return [
        make_issue(parent_number * 10 + child_offset, label=label)
        for child_offset, label in enumerate(child_labels, start=1)
    ]


def _dependency_state(dep_graph: Optional[dict]) -> dict:
    if dep_graph is None:
        return {}
    return {"dep_graph": dep_graph}


def _seed_parent_with_children(
    *,
    parent_number: int,
    child_labels: list[Optional[str]],
    dep_graph: Optional[dict] = None,
) -> tuple[FakeGitHubClient, FakeIssue, list[FakeIssue]]:
    gh = FakeGitHubClient()
    parent = make_issue(parent_number, label=LABEL_BLOCKED)
    gh.add_issue(parent)
    children = _make_children(parent_number, child_labels)
    for child in children:
        gh.add_issue(child)
    gh.seed_state(
        parent_number,
        children=[seeded_child.number for seeded_child in children],
        decomposer_agent="claude",
        decomposer_session_id="dec-sess",
        **_dependency_state(dep_graph),
    )
    return gh, parent, children


def _run_blocked(
    case: _PatchedWorkflowMixin,
    gh: FakeGitHubClient,
    parent: FakeIssue,
) -> None:
    case._run(
        lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
        run_agent=_agent(),
    )


_run_dependency = _run_blocked
_run_recovery = _run_blocked


class HandleBlockedResolutionTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_all_children_done_flips_parent_to_ready(self) -> None:
        gh, parent, children = _seed_parent_with_children(
            parent_number=ALL_DONE_PARENT_NUMBER,
            child_labels=[LABEL_DONE, LABEL_DONE],
        )

        _run_blocked(self, gh, parent)

        self.assertIn(
            (ALL_DONE_PARENT_NUMBER, LABEL_READY),
            gh.label_history,
        )
        self.assertIn(
            "all children resolved",
            "\n".join(_comments_for_issue(gh, ALL_DONE_PARENT_NUMBER)),
        )

    def test_some_children_in_progress_no_op(self) -> None:
        gh, parent, children = _seed_parent_with_children(
            parent_number=IN_PROGRESS_PARENT_NUMBER,
            child_labels=[LABEL_DONE, LABEL_IMPLEMENTING],
        )

        _run_blocked(self, gh, parent)

        # No label flip on parent and no comment posted on the parent.
        self.assertNotIn(
            (IN_PROGRESS_PARENT_NUMBER, LABEL_READY),
            gh.label_history,
        )
        self.assertEqual(
            [body for issue_number, body in gh.posted_comments if issue_number == IN_PROGRESS_PARENT_NUMBER],
            [],
        )

    def test_rejected_child_parks_parent(self) -> None:
        gh, parent, children = _seed_parent_with_children(
            parent_number=REJECTED_CHILD_PARENT_NUMBER,
            child_labels=[LABEL_DONE, "rejected"],
        )

        _run_blocked(self, gh, parent)

        state = gh.pinned_data(REJECTED_CHILD_PARENT_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
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
        parent = make_issue(MANUALLY_CLOSED_PARENT_NUMBER, label=LABEL_BLOCKED)
        gh.add_issue(parent)
        # children[0]: properly done -- closed with label `done`.
        done_child = make_issue(
            MANUALLY_CLOSED_DONE_CHILD_NUMBER,
            label=LABEL_DONE,
        )
        done_child.closed = True
        gh.add_issue(done_child)
        # children[1]: manually closed mid-implementation. Label stays
        # `implementing` because no orchestrator transition closed it.
        closed_child = make_issue(
            MANUALLY_CLOSED_CHILD_NUMBER,
            label=LABEL_IMPLEMENTING,
        )
        closed_child.closed = True
        gh.add_issue(closed_child)
        gh.seed_state(
            MANUALLY_CLOSED_PARENT_NUMBER,
            children=[
                MANUALLY_CLOSED_DONE_CHILD_NUMBER,
                MANUALLY_CLOSED_CHILD_NUMBER,
            ],
        )

        _run_blocked(self, gh, parent)

        self.assertTrue(gh.pinned_data(MANUALLY_CLOSED_PARENT_NUMBER).get(KEY_AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("closed without reaching", last_comment)
        self.assertIn(f"#{MANUALLY_CLOSED_CHILD_NUMBER}", last_comment)
        # Crucially: the parent must NOT have flipped to `ready`. With
        # only the all-done branch, the manually-closed child carrying
        # a non-"done" label correctly fails the `all(lbl == "done")`
        # check; but if a future change lowered that bar (e.g. "all
        # closed"), this assertion would catch the regression.
        self.assertNotIn(
            (MANUALLY_CLOSED_PARENT_NUMBER, LABEL_READY),
            gh.label_history,
        )

    def test_closed_review_child_does_not_park_parent(
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
        parent = make_issue(CLOSED_REVIEW_PARENT_NUMBER, label=LABEL_BLOCKED)
        gh.add_issue(parent)
        in_review_child = make_issue(
            CLOSED_REVIEW_CHILD_NUMBER,
            label="in_review",
        )
        in_review_child.closed = True
        gh.add_issue(in_review_child)
        other_child = make_issue(
            CLOSED_REVIEW_OTHER_CHILD_NUMBER,
            label=LABEL_IMPLEMENTING,
        )
        gh.add_issue(other_child)
        gh.seed_state(
            CLOSED_REVIEW_PARENT_NUMBER,
            children=[
                CLOSED_REVIEW_CHILD_NUMBER,
                CLOSED_REVIEW_OTHER_CHILD_NUMBER,
            ],
        )

        _run_blocked(self, gh, parent)

        state = gh.pinned_data(CLOSED_REVIEW_PARENT_NUMBER)
        self.assertFalse(state.get(KEY_AWAITING_HUMAN))
        # Parent stays `blocked`: no `ready` flip while other_child is
        # still implementing, and no manual-close park comment posted.
        self.assertNotIn(
            (CLOSED_REVIEW_PARENT_NUMBER, LABEL_READY),
            gh.label_history,
        )
        self.assertFalse(
            any(
                "closed without reaching" in body
                for body in _comments_for_issue(
                    gh,
                    CLOSED_REVIEW_PARENT_NUMBER,
                )
            )
        )

    def test_manual_closed_unlabeled_child_parks(self) -> None:
        # Defensive corner: a child with no workflow label at all
        # (e.g. a label was manually stripped before the issue was
        # closed) is also invisible to the closed-in_review sweep.
        # The "manually closed" branch must catch it -- otherwise the
        # parent would still wait forever.
        gh = FakeGitHubClient()
        parent = make_issue(UNLABELED_CHILD_PARENT_NUMBER, label=LABEL_BLOCKED)
        gh.add_issue(parent)
        unlabeled_closed = make_issue(UNLABELED_CHILD_NUMBER, label=None)
        unlabeled_closed.closed = True
        gh.add_issue(unlabeled_closed)
        gh.seed_state(
            UNLABELED_CHILD_PARENT_NUMBER,
            children=[UNLABELED_CHILD_NUMBER],
        )

        _run_blocked(self, gh, parent)

        state = gh.pinned_data(UNLABELED_CHILD_PARENT_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        self.assertTrue(
            any(
                "closed without reaching" in body and f"#{UNLABELED_CHILD_NUMBER}" in body
                for _, body in gh.posted_comments
            )
        )


class HandleBlockedDependencyTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_unblocks_middle_child_when_dep_done(self) -> None:
        # children[0] is done; children[1] depends on [0] and is currently
        # blocked. Next blocked tick must relabel children[1] to `ready`.
        gh, parent, children = _seed_parent_with_children(
            parent_number=DEPENDENCY_PARENT_NUMBER,
            child_labels=[LABEL_DONE, LABEL_BLOCKED],
            dep_graph={"1": [0]},
        )

        _run_dependency(self, gh, parent)

        # children[1] flipped to ready by the dep-graph walk; parent
        # stays blocked because children[1] is not yet done.
        self.assertEqual(
            _labels_for_issue(gh, children[1].number),
            [LABEL_READY],
        )
        self.assertNotIn(
            (DEPENDENCY_PARENT_NUMBER, LABEL_READY),
            gh.label_history,
        )

    def test_held_children_log_pending_deps(self) -> None:
        # Visibility feature: a child still `blocked` on an unfinished
        # sibling is "held". `_handle_blocked` must surface it -- and the
        # exact dependency gating it -- on the tick log so an operator can
        # see why a decomposed parent is not advancing. children[0] is
        # in-flight (not done), so children[1] (depends on [0]) stays held.
        gh, parent, children = _seed_parent_with_children(
            parent_number=HELD_DEPENDENCY_PARENT_NUMBER,
            child_labels=[LABEL_IMPLEMENTING, LABEL_BLOCKED],
            dep_graph={"1": [0]},
        )

        log_lines = _run_with_logs(
            self,
            "orchestrator.workflow",
            "INFO",
            lambda: _run_dependency(self, gh, parent),
        )

        self.assertTrue(
            any(
                "blocked parent" in line
                and "1 held" in line
                and f"#{children[1].number} waits on #{children[0].number}" in line
                for line in log_lines
            ),
            log_lines,
        )
        # Held means genuinely still gated -- no relabel to `ready`.
        self.assertNotIn(
            (children[1].number, LABEL_READY),
            gh.label_history,
        )

    def test_no_held_children_emits_no_log(self) -> None:
        # When every child is either done or already running (none still
        # `blocked` on a sibling), nothing is held and the visibility log
        # stays silent -- a healthy parent must not spam the tick log.
        gh, parent, _children = _seed_parent_with_children(
            parent_number=NO_HELD_CHILDREN_PARENT_NUMBER,
            child_labels=[LABEL_DONE, LABEL_IMPLEMENTING],
        )

        with self.assertNoLogs("orchestrator.workflow", level="INFO"):
            _run_dependency(self, gh, parent)


class HandleBlockedRecoveryTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_blocked_with_no_recorded_children_parks(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(MISSING_CHILDREN_PARENT_NUMBER, label=LABEL_BLOCKED)
        gh.add_issue(parent)
        # No children pinned.
        gh.seed_state(MISSING_CHILDREN_PARENT_NUMBER, decomposer_agent="claude")

        _run_recovery(self, gh, parent)

        state = gh.pinned_data(MISSING_CHILDREN_PARENT_NUMBER)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))

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
        child = make_issue(DEPENDENCY_BLOCKED_CHILD_NUMBER, label=LABEL_BLOCKED)
        gh.add_issue(child)
        gh.seed_state(
            DEPENDENCY_BLOCKED_CHILD_NUMBER,
            parent_number=DEPENDENCY_BLOCKED_PARENT_NUMBER,
        )

        before_comments = list(gh.posted_comments)
        before_labels = list(gh.label_history)

        _run_recovery(self, gh, child)

        state = gh.pinned_data(DEPENDENCY_BLOCKED_CHILD_NUMBER)
        self.assertFalse(state.get(KEY_AWAITING_HUMAN))
        self.assertEqual(gh.posted_comments, before_comments)
        self.assertEqual(gh.label_history, before_labels)

    def test_walk_readies_blocked_child_without_deps(self) -> None:
        # Activation-recovery path: a no-dep child got stuck as `blocked`
        # because the decomposer's same-tick activation step crashed
        # (network blip etc.). The parent's `_handle_blocked` walk must
        # treat empty deps as deps-satisfied and flip the child to
        # `ready` so implementation can start.
        gh, parent, children = _seed_parent_with_children(
            parent_number=ACTIVATION_RECOVERY_PARENT_NUMBER,
            child_labels=[LABEL_BLOCKED, LABEL_BLOCKED],
            # No dep_graph -- both children have no recorded deps.
        )

        _run_recovery(self, gh, parent)

        # Both children flipped to `ready`. Parent stays `blocked`
        # because no children are `done` yet.
        for child in children:
            self.assertEqual(
                _labels_for_issue(gh, child.number),
                [LABEL_READY],
            )
        self.assertNotIn(
            (ACTIVATION_RECOVERY_PARENT_NUMBER, LABEL_READY),
            gh.label_history,
        )

    def test_all_done_clears_awaiting_human(self) -> None:
        # A prior tick parked the parent on `awaiting_human=True` because
        # one child was `rejected`. The operator fixed the rejection
        # off-band; eventually all children become `done`. The parent
        # flip to `ready` MUST clear the stale park so
        # `_handle_implementing` (next tick) starts a fresh implementer
        # run rather than routing through `_resume_developer_on_human_reply`
        # and either replaying long-stale comments or sitting silent.
        gh = FakeGitHubClient()
        parent = make_issue(PREVIOUSLY_PARKED_PARENT_NUMBER, label=LABEL_BLOCKED)
        gh.add_issue(parent)
        for child_number in PREVIOUSLY_PARKED_CHILD_NUMBERS:
            gh.add_issue(make_issue(child_number, label=LABEL_DONE))
        gh.seed_state(
            PREVIOUSLY_PARKED_PARENT_NUMBER,
            children=list(PREVIOUSLY_PARKED_CHILD_NUMBERS),
            awaiting_human=True,
            park_reason="rejected_child",
            last_action_comment_id=LAST_ACTION_COMMENT_ID,
        )

        _run_recovery(self, gh, parent)

        self.assertIn(
            (PREVIOUSLY_PARKED_PARENT_NUMBER, LABEL_READY),
            gh.label_history,
        )
        state = gh.pinned_data(PREVIOUSLY_PARKED_PARENT_NUMBER)
        self.assertFalse(state.get(KEY_AWAITING_HUMAN))
        self.assertIsNone(state.get("park_reason"))
