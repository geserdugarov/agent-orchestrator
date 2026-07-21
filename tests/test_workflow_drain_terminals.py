# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Direct coverage of the shared `_drain_review_pr_terminals` helper:
the pr=None no-op, open-PR / open-issue negative path, merged-PR
finalize-to-done with event + cleanup, closed-without-merge
finalize-to-rejected with event + cleanup, the open-PR + manually-
closed-issue rejection without cleanup, the resolving_conflict
`conflict_round` coercion contract, the in_review missing-counter
contract, the already-closed-issue merged arc, and the terminal
usage-verdict receipt each arc posts before its pinned-state write."""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from orchestrator import workflow
from orchestrator.github import PinnedState

from tests.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import (
    EVENT_PR_CLOSED_WITHOUT_MERGE,
    EVENT_PR_MERGED,
    LABEL_DONE,
    LABEL_FIXING,
    LABEL_IN_REVIEW,
    LABEL_REJECTED,
    LABEL_RESOLVING_CONFLICT,
    STATE_CLOSED,
    STATE_OPEN,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
    _issue_branch,
    _state_with_pr_number,
)


DEFAULT_HEAD_SHA = "cafe1234"
MERGE_METHOD_EXTERNAL = "external"
_CLEANUP_MOCK_KEY = "_cleanup_terminal_branch"
_EVENT_KEY = "event"
_STAGE_KEY = "stage"
_CONFLICT_ROUND_KEY = "conflict_round"
_NO_PR_ISSUE_NUMBER = 310
_NO_PR_NUMBER = 31000
_OPEN_PR_ISSUE_NUMBER = 311
_OPEN_PR_NUMBER = 31100
_MERGED_PR_ISSUE_NUMBER = 312
_MERGED_PR_NUMBER = 31200
_CLOSED_PR_ISSUE_NUMBER = 313
_CLOSED_PR_NUMBER = 31300
_MANUALLY_CLOSED_ISSUE_NUMBER = 314
_MANUALLY_CLOSED_PR_NUMBER = 31400
_ALREADY_CLOSED_ISSUE_NUMBER = 315
_ALREADY_CLOSED_PR_NUMBER = 31500
_CONFLICT_MERGED_ISSUE_NUMBER = 316
_CONFLICT_MERGED_PR_NUMBER = 31600
_CONFLICT_CLOSED_ISSUE_NUMBER = 317
_CONFLICT_CLOSED_PR_NUMBER = 31700
_REVIEW_MERGED_ISSUE_NUMBER = 318
_REVIEW_MERGED_PR_NUMBER = 31800


@dataclass(frozen=True)
class _DrainContext:
    gh: FakeGitHubClient
    issue: object
    state: PinnedState
    pr: FakePR
    stage: str


class _DrainTerminalCall:
    def __init__(self, context: _DrainContext) -> None:
        self._context = context
        self.was_drained = False

    def __call__(self) -> None:
        self.was_drained = workflow._drain_review_pr_terminals(
            self._context.gh,
            _TEST_SPEC,
            self._context.issue,
            self._context.state,
            self._context.pr,
            stage=self._context.stage,
        )


class DrainReviewPrTerminalsTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Direct coverage of the shared `_drain_review_pr_terminals` helper.

    `_handle_in_review`, `_handle_fixing`, and `_handle_resolving_conflict`
    all delegate their terminal arcs (merged PR -> `done`, closed PR ->
    `rejected`, open PR + manually-closed issue -> `rejected` without
    branch cleanup) to this helper. The per-stage handler tests cover the
    integrated behavior; these focused tests pin the helper contract
    (return value, event shape, branch-cleanup semantics, pr=None no-op)
    independently of any stage wiring.
    """

    def test_pr_none_returns_false_no_op(self) -> None:
        # Fixing's PR-fetch failure path sets `pr=None` and hands it
        # straight to the helper; the helper must treat that as a no-op
        # so the calling handler can fall through to its own fetch-
        # failure deferral (the `if pr is None: return` guard further
        # down the fixing body). No label change, no state writes, no
        # cleanup, no events.
        gh = FakeGitHubClient()
        issue = make_issue(_NO_PR_ISSUE_NUMBER, label=LABEL_FIXING)
        gh.add_issue(issue)
        state = _state_with_pr_number(
            gh,
            _NO_PR_ISSUE_NUMBER,
            _NO_PR_NUMBER,
        )

        mocks = self._run(
            lambda: self.assertFalse(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, None, stage=LABEL_FIXING,
                )
            ),
            run_agent=_agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        mocks[_CLEANUP_MOCK_KEY].assert_not_called()
        self.assertEqual(gh.recorded_events, [])

    def test_open_pr_open_issue_returns_false(self) -> None:
        # The handler-side rescan / debounce / drift logic depends on
        # the helper returning False for a "nothing terminal" state so
        # the caller can continue with the same `pr`.
        gh = FakeGitHubClient()
        issue = make_issue(_OPEN_PR_ISSUE_NUMBER, label=LABEL_IN_REVIEW)
        gh.add_issue(issue)
        pr = FakePR(
            number=_OPEN_PR_NUMBER,
            head_branch=_issue_branch(_OPEN_PR_ISSUE_NUMBER),
            head=FakePRRef(sha=DEFAULT_HEAD_SHA),
            merged=False, state=STATE_OPEN,
        )
        gh.add_pr(pr)
        state = _state_with_pr_number(
            gh,
            _OPEN_PR_ISSUE_NUMBER,
            _OPEN_PR_NUMBER,
        )

        mocks = self._run(
            lambda: self.assertFalse(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage=LABEL_IN_REVIEW,
                )
            ),
            run_agent=_agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        mocks[_CLEANUP_MOCK_KEY].assert_not_called()
        self.assertEqual(gh.recorded_events, [])


class DrainReviewPrTerminalTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Merged, closed, and manually stopped PRs take distinct exits."""

    def test_merged_pr_finalizes_to_done(self) -> None:
        # The merged arc: stamp `merged_at`, flip to `done`, emit
        # `pr_merged` with `merge_method="external"` and the supplied
        # stage, close the issue if still open, and run branch cleanup.
        gh = FakeGitHubClient()
        issue = make_issue(_MERGED_PR_ISSUE_NUMBER, label=LABEL_FIXING)
        gh.add_issue(issue)
        pr = FakePR(
            number=_MERGED_PR_NUMBER,
            head_branch=_issue_branch(_MERGED_PR_ISSUE_NUMBER),
            head=FakePRRef(sha=DEFAULT_HEAD_SHA),
            merged=True, state=STATE_CLOSED,
        )
        gh.add_pr(pr)
        state = _state_with_pr_number(
            gh,
            _MERGED_PR_ISSUE_NUMBER,
            _MERGED_PR_NUMBER,
            review_round=2,
            conflict_round=0,
            branch=_issue_branch(_MERGED_PR_ISSUE_NUMBER),
        )

        mocks = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage=LABEL_FIXING,
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((_MERGED_PR_ISSUE_NUMBER, LABEL_DONE), gh.label_history)
        self.assertIn("merged_at", state.data)
        self.assertTrue(issue.closed)
        mocks[_CLEANUP_MOCK_KEY].assert_called_once_with(
            gh,
            _TEST_SPEC,
            _MERGED_PR_ISSUE_NUMBER,
            branch=_issue_branch(_MERGED_PR_ISSUE_NUMBER),
        )
        merged_events = [
            event for event in gh.recorded_events
            if event[_EVENT_KEY] == EVENT_PR_MERGED
        ]
        self.assertEqual(len(merged_events), 1)
        event = merged_events[0]
        self.assertEqual(event[_STAGE_KEY], LABEL_FIXING)
        self.assertEqual(event["pr_number"], _MERGED_PR_NUMBER)
        self.assertEqual(event["merge_method"], MERGE_METHOD_EXTERNAL)
        self.assertEqual(event["sha"], DEFAULT_HEAD_SHA)
        self.assertEqual(event["review_round"], 2)

    def test_closed_unmerged_pr_finalizes_to_rejected(
        self,
    ) -> None:
        # The closed-PR arc: stamp `closed_without_merge_at`, flip to
        # `rejected`, emit `pr_closed_without_merge` with the supplied
        # stage, close the issue if still open, and run branch cleanup.
        # The branch is dead weight once the PR is gone, mirroring the
        # merged-PR cleanup order.
        gh = FakeGitHubClient()
        issue = make_issue(
            _CLOSED_PR_ISSUE_NUMBER,
            label=LABEL_RESOLVING_CONFLICT,
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=_CLOSED_PR_NUMBER,
            head_branch=_issue_branch(_CLOSED_PR_ISSUE_NUMBER),
            head=FakePRRef(sha="dead0001"),
            merged=False, state=STATE_CLOSED,
        )
        gh.add_pr(pr)
        state = _state_with_pr_number(
            gh,
            _CLOSED_PR_ISSUE_NUMBER,
            _CLOSED_PR_NUMBER,
            review_round=3,
            conflict_round=2,
            branch=_issue_branch(_CLOSED_PR_ISSUE_NUMBER),
        )

        mocks = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr,
                    stage=LABEL_RESOLVING_CONFLICT,
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((_CLOSED_PR_ISSUE_NUMBER, LABEL_REJECTED), gh.label_history)
        self.assertIn("closed_without_merge_at", state.data)
        self.assertTrue(issue.closed)
        mocks[_CLEANUP_MOCK_KEY].assert_called_once_with(
            gh,
            _TEST_SPEC,
            _CLOSED_PR_ISSUE_NUMBER,
            branch=_issue_branch(_CLOSED_PR_ISSUE_NUMBER),
        )
        closed_events = [
            event for event in gh.recorded_events
            if event[_EVENT_KEY] == EVENT_PR_CLOSED_WITHOUT_MERGE
        ]
        self.assertEqual(len(closed_events), 1)
        event = closed_events[0]
        self.assertEqual(event[_STAGE_KEY], LABEL_RESOLVING_CONFLICT)
        self.assertEqual(event["pr_number"], _CLOSED_PR_NUMBER)
        self.assertEqual(event["sha"], "dead0001")
        self.assertEqual(event["review_round"], 3)
        self.assertEqual(event[_CONFLICT_ROUND_KEY], 2)

    def test_open_pr_closed_issue_rejects_no_cleanup(
        self,
    ) -> None:
        # Open PR + manually closed issue is a human stop signal: flip
        # to `rejected` so the in_review HITL ready-ping cannot
        # advertise the PR as ready for human merge over the human
        # rejection, but deliberately leave the branch alone so the
        # operator can salvage / reopen the still-open PR. No event
        # emit either -- `pr_closed_without_merge` is reserved for the
        # genuine closed-PR arc above.
        gh = FakeGitHubClient()
        issue = make_issue(
            _MANUALLY_CLOSED_ISSUE_NUMBER,
            label=LABEL_IN_REVIEW,
        )
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=_MANUALLY_CLOSED_PR_NUMBER,
            head_branch=_issue_branch(_MANUALLY_CLOSED_ISSUE_NUMBER),
            head=FakePRRef(sha=DEFAULT_HEAD_SHA),
            merged=False, state=STATE_OPEN,
        )
        gh.add_pr(pr)
        state = _state_with_pr_number(
            gh,
            _MANUALLY_CLOSED_ISSUE_NUMBER,
            _MANUALLY_CLOSED_PR_NUMBER,
        )

        mocks = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage=LABEL_IN_REVIEW,
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn(
            (_MANUALLY_CLOSED_ISSUE_NUMBER, LABEL_REJECTED),
            gh.label_history,
        )
        self.assertIn("closed_without_merge_at", state.data)
        # The PR is still open and may be reopened / salvaged, so the
        # branch must survive this exit.
        mocks[_CLEANUP_MOCK_KEY].assert_not_called()
        # No `pr_closed_without_merge` emit for the open-PR case.
        self.assertEqual(
            [event for event in gh.recorded_events
             if event[_EVENT_KEY] == EVENT_PR_CLOSED_WITHOUT_MERGE],
            [],
        )
        self.assertEqual(
            [
                event for event in gh.recorded_events
                if event[_EVENT_KEY] == EVENT_PR_MERGED
            ],
            [],
        )


class DrainReviewPrMetadataTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Terminal events preserve stage-specific round metadata."""

    def test_conflict_route_keeps_zero_round(
        self,
    ) -> None:
        # Legacy / manually-relabelled `resolving_conflict` states may
        # land in the terminal arcs without `conflict_round` ever being
        # seeded (the in_review route normally initializes it to 0
        # before flipping the label). The pre-refactor inline code
        # coerced the value via `int(state.get("conflict_round") or 0)`
        # so the audit record always carried the field. `build_event_record`
        # drops None-valued extras, so the helper must keep that coercion
        # for `stage="resolving_conflict"` -- otherwise legacy states
        # silently lose `conflict_round` from `pr_merged` /
        # `pr_closed_without_merge` events.
        gh = FakeGitHubClient()
        issue = make_issue(
            _CONFLICT_MERGED_ISSUE_NUMBER,
            label=LABEL_RESOLVING_CONFLICT,
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=_CONFLICT_MERGED_PR_NUMBER,
            head_branch=_issue_branch(_CONFLICT_MERGED_ISSUE_NUMBER),
            head=FakePRRef(sha="feed1234"),
            merged=True, state=STATE_CLOSED,
        )
        gh.add_pr(pr)
        # Deliberately omit `conflict_round` from the pinned state.
        state = _state_with_pr_number(
            gh,
            _CONFLICT_MERGED_ISSUE_NUMBER,
            _CONFLICT_MERGED_PR_NUMBER,
        )

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr,
                    stage=LABEL_RESOLVING_CONFLICT,
                )
            ),
            run_agent=_agent(),
        )

        merged_events = [
            event for event in gh.recorded_events
            if event[_EVENT_KEY] == EVENT_PR_MERGED
        ]
        self.assertEqual(len(merged_events), 1)
        merged_event = merged_events[0]
        self.assertEqual(merged_event[_STAGE_KEY], LABEL_RESOLVING_CONFLICT)
        # Field must be present (build_event_record drops None), and
        # the coerced default must be 0.
        self.assertIn(_CONFLICT_ROUND_KEY, merged_event)
        self.assertEqual(merged_event[_CONFLICT_ROUND_KEY], 0)

        # Same coercion for the closed-without-merge arc.
        issue2 = make_issue(
            _CONFLICT_CLOSED_ISSUE_NUMBER,
            label=LABEL_RESOLVING_CONFLICT,
        )
        gh.add_issue(issue2)
        pr2 = FakePR(
            number=_CONFLICT_CLOSED_PR_NUMBER,
            head_branch=_issue_branch(_CONFLICT_CLOSED_ISSUE_NUMBER),
            head=FakePRRef(sha="feed5678"),
            merged=False, state=STATE_CLOSED,
        )
        gh.add_pr(pr2)
        state2 = _state_with_pr_number(
            gh,
            _CONFLICT_CLOSED_ISSUE_NUMBER,
            _CONFLICT_CLOSED_PR_NUMBER,
        )

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue2, state2, pr2,
                    stage=LABEL_RESOLVING_CONFLICT,
                )
            ),
            run_agent=_agent(),
        )

        closed_events = [
            event for event in gh.recorded_events
            if event[_EVENT_KEY] == EVENT_PR_CLOSED_WITHOUT_MERGE
        ]
        self.assertEqual(len(closed_events), 1)
        closed_event = closed_events[0]
        self.assertIn(_CONFLICT_ROUND_KEY, closed_event)
        self.assertEqual(closed_event[_CONFLICT_ROUND_KEY], 0)

    def test_review_terminal_omits_missing_round(self) -> None:
        # The other two stages have always passed the raw
        # `state.get("conflict_round")` through, so a missing counter
        # naturally drops out via `build_event_record`. Pin that contract
        # so a future refactor doesn't accidentally start coercing for
        # `in_review` / `fixing` and start emitting a `conflict_round=0`
        # field on states that never had the counter.
        gh = FakeGitHubClient()
        issue = make_issue(_REVIEW_MERGED_ISSUE_NUMBER, label=LABEL_IN_REVIEW)
        gh.add_issue(issue)
        pr = FakePR(
            number=_REVIEW_MERGED_PR_NUMBER,
            head_branch=_issue_branch(_REVIEW_MERGED_ISSUE_NUMBER),
            head=FakePRRef(sha="cafe5678"),
            merged=True, state=STATE_CLOSED,
        )
        gh.add_pr(pr)
        state = _state_with_pr_number(
            gh,
            _REVIEW_MERGED_ISSUE_NUMBER,
            _REVIEW_MERGED_PR_NUMBER,
        )

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage=LABEL_IN_REVIEW,
                )
            ),
            run_agent=_agent(),
        )

        merged_events = [
            event for event in gh.recorded_events
            if event[_EVENT_KEY] == EVENT_PR_MERGED
        ]
        self.assertEqual(len(merged_events), 1)
        self.assertNotIn(_CONFLICT_ROUND_KEY, merged_events[0])


class DrainReviewPrReceiptTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Terminal drains tolerate closed issues and persist usage receipts."""

    def test_merged_arc_handles_already_closed_issue(
        self,
    ) -> None:
        # A `Resolves #N` footer auto-closes the issue the moment the PR
        # merges, so when the closed-issue sweep yields this case the
        # helper sees an already-closed issue. The merged arc still
        # finalizes the label, but must not crash trying to re-close
        # what GitHub already closed.
        gh = FakeGitHubClient()
        issue = make_issue(_ALREADY_CLOSED_ISSUE_NUMBER, label=LABEL_FIXING)
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=_ALREADY_CLOSED_PR_NUMBER,
            head_branch=_issue_branch(_ALREADY_CLOSED_ISSUE_NUMBER),
            head=FakePRRef(sha="feed0001"),
            merged=True, state=STATE_CLOSED,
        )
        gh.add_pr(pr)
        state = _state_with_pr_number(
            gh,
            _ALREADY_CLOSED_ISSUE_NUMBER,
            _ALREADY_CLOSED_PR_NUMBER,
        )

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage=LABEL_FIXING,
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn(
            (_ALREADY_CLOSED_ISSUE_NUMBER, LABEL_DONE),
            gh.label_history,
        )
        self.assertTrue(issue.closed)
        merged_events = [
            event for event in gh.recorded_events
            if event[_EVENT_KEY] == EVENT_PR_MERGED
        ]
        self.assertEqual(len(merged_events), 1)
        self.assertEqual(merged_events[0][_STAGE_KEY], LABEL_FIXING)

    def test_each_terminal_posts_usage_verdict(self) -> None:
        # All three terminal arcs -- merged -> done, closed -> rejected, and
        # the open-PR + manually-closed-issue -> rejected path -- surface the
        # cumulative usage verdict as a tracked comment posted BEFORE the
        # arc's `write_pinned_state`, so its id rides the persisted state.
        cases = [
            # (issue, pr, merged, pr_state, issue_closed, stage)
            (320, 32000, True, STATE_CLOSED, False, LABEL_IN_REVIEW),
            (321, 32100, False, STATE_CLOSED, False, LABEL_FIXING),
            (
                322,
                32200,
                False,
                STATE_OPEN,
                True,
                LABEL_RESOLVING_CONFLICT,
            ),
        ]
        for (
            issue_number,
            pr_number,
            merged,
            pr_state,
            issue_closed,
            stage,
        ) in cases:
            with self.subTest(stage=stage):
                gh = FakeGitHubClient()
                issue = make_issue(issue_number, label=stage)
                issue.closed = issue_closed
                gh.add_issue(issue)
                pr = FakePR(
                    number=pr_number,
                    head_branch=_issue_branch(issue_number),
                    head=FakePRRef(sha=DEFAULT_HEAD_SHA),
                    merged=merged, state=pr_state,
                )
                gh.add_pr(pr)
                state = _state_with_pr_number(
                    gh,
                    issue_number,
                    pr_number,
                    conflict_round=0,
                    issue_agent_runs=2, issue_total_tokens=1000,
                    issue_total_cost_usd=0.5, issue_cost_sources=["reported"],
                )

                drain_call = _DrainTerminalCall(
                    _DrainContext(gh, issue, state, pr, stage),
                )
                self._run(drain_call, run_agent=_agent())
                self.assertTrue(drain_call.was_drained)

                receipts = [
                    body for posted_n, body in gh.posted_comments
                    if posted_n == issue_number and body.startswith(":receipt:")
                ]
                self.assertEqual(len(receipts), 1)
                self.assertIn(
                    "this issue: 2 agent runs · 1,000 tokens · $0.50",
                    receipts[0],
                )
                receipt_comment = next(
                    comment for comment in issue.comments
                    if comment.body.startswith(":receipt:")
                )
                self.assertIn(
                    receipt_comment.id,
                    gh.pinned_data(issue_number).get(
                        "orchestrator_comment_ids",
                        [],
                    ),
                )


if __name__ == "__main__":
    unittest.main()
