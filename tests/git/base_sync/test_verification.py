# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Recovery exits that cannot confirm what the remote PR branch carries."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tests.git.base_sync.gate_reads_support import _gate_candidates
from tests.git.base_sync.refresh_scenarios import PUSH_PATCH, REBASE_PATCH, _scenario
from tests.git.base_sync.refresh_test_support import (
    PR_NUMBER,
    _CrashRecoveryVerificationFixture,
    _diverged,
    _git_result,
    _pending_attempt,
    _RemoteHeadGit,
    _SyncWorktreeWithBaseFixture,
)
from tests.support.fakes import FakePRRef
from tests.support.publication import LandingPush

ISSUE = 7

# Worktree HEAD SHAs threaded through the rebase / recovery flows.
BEFORE_SHA = "be40e5ba" * 5
# Whole object ids: the head a rebase leaves is what the next push is
# pinned to, and a commit field is read at its exact length.
REBASED_SHA = "5eba5ed0" * 5
NEW_REBASED_SHA = "9ea5eba1" * 5

LABEL_VALIDATING = "workflow:validating"

PARK_PUSH_FAILED = "auto_base_rebase_push_failed"

# Pinned-state field keys read back from `gh.pinned_data(...)`.
KEY_AWAITING_HUMAN = "awaiting_human"
KEY_PARK_REASON = "park_reason"
KEY_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"
KEY_REVIEW_ROUND = "review_round"

# Git output, command, and event fields the assertions match on.
TWO_BEHIND_STDOUT = "2\n"
UP_TO_DATE_STDOUT = "0\n"
RESET_COMMAND = "reset"
HARD_RESET_FLAG = "--hard"
FORCE_WITH_LEASE_KWARG = "force_with_lease"
EVENT_BASE_REBASED = "base_rebased"
EVENT_FIELD = "event"
SHA_FIELD = "sha"
METHOD_FIELD = "method"
GIT_FAILURE_EXIT_CODE = 128


class CrashRecoveryVerificationUnitTest(
    _CrashRecoveryVerificationFixture,
    unittest.TestCase,
):
    def test_pr_crash_recovery_parks_on_fetch_failure(
        self,
    ) -> None:
        # Regression: the `_authed_fetch` failure path used to
        # `return True` without parking, letting the same-tick
        # validating / in_review / fixing / documenting handler run
        # against a local SHA that recovery had NOT verified is on
        # the PR. The fix resets HEAD to the pre-rebase anchor and
        # parks awaiting human so handler dispatch short-circuits.
        hardened_mock, push_mock, merge_mock = self._run_unverifiable_recovery(
            fetch_returncode=GIT_FAILURE_EXIT_CODE,
        )
        self._assert_recovery_unverified_reset_and_park(
            hardened_mock,
            push_mock,
            merge_mock,
        )

    def test_crash_recovery_parks_on_rev_parse_error(
        self,
    ) -> None:
        # Same regression for the `rev-parse` failure path. Without
        # the park, `validating` could read the rebased local HEAD
        # (which may not be on the PR) and stamp its review against
        # a SHA the human-merge gate cannot match.
        hardened_mock, push_mock, merge_mock = self._run_unverifiable_recovery(
            rev_parse_returncode=GIT_FAILURE_EXIT_CODE,
            rev_parse_stdout="",
        )
        self._assert_recovery_unverified_reset_and_park(
            hardened_mock,
            push_mock,
            merge_mock,
        )

    def test_crash_recovery_parks_on_empty_remote(
        self,
    ) -> None:
        # `rev-parse` returncode 0 but empty stdout -- same threat
        # model, same fix.
        hardened_mock, push_mock, merge_mock = self._run_unverifiable_recovery(
            rev_parse_stdout="\n",
        )
        self._assert_recovery_unverified_reset_and_park(
            hardened_mock,
            push_mock,
            merge_mock,
        )

    def test_crash_recovery_parks_on_zero_mismatch(
        self,
    ) -> None:
        # The fourth cannot-verify path: rev-parse returns a DIFFERENT
        # SHA than local HEAD AND `_branch_ahead_behind` returns
        # `(0, 0)` (which now necessarily means a stale remote-
        # tracking ref since the SHA inequality has ruled out the
        # legitimate in-sync case). Reset + park, same as the other
        # three.
        hardened_mock, push_mock, merge_mock = self._run_unverifiable_recovery(
            rev_parse_stdout="foreign-sha\n",
            ahead_behind=(0, 0),
        )
        self._assert_recovery_unverified_reset_and_park(
            hardened_mock,
            push_mock,
            merge_mock,
        )


class CrashRecoveryDivergenceUnitTest(
    _SyncWorktreeWithBaseFixture,
    unittest.TestCase,
):
    def test_landed_push_behind_falls_through(self) -> None:
        self._seed_pr_issue(
            **_pending_attempt(REBASED_SHA),
            review_round=3,
        )
        # The crashed tick's own push landed, so the pull request is already
        # standing on the rebased head the fall-through leases against.
        self._add_pr(head=FakePRRef(sha=REBASED_SHA))
        scenario = self._fallthrough_scenario(REBASED_SHA)
        # The rebase behind the relabel-only recovery is the only push here,
        # and the commit it publishes IS the head the finalize names: one read
        # of one worktree, so a fixture spelling them apart would model the
        # race rather than the tick.
        _gate_candidates(self, NEW_REBASED_SHA)

        scenario.run(self)

        self._assert_fallthrough_publication(scenario, [REBASED_SHA])
        self._assert_fallthrough_state(
            ["crash_recovery_relabel_only", "auto_clean_rebase"],
        )

    def test_pending_push_behind_falls_through(self) -> None:
        self._seed_pr_issue(
            pending_auto_base_rebase_push_sha=BEFORE_SHA,
            review_round=3,
        )
        self._add_pr()
        scenario = self._fallthrough_scenario(
            "old-remote-sha",
            ahead_behind=(1, 0),
            # The recovered push lands, so the pull request stands on what it
            # published when the rebase behind it is leased against the head
            # that push left.
            push=LandingPush(self.gh, PR_NUMBER),
        )
        # Two pushes, two commits: the recovered head, then the one the
        # rebase behind it rewrote that into. Publishing the same commit
        # twice is a receipt the gate recognizes rather than a second push.
        # The recovered push reads twice -- the measurement, then the proof
        # that the checkout is still on what went out -- and the rebase
        # behind that is what moves the head. Each reading is the commit its
        # caller named, because both callers name the head they read out of
        # this one checkout.
        _gate_candidates(
            self, REBASED_SHA, REBASED_SHA, NEW_REBASED_SHA,
        )

        scenario.run(self)

        self._assert_fallthrough_publication(
            scenario,
            [BEFORE_SHA, REBASED_SHA],
        )
        self._assert_fallthrough_state(
            ["crash_recovery_pushed", "auto_clean_rebase"],
        )

    def test_crash_divergence_resets_and_parks(self) -> None:
        self._seed_pr_issue(
            pending_auto_base_rebase_push_sha=BEFORE_SHA,
        )
        self._add_pr()
        scenario = _scenario(
            dirty=MagicMock(return_value=[]),
            rebase=MagicMock(),
            head_sha=MagicMock(return_value=REBASED_SHA),
            ahead_behind=MagicMock(return_value=_diverged(1, 1)),
            fetch=MagicMock(return_value=_git_result()),
            push=MagicMock(),
            git=MagicMock(
                return_value=_git_result(stdout=UP_TO_DATE_STDOUT),
            ),
            hardened=MagicMock(
                side_effect=_RemoteHeadGit("foreign-sha"),
            ),
        )

        scenario.run(self)

        self._assert_divergence(scenario)

    def _fallthrough_scenario(
        self,
        remote_head: str,
        *,
        ahead_behind: tuple[int, int] | None = None,
        push=None,
    ):
        patches = {
            "dirty": MagicMock(return_value=[]),
            REBASE_PATCH: MagicMock(return_value=(True, [])),
            "head_sha": MagicMock(
                side_effect=[
                    REBASED_SHA, REBASED_SHA, NEW_REBASED_SHA, NEW_REBASED_SHA,
                ],
            ),
            "fetch": MagicMock(return_value=_git_result()),
            PUSH_PATCH: (
                MagicMock(side_effect=push) if push
                else MagicMock(return_value=True)
            ),
            "git": MagicMock(
                return_value=_git_result(stdout=TWO_BEHIND_STDOUT),
            ),
            "hardened": MagicMock(
                side_effect=_RemoteHeadGit(remote_head),
            ),
        }
        if ahead_behind is not None:
            patches["ahead_behind"] = MagicMock(
                return_value=_diverged(*ahead_behind),
            )
        return _scenario(**patches)

    def _assert_fallthrough_publication(
        self,
        scenario,
        expected_leases: list[str],
    ) -> None:
        scenario[REBASE_PATCH].assert_called_once()
        self.assertEqual(
            scenario[PUSH_PATCH].call_count,
            len(expected_leases),
        )
        leases = [
            recorded_call.kwargs.get(FORCE_WITH_LEASE_KWARG) for recorded_call in scenario[PUSH_PATCH].call_args_list
        ]
        self.assertEqual(leases, expected_leases)

    def _assert_fallthrough_state(self, expected_methods: list[str]) -> None:
        self.assertIn((ISSUE, LABEL_VALIDATING), self.gh.label_history)
        state = self.gh.pinned_data(ISSUE)
        self.assertIsNone(state.get(KEY_PENDING_PUSH_SHA))
        self.assertEqual(state.get(KEY_REVIEW_ROUND), 0)
        events = []
        for event in self.gh.recorded_events:
            if event.get(EVENT_FIELD) == EVENT_BASE_REBASED:
                events.append(event)
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [recorded_event.get(METHOD_FIELD) for recorded_event in events],
            expected_methods,
        )
        self.assertEqual(events[1].get(SHA_FIELD), NEW_REBASED_SHA)

    def _assert_divergence(self, scenario) -> None:
        reset_calls = []
        for recorded_call in scenario["hardened"].call_args_list:
            if recorded_call.args[:3] == (
                RESET_COMMAND,
                HARD_RESET_FLAG,
                BEFORE_SHA,
            ):
                reset_calls.append(recorded_call)
        self.assertEqual(len(reset_calls), 1)
        scenario[PUSH_PATCH].assert_not_called()
        scenario[REBASE_PATCH].assert_not_called()
        self.assertEqual(self.gh.label_history, [])
        state = self.gh.pinned_data(ISSUE)
        self.assertIsNone(state.get(KEY_PENDING_PUSH_SHA))
        self.assertTrue(state.get(KEY_AWAITING_HUMAN))
        self.assertEqual(state.get(KEY_PARK_REASON), PARK_PUSH_FAILED)


if __name__ == "__main__":
    unittest.main()
