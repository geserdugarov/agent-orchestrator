# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The three ways an interrupted auto-rebase reaches `validating` again."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tests.git.base_sync.refresh_scenarios import (
    PUSH_PATCH,
    REBASE_PATCH,
    _scenario,
)
from tests.git.base_sync.refresh_test_support import (
    AFTER_SHA,
    REBASED_SHA,
    _diverged,
    _git_result,
    _pending_attempt,
    _RemoteHeadGit,
    _SyncWorktreeWithBaseFixture,
)

ISSUE = 7

# Worktree HEAD SHAs threaded through the rebase / push / recovery flows.
# The post-rebase and recovered heads come off the shared fixture, where they
# are the commit the size gate proves the checkout to.
BEFORE_SHA = "be40e5ba" * 5

LABEL_VALIDATING = "workflow:validating"

EVENT_BASE_REBASED = "base_rebased"

KEY_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"
KEY_REVIEW_ROUND = "review_round"

TWO_BEHIND_STDOUT = "2\n"
UP_TO_DATE_STDOUT = "0\n"
FORCE_WITH_LEASE_KWARG = "force_with_lease"
EVENT_FIELD = "event"
SHA_FIELD = "sha"
METHOD_FIELD = "method"


class CrashRecoverySuccessUnitTest(_SyncWorktreeWithBaseFixture, unittest.TestCase):
    def test_pr_crash_recovery_pushes_unpushed_rebase(self) -> None:
        self._seed_pr_issue(
            pending_auto_base_rebase_push_sha=BEFORE_SHA,
        )
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(),
            head_sha=MagicMock(return_value=REBASED_SHA),
            ahead_behind=MagicMock(return_value=_diverged(1, 0)),
            fetch=MagicMock(return_value=_git_result()),
            push=MagicMock(return_value=True),
            git=MagicMock(
                return_value=_git_result(stdout=UP_TO_DATE_STDOUT),
            ),
            hardened=MagicMock(side_effect=_RemoteHeadGit(BEFORE_SHA)),
        )

        scenario.run(self)

        scenario[PUSH_PATCH].assert_called_once()
        self.assertEqual(
            scenario[PUSH_PATCH].call_args.kwargs.get(FORCE_WITH_LEASE_KWARG),
            BEFORE_SHA,
        )
        scenario[REBASE_PATCH].assert_not_called()
        self.assertEqual(
            self.gh.pinned_data(ISSUE).get(KEY_REVIEW_ROUND),
            0,
        )
        self._assert_recovery_event(
            "crash_recovery_pushed",
            expected_sha=REBASED_SHA,
        )

    def test_crash_recovery_finishes_landed_push(self) -> None:
        self._seed_pr_issue(
            **_pending_attempt(REBASED_SHA),
            review_round=3,
        )
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(),
            head_sha=MagicMock(return_value=REBASED_SHA),
            ahead_behind=MagicMock(return_value=_diverged(0, 0)),
            fetch=MagicMock(return_value=_git_result()),
            push=MagicMock(),
            git=MagicMock(
                return_value=_git_result(stdout=UP_TO_DATE_STDOUT),
            ),
            hardened=MagicMock(side_effect=_RemoteHeadGit(REBASED_SHA)),
        )

        scenario.run(self)

        scenario[PUSH_PATCH].assert_not_called()
        scenario[REBASE_PATCH].assert_not_called()
        self.assertEqual(
            self.gh.pinned_data(ISSUE).get(KEY_REVIEW_ROUND),
            0,
        )
        self._assert_recovery_event("crash_recovery_relabel_only")

    def test_crash_recovery_clears_same_head_flag(self) -> None:
        self._seed_pr_issue(
            pending_auto_base_rebase_push_sha=BEFORE_SHA,
        )
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(return_value=(True, [])),
            # The recovery's unchanged reading, the anchor the fresh
            # rebase behind it pins, the replay that attempt records, and the
            # head its publication names.
            head_sha=MagicMock(
                side_effect=[BEFORE_SHA, BEFORE_SHA, AFTER_SHA, AFTER_SHA],
            ),
            fetch=MagicMock(return_value=_git_result()),
            push=MagicMock(return_value=True),
            git=MagicMock(
                return_value=_git_result(stdout=TWO_BEHIND_STDOUT),
            ),
        )

        scenario.run(self)

        scenario[REBASE_PATCH].assert_called_once()
        scenario[PUSH_PATCH].assert_called_once()
        self._assert_recovery_event("auto_clean_rebase")

    def _assert_recovery_event(
        self,
        method: str,
        *,
        expected_sha: str | None = None,
    ) -> None:
        self.assertIn((ISSUE, LABEL_VALIDATING), self.gh.label_history)
        state = self.gh.pinned_data(ISSUE)
        self.assertIsNone(state.get(KEY_PENDING_PUSH_SHA))
        events = []
        for event in self.gh.recorded_events:
            if event.get(EVENT_FIELD) == EVENT_BASE_REBASED:
                events.append(event)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get(METHOD_FIELD), method)
        if expected_sha is not None:
            self.assertEqual(events[0].get(SHA_FIELD), expected_sha)


if __name__ == "__main__":
    unittest.main()
