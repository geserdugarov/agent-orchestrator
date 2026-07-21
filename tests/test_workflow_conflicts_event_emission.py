# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from tests.workflow_helpers import (
    EVENT_PR_CLOSED_WITHOUT_MERGE,
    EVENT_PR_MERGED,
    _ResolvingConflictMixin,
)

BEFORE_HEAD = "before"
MERGED_HEAD = "merged"
CONFLICT_ROUND = "conflict_round"


def _events_of(gh, event_name: str) -> list[dict]:
    return [event for event in gh.recorded_events if event["event"] == event_name]


class ResolvingConflictEventEmissionTest(
    unittest.TestCase,
    _ResolvingConflictMixin,
):
    """`_handle_resolving_conflict` emits `merge_attempt` for each base-
    rebase attempt, `conflict_round` whenever the counter ticks, and the
    same `pr_merged` / `pr_closed_without_merge` terminals as in_review.
    """

    def test_clean_rebase_emits_merge_success(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
            head_shas=[BEFORE_HEAD, MERGED_HEAD],
        )
        attempts = _events_of(gh, "merge_attempt")
        self.assertEqual(len(attempts), 1)
        event = attempts[0]
        self.assertEqual(event["stage"], "resolving_conflict")
        self.assertEqual(event["pr_number"], self.pr_number)
        self.assertEqual(event["method"], "base_rebase")
        self.assertEqual(event["result"], "success")
        self.assertEqual(event[CONFLICT_ROUND], 0)

    def test_merge_attempt_conflict_on_unmerged_paths(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh,
            issue,
            merge_succeeded=False,
            conflicted_files=["a.py", "b.py"],
            head_shas=[BEFORE_HEAD, MERGED_HEAD],
        )
        attempts = _events_of(gh, "merge_attempt")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["result"], "conflict")

    def test_clean_rebase_push_bumps_round(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
            head_shas=[BEFORE_HEAD, MERGED_HEAD],
            push_branch=True,
        )
        rounds = [event for event in _events_of(gh, CONFLICT_ROUND) if event["action"] == "incremented"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0][CONFLICT_ROUND], 1)
        self.assertEqual(rounds[0]["outcome"], "base_rebased_clean")
        # SHA is the after-rebase HEAD captured before the push.
        self.assertEqual(rounds[0]["sha"], MERGED_HEAD)

    def test_up_to_date_noop_bumps_round(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
            head_shas=["samehead", "samehead"],
        )
        rounds = [event for event in _events_of(gh, CONFLICT_ROUND) if event["action"] == "incremented"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["outcome"], "base_up_to_date")

    def test_agent_resolution_bumps_round(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh,
            issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=[BEFORE_HEAD, MERGED_HEAD],
            push_branch=True,
        )
        rounds = [event for event in _events_of(gh, CONFLICT_ROUND) if event["action"] == "incremented"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["outcome"], "agent_resolved")

    def test_pr_merged_event_on_external_merge(self) -> None:
        gh, issue, pr = self._seed(pr_state="closed", pr_merged=True)
        self._run_with_merge(gh, issue)
        merged = _events_of(gh, EVENT_PR_MERGED)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["stage"], "resolving_conflict")
        self.assertEqual(merged[0]["pr_number"], self.pr_number)
        # No base rebase attempted on the terminal path.
        self.assertEqual(_events_of(gh, "merge_attempt"), [])

    def test_pr_closed_without_merge_event(self) -> None:
        gh, issue, pr = self._seed(pr_state="closed", pr_merged=False)
        self._run_with_merge(gh, issue)
        closed = _events_of(gh, EVENT_PR_CLOSED_WITHOUT_MERGE)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["stage"], "resolving_conflict")
        self.assertEqual(closed[0]["pr_number"], self.pr_number)


if __name__ == "__main__":
    unittest.main()
