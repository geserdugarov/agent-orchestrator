# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop signal handling tests."""

import signal
import tempfile
import unittest
from unittest.mock import patch

from tests import main_helpers as _helpers


class SignalHandlingTest(unittest.TestCase):
    """A signal that arrives mid-tick must propagate as a non-zero exit
    code so `run.sh` skips its restart loop. With parallel fan-out the
    in-flight repo ticks finish what they started (interrupting a
    `workflow.tick` mid-flight could leave a worktree half-rebased), but
    the loop exits after the current tick instead of continuing to the
    next poll iteration.
    """

    def test_sigint_tick_yields_signal_exit(self) -> None:
        # The first repo to start triggers SIGINT. Both repos may
        # complete (parallel ticks can't be cancelled mid-run without
        # leaving worktrees inconsistent), but the loop must exit with
        # the signal-aware code so `run.sh` keys on it to skip restart.
        with (
            tempfile.TemporaryDirectory() as td,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{td}|main\nbeta/two|{td}|develop"),
                }
            ) as main_mod,
        ):
            tick_probe = _helpers._FirstTickShutdown(main_mod, signal.SIGINT)
            clients = _helpers._ClientFactory()

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=tick_probe),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            # 128 + SIGINT(2) = 130. run.sh keys on this to skip restart.
            self.assertEqual(rc, _helpers._SIGNAL_EXIT_BASE + signal.SIGINT)

    def test_shutdown_flag_preempts_single_repo_tick(self) -> None:
        # The single-repo path stays in-thread and checks `running`
        # before invoking `workflow.tick`. A shutdown that already
        # arrived (e.g. between poll iterations) must therefore skip
        # the tick entirely instead of running one more before the
        # process exits.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder()

            # Pre-set the shutdown flag so the `--once` tick observes
            # `running=False` immediately when `_run_tick` is entered.
            main_mod.running = False
            main_mod.received_signal = signal.SIGINT

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=recorder),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            # No tick ran AND the exit code carried the signal forward.
            self.assertEqual(recorder.slugs, [])
            self.assertEqual(rc, _helpers._SIGNAL_EXIT_BASE + signal.SIGINT)

    def test_sigterm_yields_signal_exit_code(self) -> None:
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder(
                on_tick=lambda gh, spec: main_mod._shutdown(
                    signal.SIGTERM,
                    None,
                ),
            )

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=recorder),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, _helpers._SIGNAL_EXIT_BASE + signal.SIGTERM)
