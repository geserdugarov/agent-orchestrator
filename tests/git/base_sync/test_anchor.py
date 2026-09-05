# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The pre-rebase anchor `startup` pins, and the parks that clear it."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tests.git.base_sync.anchor_assertions import (
    _assert_hardened_calls,
    _assert_not_called,
    _assert_parked_without_anchor,
)
from tests.git.base_sync.refresh_scenarios import (
    PUSH_PATCH,
    REBASE_PATCH,
    _clean_rebase_scenario,
    _scenario,
)
from tests.git.base_sync.refresh_test_support import (
    AFTER_SHA,
    REBASED_SHA,
    _diverged,
    _git_result,
    _RebaseAnchorRecorder,
    _RemoteHeadGit,
    _SyncWorktreeWithBaseFixture,
)

ISSUE = 7

# Worktree HEAD SHAs threaded through the rebase / push / recovery flows.
# The post-rebase and recovered heads come off the shared fixture, where they
# are the commit the size gate proves the checkout to: the refresh names what
# it means to publish and the gate refuses a checkout standing anywhere else.
BEFORE_SHA = "be40e5ba" * 5

LABEL_RESOLVING_CONFLICT = "workflow:resolving_conflict"

# The anchor an attempt pinned before an operator moved the issue off the
# refresh-driven set, and the head its checkout is still standing on.
STALE_ANCHOR = "57a1ea11" * 5

# Awaiting-human park reasons the auto-rebase flow writes.
PARK_PUSH_FAILED = "auto_base_rebase_push_failed"
PARK_DIRTY = "auto_base_rebase_dirty"
PARK_FAILED = "auto_base_rebase_failed"

KEY_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"

# Git output and commands the scenario assertions match on.
THREE_BEHIND_STDOUT = "3\n"
TWO_BEHIND_STDOUT = "2\n"
UP_TO_DATE_STDOUT = "0\n"
RESET_COMMAND = "reset"
HARD_RESET_FLAG = "--hard"


class CrashRecoveryAnchorUnitTest(_SyncWorktreeWithBaseFixture, unittest.TestCase):
    def test_normal_rebase_sets_then_clears_anchor(self) -> None:
        self._seed_pr_issue(review_round=2)
        self._add_pr()
        rebase = _RebaseAnchorRecorder(self.gh)
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(side_effect=rebase),
            push=MagicMock(return_value=True),
            head_sha=MagicMock(
                side_effect=[BEFORE_SHA, AFTER_SHA, AFTER_SHA],
            ),
            git=MagicMock(
                return_value=_git_result(stdout=THREE_BEHIND_STDOUT),
            ),
        )

        scenario.run(self)

        self.assertEqual(rebase.observed, [BEFORE_SHA])
        self.assertIsNone(
            self.gh.pinned_data(ISSUE).get(KEY_PENDING_PUSH_SHA),
        )

    def test_push_failure_clears_recovery_anchor(self) -> None:
        self._seed_pr_issue()
        self._add_pr()
        scenario = _clean_rebase_scenario(push_result=False)

        scenario.run(self)

        _assert_parked_without_anchor(
            self,
            self,
            PARK_PUSH_FAILED,
        )

    def test_unreadable_pre_head_parks_without_rebase(self) -> None:
        self._seed_pr_issue()
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(),
            push=MagicMock(),
            head_sha=MagicMock(return_value=""),
            git=MagicMock(
                return_value=_git_result(stdout=THREE_BEHIND_STDOUT),
            ),
        )

        scenario.run(self)

        _assert_not_called(self, scenario, REBASE_PATCH, PUSH_PATCH)
        _assert_parked_without_anchor(
            self,
            self,
            PARK_FAILED,
            message_fragment="HEAD",
        )

    def test_unreadable_post_head_resets_and_parks(self) -> None:
        self._seed_pr_issue()
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(return_value=(True, [])),
            push=MagicMock(),
            head_sha=MagicMock(side_effect=[BEFORE_SHA, "", ""]),
            git=MagicMock(
                return_value=_git_result(stdout=TWO_BEHIND_STDOUT),
            ),
            hardened=MagicMock(return_value=_git_result()),
        )

        scenario.run(self)

        _assert_hardened_calls(
            self,
            scenario,
            (RESET_COMMAND, HARD_RESET_FLAG, BEFORE_SHA),
        )
        _assert_not_called(self, scenario, PUSH_PATCH)
        _assert_parked_without_anchor(self, self, PARK_FAILED)

    def test_crashed_rebase_dirty_reaches_recovery(self) -> None:
        self._seed_pr_issue(
            pending_auto_base_rebase_push_sha=BEFORE_SHA,
        )
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=["scratch.py"]),
            rebase=MagicMock(),
            head_sha=MagicMock(return_value=REBASED_SHA),
            ahead_behind=MagicMock(return_value=_diverged(1, 0)),
            fetch=MagicMock(return_value=_git_result()),
            push=MagicMock(),
            git=MagicMock(
                return_value=_git_result(stdout=UP_TO_DATE_STDOUT),
            ),
            hardened=MagicMock(side_effect=_RemoteHeadGit(BEFORE_SHA)),
        )

        scenario.run(self)

        _assert_hardened_calls(
            self,
            scenario,
            (RESET_COMMAND, HARD_RESET_FLAG, BEFORE_SHA),
            ("clean", "-fd"),
        )
        _assert_not_called(self, scenario, PUSH_PATCH)
        _assert_parked_without_anchor(self, self, PARK_DIRTY)

    def test_stale_anchor_cleared_after_label_exit(self) -> None:
        # The checkout is still standing on the anchor, so nothing this
        # attempt could have left is stranded by dropping it.
        self._seed_pr_issue(
            label=LABEL_RESOLVING_CONFLICT,
            pending_auto_base_rebase_push_sha=STALE_ANCHOR,
        )
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(),
            push=MagicMock(),
            head_sha=MagicMock(return_value=STALE_ANCHOR),
            ahead_behind=MagicMock(),
            fetch=MagicMock(),
            git=MagicMock(
                return_value=_git_result(stdout=THREE_BEHIND_STDOUT),
            ),
        )

        scenario.run(self)

        _assert_not_called(
            self,
            scenario,
            REBASE_PATCH,
            PUSH_PATCH,
            "fetch",
            "ahead_behind",
        )
        self.assertIsNone(
            self.gh.pinned_data(ISSUE).get(KEY_PENDING_PUSH_SHA),
        )


if __name__ == "__main__":
    unittest.main()
