# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from tests.workflow_helpers import (
    _FAKE_WT,
    _ResolvingConflictMixin,
    _TEST_SPEC,
    _agent,
)


class ResolvingConflictAgentExecutionTest(
    unittest.TestCase, _ResolvingConflictMixin
):
    """Drive `_handle_resolving_conflict` through the agent-execution
    branches: the dev spawned to resolve a rebase conflict pushes, times
    out, fails to push, or is interrupted mid-flight.
    """

    def test_conflict_resolved_by_agent_pushes_and_flips_to_validating(
        self,
    ) -> None:
        # Agent-resolved conflict push pushes the resolved branch and
        # hands straight back to `validating`. Docs do not run here --
        # the single docs pass runs after reviewer approval before
        # `in_review` via the final-docs handoff.
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py", "b.py"],
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )
        # Agent IS spawned with the conflict-resolution prompt.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("a.py", prompt)
        self.assertIn("b.py", prompt)
        self.assertIn("rebase", prompt.lower())
        self.assertIn("git rebase --skip", prompt)
        self.assertIn("git commit --allow-empty", prompt)
        self.assertIn("git rebase --abort", prompt)
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.BRANCH,
            force_with_lease="beforehead",
        )
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertIn("last_conflict_resolved_at", data)

    def test_agent_timeout_parks_awaiting_human(self) -> None:
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "after"],
            run_agent_result=_agent(
                session_id="dev-sess", last_message="", timed_out=True,
            ),
        )
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        # Label stays on resolving_conflict -- the dispatcher will keep
        # routing here until the operator clears the park.
        self.assertNotIn((200, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("timed out", last_comment)

    def test_push_failure_parks_awaiting_human(self) -> None:
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "merged"],
            push_branch=False,
        )
        # Agent ran successfully and committed, but the push failed.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        mocks["_push_branch"].assert_called_once()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        # No label flip -- still resolving_conflict.
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_conflict_resolution_interrupted_leaves_state_untouched(self) -> None:
        # A dev run spawned to resolve the rebase conflict, but the shutdown
        # sweep killed it mid-flight. The partial result must be ignored:
        # `_post_conflict_resolution_result` returns WITHOUT writing pinned
        # state, so durable state stays retryable -- no park, no flip, no
        # round increment, no push off the partial tree.
        gh, issue, pr = self._seed()
        self._seed_with_baseline_hash(gh, issue)
        before_writes = gh.write_state_calls

        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "after"],
            run_agent_result=_agent(
                session_id="dev-sess", last_message="", interrupted=True,
            ),
        )

        # The conflict-resolution dev run spawned, then was seen interrupted.
        mocks["run_agent"].assert_called_once()
        self.assertEqual(gh.write_state_calls, before_writes)
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertFalse(data.get("awaiting_human"))
        # `conflict_round` not bumped and no flip back to validating.
        self.assertEqual(data.get("conflict_round"), 0)
        self.assertNotIn((200, "validating"), gh.label_history)
        self.assertFalse(any(
            "timed out" in body
            or "rebase is still in progress" in body
            or "agent needs your input" in body
            or "git push failed" in body
            for _, body in gh.posted_comments
        ))


if __name__ == "__main__":
    unittest.main()
