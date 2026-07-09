# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import patch

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


class ResolvingConflictEventEmissionTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """`_handle_resolving_conflict` emits `merge_attempt` for each base-
    rebase attempt, `conflict_round` whenever the counter ticks, and the
    same `pr_merged` / `pr_closed_without_merge` terminals as in_review.
    """

    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-300"
    PR_NUMBER = 900

    @staticmethod
    def _events_of(gh, event_name: str) -> list[dict]:
        return [e for e in gh.recorded_events if e["event"] == event_name]

    def _seed(self, *, pr_state="open", pr_merged=False, extra_state=None):
        gh = FakeGitHubClient()
        issue = make_issue(300, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="feed1234"),
            mergeable=False, check_state="success",
            merged=pr_merged, state=pr_state,
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=2, conflict_round=0,
        )
        if extra_state:
            state.update(extra_state)
        gh.seed_state(300, **state)
        return gh, issue, pr

    def _run_with_merge(
        self, gh, issue, *,
        merge_succeeded=True, conflicted_files=(),
        head_shas=("before", "after"), push_branch=True,
        run_agent_result=None,
    ):
        from unittest.mock import MagicMock

        agent = run_agent_result or _agent(
            session_id="dev-sess", last_message="resolved",
        )
        merge_mock = MagicMock(
            return_value=(merge_succeeded, list(conflicted_files))
        )
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock), \
             patch.object(workflow, "_git", MagicMock(return_value=ok)), \
             patch.object(workflow, "_git_hardened", MagicMock(return_value=ok)):
            return self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=agent,
                push_branch=push_branch,
                head_shas=head_shas,
            )

    def test_merge_attempt_success_on_clean_base_rebase(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=True,
            head_shas=["before", "merged"],
        )
        attempts = self._events_of(gh, "merge_attempt")
        self.assertEqual(len(attempts), 1)
        event = attempts[0]
        self.assertEqual(event["stage"], "resolving_conflict")
        self.assertEqual(event["pr_number"], self.PR_NUMBER)
        self.assertEqual(event["method"], "base_rebase")
        self.assertEqual(event["result"], "success")
        self.assertEqual(event["conflict_round"], 0)

    def test_merge_attempt_conflict_on_unmerged_paths(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=False,
            conflicted_files=["a.py", "b.py"],
            head_shas=["before", "merged"],
        )
        attempts = self._events_of(gh, "merge_attempt")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["result"], "conflict")

    def test_conflict_round_incremented_on_clean_base_rebase_push(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=True,
            head_shas=["before", "merged"], push_branch=True,
        )
        rounds = [
            e for e in self._events_of(gh, "conflict_round")
            if e["action"] == "incremented"
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["conflict_round"], 1)
        self.assertEqual(rounds[0]["outcome"], "base_rebased_clean")
        # SHA is the after-rebase HEAD captured before the push.
        self.assertEqual(rounds[0]["sha"], "merged")

    def test_conflict_round_incremented_on_base_up_to_date_no_op(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=True,
            head_shas=["samehead", "samehead"],
        )
        rounds = [
            e for e in self._events_of(gh, "conflict_round")
            if e["action"] == "incremented"
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["outcome"], "base_up_to_date")

    def test_conflict_round_incremented_after_agent_resolves(self) -> None:
        gh, issue, pr = self._seed()
        self._run_with_merge(
            gh, issue, merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["before", "merged"],
            push_branch=True,
        )
        rounds = [
            e for e in self._events_of(gh, "conflict_round")
            if e["action"] == "incremented"
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["outcome"], "agent_resolved")

    def test_pr_merged_event_on_external_merge(self) -> None:
        gh, issue, pr = self._seed(pr_state="closed", pr_merged=True)
        self._run_with_merge(gh, issue)
        merged = self._events_of(gh, "pr_merged")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["stage"], "resolving_conflict")
        self.assertEqual(merged[0]["pr_number"], self.PR_NUMBER)
        # No base rebase attempted on the terminal path.
        self.assertEqual(self._events_of(gh, "merge_attempt"), [])

    def test_pr_closed_without_merge_event(self) -> None:
        gh, issue, pr = self._seed(pr_state="closed", pr_merged=False)
        self._run_with_merge(gh, issue)
        closed = self._events_of(gh, "pr_closed_without_merge")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["stage"], "resolving_conflict")
        self.assertEqual(closed[0]["pr_number"], self.PR_NUMBER)


if __name__ == "__main__":
    unittest.main()
