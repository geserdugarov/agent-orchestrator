# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop shutdown watchdog tests."""

import signal
import unittest
from unittest.mock import MagicMock, patch

from tests import main_helpers as _helpers


class ShutdownBudgetTest(unittest.TestCase):
    """A signal-initiated stop must exit within `SHUTDOWN_GRACE_SECONDS`
    regardless of what an in-flight worker is blocked on. The cooperative
    drain only advances at tick boundaries and then waits on
    `scheduler.shutdown`, so without a bound a tick wedged in a GitHub retry
    loop -- or a worker parked in a 30-minute agent subprocess -- held the
    process past systemd's `TimeoutStopSec` and earned a SIGKILL. The
    watchdog is the hard backstop; terminating in-flight agents up front is
    what makes the common case exit cleanly before the backstop fires.
    """

    def test_drain_window_reserves_terminate_grace(self) -> None:
        # The hard ceiling is `SHUTDOWN_GRACE_SECONDS`. `_force_exit`'s own
        # SIGTERM->SIGKILL sweep takes up to `_shutdown_terminate_grace()`, so
        # the watchdog must wait only `grace - reserve` for the drain; adding
        # the sweep on top of the full grace would overrun the ceiling by the
        # sweep's grace. Capture the timeout the watchdog waits on to prove
        # drain_window + sweep_reserve == SHUTDOWN_GRACE_SECONDS.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            wait_recorder = _helpers._WaitRecorder()
            fake_event = MagicMock()
            fake_event.wait.side_effect = wait_recorder
            with (
                patch.object(main_mod, "_shutdown_complete", fake_event),
                patch.object(
                    main_mod.config,
                    _helpers._SHUTDOWN_GRACE_ATTR,
                    _helpers._SHUTDOWN_GRACE_SECONDS,
                ),
            ):
                main_mod._run_shutdown_watchdog(signal.SIGTERM)
            reserve = main_mod._shutdown_terminate_grace()
            self.assertEqual(
                wait_recorder.timeout,
                _helpers._SHUTDOWN_GRACE_SECONDS - reserve,
            )
            self.assertLessEqual(
                wait_recorder.timeout + reserve,
                _helpers._SHUTDOWN_GRACE_SECONDS,
            )

    def test_terminate_grace_capped_and_within_budget(self) -> None:
        # The reserve is a slice of the budget, never the whole of it (which
        # would starve the drain) and never more than 5s for a large grace.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            for grace in (1, 2, 10, _helpers._SHUTDOWN_GRACE_SECONDS, 3600):
                with patch.object(
                    main_mod.config,
                    _helpers._SHUTDOWN_GRACE_ATTR,
                    grace,
                ):
                    reserve = main_mod._shutdown_terminate_grace()
                self.assertGreater(reserve, 0)
                self.assertLess(reserve, grace)
                self.assertLessEqual(reserve, _helpers._WORKER_WAIT_SECONDS)

    def test_signal_exit_terminates_in_flight_agents(self) -> None:
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder(
                on_tick=lambda gh, spec: main_mod._shutdown(
                    signal.SIGTERM,
                    None,
                ),
            )

            with (
                patch.object(main_mod, "_arm_shutdown_watchdog"),
                patch.object(
                    main_mod.agents,
                    "terminate_all_running",
                ) as term,
                patch.object(
                    main_mod,
                    _helpers._GITHUB_CLIENT_ATTR,
                    side_effect=clients,
                ),
                patch.object(
                    main_mod.workflow,
                    _helpers._TICK_ATTR,
                    side_effect=recorder,
                ),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)
                term.assert_called_once_with()

            self.assertEqual(rc, _helpers._SIGNAL_EXIT_BASE + signal.SIGTERM)

    def test_normal_exit_does_not_terminate_agents(self) -> None:
        # The non-signal paths (`--once` finishing, self-modifying-merge
        # restart) must keep the existing "let in-flight work finish" drain
        # -- only a signal stop, which is under the systemd deadline, kills
        # agents up front.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()

            with (
                patch.object(
                    main_mod.agents,
                    "terminate_all_running",
                ) as term,
                patch.object(
                    main_mod,
                    _helpers._GITHUB_CLIENT_ATTR,
                    side_effect=clients,
                ),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)
                term.assert_not_called()

            self.assertEqual(rc, 0)
