# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.workflow_helpers import (
    _FAKE_WT,
    _ResolvingConflictMixin,
    _TEST_SPEC,
    _agent,
)

CONFLICT_ISSUE = 200
CONFLICT_FILE = "a.py"
BEFORE_HEAD = "beforehead"
RUN_AGENT = "run_agent"
PUSH_BRANCH = "_push_branch"
LABEL_VALIDATING = "validating"


def _assert_resolution_prompt(test_case, prompt: str) -> None:
    test_case.assertIn(CONFLICT_FILE, prompt)
    test_case.assertIn("b.py", prompt)
    test_case.assertIn("rebase", prompt.lower())
    test_case.assertIn("git rebase --skip", prompt)
    test_case.assertIn("git commit --allow-empty", prompt)
    test_case.assertIn("git rebase --abort", prompt)


def _assert_resolved_state(test_case, github) -> None:
    pinned_state = github.pinned_data(CONFLICT_ISSUE)
    test_case.assertEqual(pinned_state.get("review_round"), 0)
    test_case.assertEqual(pinned_state.get("conflict_round"), 1)
    test_case.assertIn("last_conflict_resolved_at", pinned_state)


def _assert_interrupted_state(test_case, github) -> None:
    pinned_state = github.pinned_data(CONFLICT_ISSUE)
    test_case.assertFalse(pinned_state.get("awaiting_human"))
    test_case.assertEqual(pinned_state.get("conflict_round"), 0)
    test_case.assertNotIn((CONFLICT_ISSUE, LABEL_VALIDATING), github.label_history)
    test_case.assertFalse(
        any(
            "timed out" in body
            or "rebase is still in progress" in body
            or "agent needs your input" in body
            or "git push failed" in body
            for _, body in github.posted_comments
        )
    )


class ResolvingConflictAgentExecutionTest(unittest.TestCase, _ResolvingConflictMixin):
    """Drive `_handle_resolving_conflict` through the agent-execution
    branches: the dev spawned to resolve a rebase conflict pushes, times
    out, fails to push, or is interrupted mid-flight.
    """

    def test_resolved_pushes_and_routes_to_validating(self) -> None:
        # Agent-resolved conflict push pushes the resolved branch and
        # hands straight back to `validating`. Docs do not run here --
        # the single docs pass runs after reviewer approval before
        # `in_review` via the final-docs handoff.
        gh, issue = self._seed()[:2]
        mocks = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=False,
            conflicted_files=[CONFLICT_FILE, "b.py"],
            head_shas=[BEFORE_HEAD, "merged"],
            push_branch=True,
        )[0]
        # Agent IS spawned with the conflict-resolution prompt.
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        prompt = mocks[RUN_AGENT].call_args.args[1]
        _assert_resolution_prompt(self, prompt)
        mocks[PUSH_BRANCH].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.issue_branch,
            force_with_lease=BEFORE_HEAD,
        )
        self.assertIn((CONFLICT_ISSUE, LABEL_VALIDATING), gh.label_history)
        self.assertNotIn((CONFLICT_ISSUE, "documenting"), gh.label_history)
        _assert_resolved_state(self, gh)

    def test_agent_timeout_parks_awaiting_human(self) -> None:
        gh, issue, _ = self._seed()
        mocks, _, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=False,
            conflicted_files=[CONFLICT_FILE],
            head_shas=[BEFORE_HEAD, "after"],
            run_agent_result=_agent(
                session_id="dev-sess",
                last_message="",
                timed_out=True,
            ),
        )
        mocks[PUSH_BRANCH].assert_not_called()
        pinned_state = gh.pinned_data(CONFLICT_ISSUE)
        self.assertTrue(pinned_state.get("awaiting_human"))
        # Label stays on resolving_conflict -- the dispatcher will keep
        # routing here until the operator clears the park.
        self.assertNotIn((CONFLICT_ISSUE, LABEL_VALIDATING), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("timed out", last_comment)

    def test_push_failure_parks_awaiting_human(self) -> None:
        gh, issue, _ = self._seed()
        mocks, _, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=False,
            conflicted_files=[CONFLICT_FILE],
            head_shas=[BEFORE_HEAD, "merged"],
            push_branch=False,
        )
        # Agent ran successfully and committed, but the push failed.
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        mocks[PUSH_BRANCH].assert_called_once()
        pinned_state = gh.pinned_data(CONFLICT_ISSUE)
        self.assertTrue(pinned_state.get("awaiting_human"))
        # No label flip -- still resolving_conflict.
        self.assertNotIn((CONFLICT_ISSUE, LABEL_VALIDATING), gh.label_history)

    def test_interrupted_resolution_keeps_state(self) -> None:
        # A dev run spawned to resolve the rebase conflict, but the shutdown
        # sweep killed it mid-flight. The partial result must be ignored:
        # `_post_conflict_resolution_result` returns WITHOUT writing pinned
        # state, so durable state stays retryable -- no park, no flip, no
        # round increment, no push off the partial tree.
        gh, issue, _ = self._seed()
        self._seed_with_baseline_hash(gh, issue)
        before_writes = gh.write_state_calls

        mocks, _, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=False,
            conflicted_files=[CONFLICT_FILE],
            head_shas=[BEFORE_HEAD, "after"],
            run_agent_result=_agent(
                session_id="dev-sess",
                last_message="",
                interrupted=True,
            ),
        )

        # The conflict-resolution dev run spawned, then was seen interrupted.
        mocks[RUN_AGENT].assert_called_once()
        self.assertEqual(gh.write_state_calls, before_writes)
        mocks[PUSH_BRANCH].assert_not_called()
        _assert_interrupted_state(self, gh)


if __name__ == "__main__":
    unittest.main()
