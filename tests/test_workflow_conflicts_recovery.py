# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow

from tests.workflow_helpers import (
    _ResolvingConflictMixin,
    _agent,
)

CONFLICT_ISSUE = 200


def _assert_completed_round(test_case, github) -> None:
    state = github.pinned_data(CONFLICT_ISSUE)
    test_case.assertEqual(state.get("review_round"), 0)
    test_case.assertEqual(state.get("conflict_round"), 1)
    test_case.assertIn("last_conflict_resolved_at", state)


def _assert_combined_round_event(test_case, github) -> None:
    rounds = [
        event
        for event in github.recorded_events
        if event.get("event") == "conflict_round" and event.get("action") == "incremented"
    ]
    test_case.assertEqual(len(rounds), 1)
    test_case.assertEqual(rounds[0].get("outcome"), "base_rebased_clean")


class ResolvingConflictRecoveryPushTest(unittest.TestCase, _ResolvingConflictMixin):
    """Drive `_handle_resolving_conflict` through the crash-recovery push
    branches: an unpushed local commit ships on the next tick, a failed
    recovery push parks, and a recovered push onto a stale base falls
    through to the rebase path for a single combined round.
    """

    def test_recovery_pushes_local_commits(self) -> None:
        # Crash recovery: a previous tick committed a conflict resolution
        # but crashed before `_push_branch` returned (or before the post-
        # push state write landed). The next tick must push the local
        # commit and complete the round, NOT treat it as "no work needed"
        # and flip to validating with the resolution unpushed.
        gh, issue, _ = self._seed()

        merge_mock = MagicMock(return_value=(True, []))
        # After the recovered push the handler probes whether the
        # worktree is still behind base via `git rev-list --count
        # HEAD..origin/<base>`. The crash-recovery scenario this test
        # exercises has HEAD already on base, so the probe returns 0
        # and the handler takes the fast path to validating without a
        # follow-up rebase.
        git_on_base = MagicMock(
            return_value=MagicMock(returncode=0, stdout="0\n", stderr=""),
        )

        with (
            patch.object(workflow, "_rebase_base_into_worktree", merge_mock),
            patch.object(workflow, "_git", git_on_base),
        ):
            mocks = self._run_resolving_conflict(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
                # HEAD ahead of `origin/<branch>` by one commit (the
                # unpushed resolution); not behind.
                branch_ahead_behind=(1, 0),
            )
        # Recovered work pushed; rebase NOT attempted (we already have a
        # resolution waiting to ship).
        mocks["_push_branch"].assert_called_once()
        merge_mock.assert_not_called()
        # No agent spawn -- the recovery is a pure push, the dev already
        # produced the commit on the previous tick.
        mocks["run_agent"].assert_not_called()
        # Round completed: counter incremented, label flipped, marker
        # stamped exactly as on the happy-path resolve. The recovered
        # push hands straight back to `validating`; the single docs
        # pass is deferred to the post-approval hop.
        _assert_completed_round(self, gh)
        self.assertIn((CONFLICT_ISSUE, "validating"), gh.label_history)
        self.assertNotIn((CONFLICT_ISSUE, "documenting"), gh.label_history)

    def test_unpushed_recovery_push_failure_parks(self) -> None:
        # Recovery push fails (e.g. force-with-lease lease miss because
        # the remote actually moved). Park rather than silently flipping
        # to validating with an unsynced local SHA.
        gh, issue, _ = self._seed()

        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run_resolving_conflict(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=False,
                branch_ahead_behind=(1, 0),
            )
        mocks["_push_branch"].assert_called_once()
        merge_mock.assert_not_called()
        self.assertTrue(gh.pinned_data(CONFLICT_ISSUE).get("awaiting_human"))
        self.assertNotIn((CONFLICT_ISSUE, "validating"), gh.label_history)

    def test_stale_base_falls_through_to_rebase(self) -> None:
        # The `fixing` drift router
        # (`_reconcile_parked_fixing`) reroutes here
        # when a stuck `push_failed` / `agent_timeout` park has
        # UNPUSHED FIX COMMITS on a base that has since advanced. The
        # recovered-push fast path would publish the fix to the PR
        # branch and flip straight to `validating` -- but the branch
        # is still behind base. Probe behind-base after the push and
        # fall through to the rebase path so the same tick integrates
        # base and consumes exactly ONE `conflict_round` for the
        # combined push+rebase reconciliation. Without this, the PR
        # would be republished still-behind-base and the round counter
        # would burn a slot toward `MAX_CONFLICT_ROUNDS` without ever
        # attempting the base rebase the reroute was meant to perform.
        gh, issue, _ = self._seed()

        # Clean rebase that actually moved HEAD (recovered push +
        # rebase pushes a different SHA than the recovered SHA).
        merge_mock = MagicMock(return_value=(True, []))
        # Probe says still 2 commits behind base after the recovered
        # push, forcing the fall-through.
        git_behind_base = MagicMock(
            return_value=MagicMock(returncode=0, stdout="2\n", stderr=""),
        )

        with (
            patch.object(workflow, "_rebase_base_into_worktree", merge_mock),
            patch.object(workflow, "_git", git_behind_base),
        ):
            mocks = self._run_resolving_conflict(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
                # Recovered push first (force-with-lease=None on a
                # straight-ahead push), then the rebased-head push
                # (force-with-lease=before_sha). The handler also reads
                # HEAD for the round-emit on success, so feed enough
                # SHAs through `_head_sha` for both the rebase-path's
                # before/after compare and the audit emit.
                branch_ahead_behind=(1, 0),
                head_shas=["before", "after", "after"],
            )

        # Both the recovered push AND the rebased-head push fired this
        # tick; the merge attempt ran in between.
        self.assertEqual(mocks["_push_branch"].call_count, 2)
        merge_mock.assert_called_once()
        # No agent spawn -- the rebase was clean.
        mocks["run_agent"].assert_not_called()
        # Single conflict_round increment for the combined push+rebase
        # reconciliation, NOT one per push.
        _assert_completed_round(self, gh)
        # The combined round outcome is the rebase path's
        # `base_rebased_clean`, not the fast-path `recovered_push`.
        _assert_combined_round_event(self, gh)
        # Hand back to validating after the rebase landed.
        self.assertIn((CONFLICT_ISSUE, "validating"), gh.label_history)
        self.assertNotIn((CONFLICT_ISSUE, "documenting"), gh.label_history)


if __name__ == "__main__":
    unittest.main()
