# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Asynchronous polling-dispatch tests."""

import time
import unittest

from tests import main_dispatch_execution as _execution
from tests import main_helpers as _helpers


class AsyncPollingDispatchTest(unittest.TestCase):
    def test_long_handler_does_not_block_next_poll(self) -> None:
        with (
            _helpers.reload_main(_helpers._LEGACY_ENV) as main_module,
            _execution.dispatch_context(
                main_module,
                [_helpers._ALPHA_REPO, _helpers._BETA_REPO],
            ) as dispatch,
        ):
            poll_probe = _helpers._CrossPollProbe()
            self.addCleanup(poll_probe.alpha_release.set)
            poll_probe.current_pass = 1
            started_at = time.monotonic()
            dispatch.run(poll_probe.tick)
            first_pass_elapsed = time.monotonic() - started_at

            self.assertTrue(
                poll_probe.alpha_started.wait(timeout=_helpers._FAST_WAIT_SECONDS),
                "alpha worker should have started during pass 1",
            )
            self.assertLess(first_pass_elapsed, _helpers._FAST_WAIT_SECONDS)

            poll_probe.current_pass = 2
            dispatch.run(poll_probe.tick)
            self.assertTrue(
                poll_probe.beta_done.wait(timeout=_helpers._FAST_WAIT_SECONDS),
                "beta worker did not run while alpha remained in flight",
            )
            self.assertTrue(dispatch.scheduler.is_active(_helpers._ALPHA_REPO, 1))

    def test_issue_not_relaunched_across_polls(self) -> None:
        with (
            _helpers.reload_main(_helpers._LEGACY_ENV) as main_module,
            _execution.dispatch_context(main_module, [_helpers._REPO]) as dispatch,
        ):
            active_probe = _helpers._DuplicateActiveProbe()
            self.addCleanup(active_probe.release.set)
            dispatch.run(active_probe.tick)
            self.assertTrue(
                active_probe.started.wait(timeout=_helpers._FAST_WAIT_SECONDS),
            )
            dispatch.run(active_probe.tick)

            self.assertEqual(active_probe.submit_results, [True, False])
            with active_probe.lock:
                self.assertEqual(active_probe.run_count, 1)

    def test_worker_finish_clears_in_flight_marker(self) -> None:
        with (
            _helpers.reload_main(_helpers._LEGACY_ENV) as main_module,
            _execution.dispatch_context(main_module, [_helpers._REPO]) as dispatch,
        ):
            finish_probe = _helpers._FinishedWorkerProbe()
            dispatch.run(finish_probe.tick)
            self.assertTrue(
                finish_probe.done_events[-1].wait(timeout=_helpers._FAST_WAIT_SECONDS),
            )
            _helpers._wait_until_inactive(dispatch.scheduler, _helpers._REPO, 3)
            self.assertFalse(dispatch.scheduler.is_active(_helpers._REPO, 3))

            dispatch.run(finish_probe.tick)
            self.assertTrue(
                finish_probe.done_events[-1].wait(timeout=_helpers._FAST_WAIT_SECONDS),
            )
            self.assertEqual(finish_probe.submit_results, [True, True])
            with finish_probe.lock:
                self.assertEqual(finish_probe.run_count, 2)
