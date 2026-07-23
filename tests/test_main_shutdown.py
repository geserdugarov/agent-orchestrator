# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop shutdown watchdog tests."""

import signal
import unittest
from unittest.mock import patch

from tests import main_helpers as _helpers


class ShutdownWatchdogTest(unittest.TestCase):
    """A signal-initiated stop must exit within `SHUTDOWN_GRACE_SECONDS`
    regardless of what an in-flight worker is blocked on. The cooperative
    drain only advances at tick boundaries and then waits on
    `scheduler.shutdown`, so without a bound a tick wedged in a GitHub retry
    loop -- or a worker parked in a 30-minute agent subprocess -- held the
    process past systemd's `TimeoutStopSec` and earned a SIGKILL. The
    watchdog is the hard backstop; terminating in-flight agents up front is
    what makes the common case exit cleanly before the backstop fires.
    """

    def test_shutdown_arms_watchdog(self) -> None:
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            with patch.object(main_mod, "_arm_shutdown_watchdog") as arm:
                main_mod._shutdown(signal.SIGTERM, None)
                arm.assert_called_once_with(signal.SIGTERM)

    def test_watchdog_force_exits_when_drain_overruns(self) -> None:
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            main_mod._shutdown_complete.clear()
            forced: list[int] = []
            with (
                patch.object(
                    main_mod,
                    "_force_exit",
                    side_effect=lambda signal_number: forced.append(signal_number),
                ),
                patch.object(
                    main_mod.config,
                    _helpers._SHUTDOWN_GRACE_ATTR,
                    _helpers._SHORT_SHUTDOWN_GRACE_SECONDS,
                ),
            ):
                main_mod._run_shutdown_watchdog(signal.SIGTERM)
            self.assertEqual(forced, [signal.SIGTERM])

    def test_clean_return_after_drain_completes(self) -> None:
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            # Drain already finished: the watchdog must return without ever
            # touching the process even though grace has not elapsed.
            main_mod._shutdown_complete.set()
            forced: list[int] = []
            with (
                patch.object(
                    main_mod,
                    "_force_exit",
                    side_effect=lambda signal_number: forced.append(signal_number),
                ),
                patch.object(
                    main_mod.config,
                    _helpers._SHUTDOWN_GRACE_ATTR,
                    _helpers._WORKER_WAIT_SECONDS,
                ),
            ):
                main_mod._run_shutdown_watchdog(signal.SIGTERM)
            self.assertEqual(forced, [])

    def test_force_exit_terminates_then_hard_exits(self) -> None:
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            with (
                patch.object(
                    main_mod.agents,
                    "terminate_all_running",
                ) as term,
                patch.object(
                    main_mod.os,
                    "_exit",
                    side_effect=RuntimeError("exit"),
                ) as os_exit,
            ):
                with self.assertRaises(RuntimeError):
                    main_mod._force_exit(signal.SIGTERM)
                # The sweep is bounded by the reserved terminate grace -- NOT
                # the default 5s -- so the watchdog path stays within budget.
                term.assert_called_once_with(
                    grace=main_mod._shutdown_terminate_grace(),
                )
                os_exit.assert_called_once_with(
                    _helpers._SIGNAL_EXIT_BASE + signal.SIGTERM,
                )
