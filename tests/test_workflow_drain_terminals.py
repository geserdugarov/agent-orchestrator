# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Direct coverage of the shared `_drain_review_pr_terminals` helper:
the pr=None no-op, open-PR / open-issue negative path, merged-PR
finalize-to-done with event + cleanup, closed-without-merge
finalize-to-rejected with event + cleanup, the open-PR + manually-
closed-issue rejection without cleanup, the resolving_conflict
`conflict_round` coercion contract, the in_review missing-counter
contract, and the already-closed-issue merged arc."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
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

    def _state_with_pr_number(self, gh, issue_number, pr_number, **extra):
        from orchestrator.github import PinnedState

        seed = {"pr_number": pr_number, **extra}
        gh.seed_state(issue_number, **seed)
        return PinnedState(comment_id=None, data=dict(seed))

    def test_pr_none_returns_false_no_op(self) -> None:
        # Fixing's PR-fetch failure path sets `pr=None` and hands it
        # straight to the helper; the helper must treat that as a no-op
        # so the calling handler can fall through to its own fetch-
        # failure deferral (the `if pr is None: return` guard further
        # down the fixing body). No label change, no state writes, no
        # cleanup, no events.
        gh = FakeGitHubClient()
        issue = make_issue(310, label="fixing")
        gh.add_issue(issue)
        state = self._state_with_pr_number(gh, 310, 31000)

        result = self._run(
            lambda: self.assertFalse(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, None, stage="fixing",
                )
            ),
            run_agent=_agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()
        self.assertEqual(gh.recorded_events, [])

    def test_open_pr_open_issue_returns_false(self) -> None:
        # The handler-side rescan / debounce / drift logic depends on
        # the helper returning False for a "nothing terminal" state so
        # the caller can continue with the same `pr`.
        gh = FakeGitHubClient()
        issue = make_issue(311, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=31100, head_branch="orchestrator/issue-311",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 311, 31100)

        result = self._run(
            lambda: self.assertFalse(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="in_review",
                )
            ),
            run_agent=_agent(),
        )

        self.assertEqual(gh.label_history, [])
        self.assertFalse(issue.closed)
        result["_cleanup_terminal_branch"].assert_not_called()
        self.assertEqual(gh.recorded_events, [])

    def test_merged_pr_finalizes_to_done_with_event_and_cleanup(self) -> None:
        # The merged arc: stamp `merged_at`, flip to `done`, emit
        # `pr_merged` with `merge_method="external"` and the supplied
        # stage, close the issue if still open, and run branch cleanup.
        gh = FakeGitHubClient()
        issue = make_issue(312, label="fixing")
        gh.add_issue(issue)
        pr = FakePR(
            number=31200, head_branch="orchestrator/issue-312",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(
            gh, 312, 31200, review_round=2, conflict_round=0,
        )

        result = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="fixing",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((312, "done"), gh.label_history)
        self.assertIn("merged_at", state.data)
        self.assertTrue(issue.closed)
        result["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 312,
        )
        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        ev = merged_events[0]
        self.assertEqual(ev["stage"], "fixing")
        self.assertEqual(ev["pr_number"], 31200)
        self.assertEqual(ev["merge_method"], "external")
        self.assertEqual(ev["sha"], "cafe1234")
        self.assertEqual(ev["review_round"], 2)

    def test_closed_unmerged_pr_finalizes_to_rejected_with_event_and_cleanup(
        self,
    ) -> None:
        # The closed-PR arc: stamp `closed_without_merge_at`, flip to
        # `rejected`, emit `pr_closed_without_merge` with the supplied
        # stage, close the issue if still open, and run branch cleanup.
        # The branch is dead weight once the PR is gone, mirroring the
        # merged-PR cleanup order.
        gh = FakeGitHubClient()
        issue = make_issue(313, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=31300, head_branch="orchestrator/issue-313",
            head=FakePRRef(sha="dead0001"),
            merged=False, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(
            gh, 313, 31300, review_round=3, conflict_round=2,
        )

        result = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr,
                    stage="resolving_conflict",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((313, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", state.data)
        self.assertTrue(issue.closed)
        result["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 313,
        )
        closed_events = [
            e for e in gh.recorded_events
            if e["event"] == "pr_closed_without_merge"
        ]
        self.assertEqual(len(closed_events), 1)
        ev = closed_events[0]
        self.assertEqual(ev["stage"], "resolving_conflict")
        self.assertEqual(ev["pr_number"], 31300)
        self.assertEqual(ev["sha"], "dead0001")
        self.assertEqual(ev["review_round"], 3)
        self.assertEqual(ev["conflict_round"], 2)

    def test_open_pr_with_manually_closed_issue_rejects_without_cleanup(
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
        issue = make_issue(314, label="in_review")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=31400, head_branch="orchestrator/issue-314",
            head=FakePRRef(sha="cafe1234"),
            merged=False, state="open",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 314, 31400)

        result = self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="in_review",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((314, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", state.data)
        # The PR is still open and may be reopened / salvaged, so the
        # branch must survive this exit.
        result["_cleanup_terminal_branch"].assert_not_called()
        # No `pr_closed_without_merge` emit for the open-PR case.
        self.assertEqual(
            [e for e in gh.recorded_events
             if e["event"] == "pr_closed_without_merge"],
            [],
        )
        self.assertEqual(
            [e for e in gh.recorded_events if e["event"] == "pr_merged"],
            [],
        )

    def test_resolving_conflict_terminal_preserves_zero_conflict_round(
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
        issue = make_issue(316, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=31600, head_branch="orchestrator/issue-316",
            head=FakePRRef(sha="feed1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        # Deliberately omit `conflict_round` from the pinned state.
        state = self._state_with_pr_number(gh, 316, 31600)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr,
                    stage="resolving_conflict",
                )
            ),
            run_agent=_agent(),
        )

        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        ev = merged_events[0]
        self.assertEqual(ev["stage"], "resolving_conflict")
        # Field must be present (build_event_record drops None), and
        # the coerced default must be 0.
        self.assertIn("conflict_round", ev)
        self.assertEqual(ev["conflict_round"], 0)

        # Same coercion for the closed-without-merge arc.
        issue2 = make_issue(317, label="resolving_conflict")
        gh.add_issue(issue2)
        pr2 = FakePR(
            number=31700, head_branch="orchestrator/issue-317",
            head=FakePRRef(sha="feed5678"),
            merged=False, state="closed",
        )
        gh.add_pr(pr2)
        state2 = self._state_with_pr_number(gh, 317, 31700)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue2, state2, pr2,
                    stage="resolving_conflict",
                )
            ),
            run_agent=_agent(),
        )

        closed_events = [
            e for e in gh.recorded_events
            if e["event"] == "pr_closed_without_merge"
        ]
        self.assertEqual(len(closed_events), 1)
        ev2 = closed_events[0]
        self.assertIn("conflict_round", ev2)
        self.assertEqual(ev2["conflict_round"], 0)

    def test_in_review_terminal_omits_missing_conflict_round(self) -> None:
        # The other two stages have always passed the raw
        # `state.get("conflict_round")` through, so a missing counter
        # naturally drops out via `build_event_record`. Pin that contract
        # so a future refactor doesn't accidentally start coercing for
        # `in_review` / `fixing` and start emitting a `conflict_round=0`
        # field on states that never had the counter.
        gh = FakeGitHubClient()
        issue = make_issue(318, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=31800, head_branch="orchestrator/issue-318",
            head=FakePRRef(sha="cafe5678"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 318, 31800)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="in_review",
                )
            ),
            run_agent=_agent(),
        )

        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        self.assertNotIn("conflict_round", merged_events[0])

    def test_merged_arc_handles_already_closed_issue_without_re_closing(
        self,
    ) -> None:
        # A `Resolves #N` footer auto-closes the issue the moment the PR
        # merges, so when the closed-issue sweep yields this case the
        # helper sees an already-closed issue. The merged arc still
        # finalizes the label, but must not crash trying to re-close
        # what GitHub already closed.
        gh = FakeGitHubClient()
        issue = make_issue(315, label="fixing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=31500, head_branch="orchestrator/issue-315",
            head=FakePRRef(sha="feed0001"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        state = self._state_with_pr_number(gh, 315, 31500)

        self._run(
            lambda: self.assertTrue(
                workflow._drain_review_pr_terminals(
                    gh, _TEST_SPEC, issue, state, pr, stage="fixing",
                )
            ),
            run_agent=_agent(),
        )

        self.assertIn((315, "done"), gh.label_history)
        self.assertTrue(issue.closed)
        merged_events = [
            e for e in gh.recorded_events if e["event"] == "pr_merged"
        ]
        self.assertEqual(len(merged_events), 1)
        self.assertEqual(merged_events[0]["stage"], "fixing")


if __name__ == "__main__":
    unittest.main()
