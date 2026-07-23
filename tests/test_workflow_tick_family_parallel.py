# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Family-aware workflow tick scheduling tests."""
from __future__ import annotations

import unittest

import threading
from unittest.mock import patch

from orchestrator import workflow

from tests import workflow_tick_parallel_test_support as support
from tests import workflow_tick_probe_test_support as probes
from tests import workflow_tick_family_test_support as family_support


class TickFamilySchedulingTest(unittest.TestCase):
    """Family-aware work serializes internally while fanout stays parallel."""

    def test_family_aware_stages_never_overlap(self) -> None:
        # Family-aware labels (decomposing, blocked, umbrella, and unlabeled
        # pickup) write across parent/child boundaries -- the parent's
        # `_handle_decomposing` recovery seeds `parent_number` on each
        # recorded child, while `_handle_blocked` would otherwise park the
        # same child as `blocked_no_children`. Running two of these
        # concurrently raced the writes (the child's late
        # `awaiting_human=True` write clobbered the parent's just-seeded
        # `parent_number`). `tick()` must therefore hold a tick-local
        # lock around the family-aware handlers so no two run at the same
        # time -- AND must let non-family-aware workers run alongside,
        # so a slow decomposing handler does not block unrelated
        # implementing / validating work in the same tick.
        #
        # `ready` is deliberately NOT family-aware (it only writes its own
        # state and recurses into `_handle_implementing`) -- the separate
        # `test_ready_issues_fan_out_concurrently` test pins that
        # contract down.
        gh = support.FakeGitHubClient()
        family_support._seed_overlap_issues(gh)

        probe = probes._FamilyOverlapProbe(fanout_issue=support._FANOUT_ISSUE_NUMBER)

        # parallel_limit=5 and no `global_semaphore` means every submission
        # gets its own worker thread; the family lock is the ONLY thing
        # preventing family-aware handlers from overlapping with each
        # other, and the fanout worker is free to run alongside whichever
        # family handler currently holds the lock.
        with patch.object(workflow, support.REFRESH_BASE), \
             patch.object(workflow, support.PROCESS_ISSUE, side_effect=probe):
            workflow.tick(gh, support._spec(parallel_limit=5))

        # Four family-aware issues observed; the family lock kept them
        # from overlapping with each other.
        self.assertEqual(probe.family_count, 4)
        self.assertEqual(probe.family_max_in_flight, 1)
        self.assertEqual(probe.fanout_count, 1)
        # Fanout handler ran concurrently with at least one family
        # handler. Without the overlap fix (family draining before
        # fanout starts), `overlap_seen` would stay False.
        self.assertTrue(
            probe.overlap_seen,
            "family bucket and fanout bucket did not overlap -- regression "
            "to draining family synchronously before the executor starts?",
        )

    def test_ready_issues_fan_out_concurrently(self) -> None:
        # `ready` is NOT family-aware -- `_handle_ready` only writes its
        # own pinned state, then recurses into `_handle_implementing`
        # which runs the long-running dev-agent work. Putting `ready` in
        # the family bucket would force every ready->implementing job to
        # run sequentially on the caller thread, defeating the
        # `parallel_limit > 1` concurrency goal of issue #115. This test
        # pins that contract: with three `ready` issues and
        # `parallel_limit=3`, all three must be able to enter
        # `_process_issue` concurrently.
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2, 3), label=support.LABEL_READY)

        caller_thread = threading.get_ident()
        recorder = probes._BarrierProcessRecorder(3, record_thread=True)

        with patch.object(workflow, support.REFRESH_BASE), \
             patch.object(workflow, support.PROCESS_ISSUE, side_effect=recorder):
            workflow.tick(gh, support._spec(parallel_limit=3))

        recorder.assert_worker_records(self, caller_thread)

    def test_label_error_does_not_abort_others(self) -> None:
        # Per-issue exception isolation must extend to the partition's
        # label read. The reviewer's reproducer: if `gh.workflow_label`
        # raises on one issue while classifying for parallel fanout, the
        # partition loop aborts and EVERY other eligible issue this tick
        # goes unprocessed -- a regression of the existing per-issue
        # isolation invariant. The fix catches the read, logs it, and
        # routes the offending issue into the family bucket where the
        # per-issue try/except picks up any sustained failure.
        gh = support.FakeGitHubClient()
        support._seed_issues(gh, (1, 2, 3))
        recorder = probes._IssueProcessRecorder()
        # Issue #2 still ends up in `_process_issue` via the family
        # bucket (the partition routes label-read failures there) so the
        # fake_process gets called for it too -- but ALSO for #1 and #3,
        # proving the other issues weren't aborted.

        with (
            patch.object(
                support.FakeGitHubClient,
                "workflow_label",
                family_support._flaky_workflow_label,
            ),
            patch.object(workflow, support.REFRESH_BASE),
            patch.object(workflow, support.PROCESS_ISSUE, side_effect=recorder),
        ):
            workflow.tick(gh, support._spec(parallel_limit=3))

        # All three issues were attempted -- the partition did not abort
        # after the bad label read on #2.
        self.assertEqual(sorted(recorder.processed), [1, 2, 3])

    def test_family_bucket_uses_one_slot(self) -> None:
        # Reviewer's exact reproducer: with `parallel_limit=2`, two
        # family-aware issues, and one fanout issue, an earlier draft
        # that submitted per-family-issue futures plus a shared lock
        # let the slow family handler hold one worker slot while the
        # second family future occupied the OTHER worker slot blocking
        # on the lock -- the fanout issue stayed queued until the slow
        # family handler exited. The drain-task design folds the whole
        # family bucket into one future so it consumes exactly one
        # executor slot regardless of how many family-aware issues are
        # pending, leaving the other limit-1 slots free for fanout.
        #
        # The test holds the FIRST family handler inside `_process_issue`
        # until the fanout handler completes; without the drain-task fix
        # the fanout handler would be queued and never run, the wait
        # below would time out, and the assertion would fire.
        gh = support.FakeGitHubClient()
        # Two family-aware issues. The first is slow; the second
        # must wait for the first because the family bucket runs them
        # sequentially in one drain task.
        gh.add_issue(support.make_issue(1, label=support.LABEL_DECOMPOSING))
        gh.add_issue(support.make_issue(2, label=support.LABEL_BLOCKED))
        # One fanout issue that MUST advance while the slow family
        # handler is still inside `_process_issue`.
        gh.add_issue(
            support.make_issue(support._FANOUT_ISSUE_NUMBER, label=support.LABEL_IMPLEMENTING),
        )
        probe = probes._FamilySlotProbe()
        with support._running_thread(
            probe.release_after_fanout,
            probe.cleanup,
        ):
            # parallel_limit=2 + 3 submissions total. Family bucket =
            # one drain task = one slot. Fanout = one task = one slot.
            # The second family issue stays inside the drain task (not
            # a separate executor slot), so the fanout's slot is free
            # while issue #1 is held.
            with patch.object(workflow, support.REFRESH_BASE), patch.object(
                workflow,
                support.PROCESS_ISSUE,
                side_effect=probe.process,
            ):
                workflow.tick(gh, support._spec(parallel_limit=2))

        if probe.releaser_errors:
            raise probe.releaser_errors[0]

        # All three issues handled.
        self.assertEqual(
            sorted(probe.observed_order),
            [1, 2, support._FANOUT_ISSUE_NUMBER],
        )
        # Family #2 ran AFTER family #1 (drain task is sequential).
        first_family_index = probe.observed_order.index(1)
        second_family_index = probe.observed_order.index(2)
        self.assertLess(
            first_family_index,
            second_family_index,
            probe.observed_order,
        )
        # And the fanout entered `_process_issue` BEFORE family #1
        # exited (the releaser only released after `fanout_done` was
        # set, which the fanout handler sets on entry).
        fanout_index = probe.observed_order.index(support._FANOUT_ISSUE_NUMBER)
        self.assertLess(
            fanout_index,
            second_family_index,
            probe.observed_order,
        )

    def test_slow_family_does_not_block_fanout(self) -> None:
        # Reviewer's reproducer: a single long decomposing / unlabeled-
        # pickup agent run must NOT block the other workers in the same
        # tick. With the family lock holding the family bucket on one
        # worker, the other (limit-1) workers must still be able to
        # advance unrelated implementing / validating issues -- otherwise
        # a mixed-stage tick collapses back to serial in practice.
        gh = support.FakeGitHubClient()
        # One slow family-aware issue. The handler holds inside
        # `_process_issue` until released by the test; without the
        # overlap fix this would freeze every other worker.
        gh.add_issue(support.make_issue(1, label=support.LABEL_DECOMPOSING))
        # Several fanout issues that MUST advance while the family
        # handler is still running.
        support._seed_issues(gh, (10, 11, 12))
        probe = family_support._SlowFamilyProbe(fanout_count=3)
        with support._running_thread(
            probe.release_after_fanout,
            probe.cleanup,
        ), patch.object(workflow, support.REFRESH_BASE), patch.object(
            workflow,
            support.PROCESS_ISSUE,
            side_effect=probe.process,
        ):
            workflow.tick(gh, support._spec(parallel_limit=4))

        # All three fanout issues completed while the family handler
        # was still inside `_process_issue` -- exactly the property the
        # reviewer asked for. Without the overlap fix, this list would
        # be empty (or only one entry, the lucky fanout that grabbed
        # the caller thread).
        self.assertTrue(probe.released_after_fanout)
        self.assertEqual(sorted(probe.fanout_done), [10, 11, 12])

    def test_family_stages_do_not_race_child_state(
        self,
    ) -> None:
        # Regression for the reproducer the reviewer flagged: a parent
        # `decomposing` recovery seeded `parent_number` on a child while a
        # concurrent `blocked` tick on the same child cleared it and
        # wrote `awaiting_human=True` + `park_reason=blocked_no_children`.
        # With the tick-local family lock in place, the two family-aware
        # handlers cannot overlap regardless of which worker picks each
        # one up -- whichever runs first, the parent's repair is the
        # final word and the child's pinned state retains `parent_number`
        # without the stale park flags.
        gh = support.FakeGitHubClient()
        # Parent #10 carries the half-finished-decomposition recovery
        # markers (`expected_children_count=1`, `children=[20]`) so its
        # `_handle_decomposing` enters the repair branch and seeds the
        # child's state. Child #20 is labeled `blocked` with empty pinned
        # state, so its `_handle_blocked` would normally park
        # `blocked_no_children` and clobber the parent's seed.
        gh.add_issue(support.make_issue(10, label=support.LABEL_DECOMPOSING))
        gh.add_issue(support.make_issue(support._FAMILY_CHILD_ISSUE_NUMBER, label=support.LABEL_BLOCKED))
        gh.seed_state(
            10,
            expected_children_count=1,
            children=[support._FAMILY_CHILD_ISSUE_NUMBER],
            umbrella=None,
        )

        with (
            patch.object(workflow, support.REFRESH_BASE),
            patch.object(
                workflow,
                support.PROCESS_ISSUE,
                side_effect=family_support._simulate_family_child_state,
            ),
        ):
            workflow.tick(gh, support._spec(parallel_limit=4))

        # Child's final state retains the parent's seed and is not parked.
        # The family lock guarantees the two handlers ran sequentially
        # in some order; either order produces this final state because
        # the parent's repair either runs first (child sees parent_number
        # set and returns early) or last (parent's write is final).
        child_state = gh.pinned_data(support._FAMILY_CHILD_ISSUE_NUMBER)
        self.assertEqual(child_state.get(support.KEY_PARENT_NUMBER), 10)
        self.assertFalse(child_state.get(support.KEY_AWAITING_HUMAN))
        self.assertIsNone(child_state.get(support.KEY_PARK_REASON))
