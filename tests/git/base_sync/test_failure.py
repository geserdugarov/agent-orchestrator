# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""How `startup` ends a rebase that failed or left the worktree dirty."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tests.git.base_sync.refresh_scenarios import PUSH_PATCH, _scenario
from tests.git.base_sync.refresh_test_support import (
    AFTER_SHA,
    _git_result,
    _SyncWorktreeWithBaseFixture,
)

ISSUE = 7

BEFORE_SHA = "be40e5ba" * 5

# Awaiting-human park reasons the auto-rebase flow writes.
PARK_DIRTY = "auto_base_rebase_dirty"
PARK_FAILED = "auto_base_rebase_failed"

KEY_AWAITING_HUMAN = "awaiting_human"
KEY_PARK_REASON = "park_reason"

# Git output and commands the scenario assertions match on.
TWO_BEHIND_STDOUT = "2\n"
REBASE_COMMAND = "rebase"
ABORT_FLAG = "--abort"
RESET_COMMAND = "reset"
HARD_RESET_FLAG = "--hard"


class RebaseFailureRoutingUnitTest(_SyncWorktreeWithBaseFixture, unittest.TestCase):
    def test_dirty_after_rebase_resets_and_parks(self) -> None:
        self._seed_pr_issue()
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(side_effect=[[], ["scratch.py"]]),
            rebase=MagicMock(return_value=(True, [])),
            push=MagicMock(),
            head_sha=MagicMock(
                side_effect=[BEFORE_SHA, AFTER_SHA, AFTER_SHA],
            ),
            git=MagicMock(return_value=_git_result(stdout=TWO_BEHIND_STDOUT)),
            hardened=MagicMock(return_value=_git_result()),
        )

        scenario.run(self)

        scenario[PUSH_PATCH].assert_not_called()
        self._assert_hardened_call(
            scenario,
            (RESET_COMMAND, HARD_RESET_FLAG, BEFORE_SHA),
        )
        self._assert_hardened_call(scenario, ("clean", "-fd"))
        self._assert_park(PARK_DIRTY, "uncommitted change")

    def test_pr_rebase_failed_without_conflicts_parks(self) -> None:
        self._seed_pr_issue()
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(return_value=(False, [])),
            push=MagicMock(),
            head_sha=MagicMock(return_value=BEFORE_SHA),
            git=MagicMock(return_value=_git_result(stdout=TWO_BEHIND_STDOUT)),
            hardened=MagicMock(return_value=_git_result()),
        )

        scenario.run(self)

        self._assert_hardened_call(
            scenario,
            (REBASE_COMMAND, ABORT_FLAG),
        )
        scenario[PUSH_PATCH].assert_not_called()
        self._assert_park(PARK_FAILED, "non-conflict reason")

    def _assert_hardened_call(self, scenario, prefix: tuple[str, ...]) -> None:
        matching = [
            recorded_call
            for recorded_call in scenario["hardened"].call_args_list
            if recorded_call.args[: len(prefix)] == prefix
        ]
        self.assertEqual(
            len(matching),
            1,
            scenario["hardened"].call_args_list,
        )

    def _assert_park(self, reason: str, message_fragment: str) -> None:
        self.assertEqual(self.gh.label_history, [])
        state = self.gh.pinned_data(ISSUE)
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        self.assertEqual(state.get(KEY_PARK_REASON), reason)
        self.assertEqual(len(self.gh.posted_comments), 1)
        self.assertIn(message_fragment, self.gh.posted_comments[0][1])


if __name__ == "__main__":
    unittest.main()
