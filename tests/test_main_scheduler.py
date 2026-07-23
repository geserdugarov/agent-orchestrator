# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop scheduler lifecycle tests."""

import signal
import tempfile
import unittest
from unittest.mock import patch

from tests import main_helpers as _helpers


class SchedulerWiringTest(unittest.TestCase):
    """`MAX_PARALLEL_ISSUES_GLOBAL` and `MAX_PARALLEL_ISSUES_PER_REPO` are
    the host-wide and per-repo ceilings on concurrent per-issue handlers.
    The polling loop builds ONE `IssueScheduler` at startup from those
    env vars and threads the SAME instance through every `workflow.tick`
    call so cross-repo workers actually contend on the same caps. The
    scheduler is shut down on exit so in-flight workers complete cleanly
    regardless of how the loop terminates (`--once` finishing, signal,
    self-modifying-merge restart).
    """

    def test_main_builds_one_scheduler_for_every_tick(
        self,
    ) -> None:
        # The polling loop must build one IssueScheduler at startup
        # sized to (MAX_PARALLEL_ISSUES_GLOBAL, MAX_PARALLEL_ISSUES_PER_REPO)
        # and pass the SAME instance to every `workflow.tick` call so
        # cross-repo workers actually contend on the same caps.
        # Building a fresh scheduler per repo would isolate each repo
        # to its own caps and defeat the global ceiling.
        with (
            tempfile.TemporaryDirectory() as td,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{td}|main\nbeta/two|{td}|develop"),
                    "MAX_PARALLEL_ISSUES_GLOBAL": "4",
                    "MAX_PARALLEL_ISSUES_PER_REPO": "3",
                }
            ) as main_mod,
        ):
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder()

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=recorder),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, 0)
            self.assertEqual(len(recorder.schedulers), 2)
            self.assertIsNotNone(recorder.schedulers[0])
            # Same instance for every spec -- a per-repo scheduler would
            # let every repo independently saturate the global cap.
            self.assertIs(recorder.schedulers[0], recorder.schedulers[1])
            # Caps derived from the env vars.
            self.assertEqual(recorder.schedulers[0].global_cap, 4)
            self.assertEqual(recorder.schedulers[0].per_repo_cap, 3)

    def test_main_uses_scheduler_across_legacy_path(
        self,
    ) -> None:
        # The legacy single-repo path must also receive a real scheduler
        # (not None) -- production "normal `python -m orchestrator.main`"
        # invocations would otherwise fall back to the in-tick dispatch
        # and wait for handler completion on the caller thread.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder()

            with (
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=recorder),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, 0)
            self.assertEqual(len(recorder.schedulers), 1)
            self.assertIsNotNone(recorder.schedulers[0])
            self.assertIsInstance(recorder.schedulers[0], main_mod.IssueScheduler)

    def test_main_shuts_down_scheduler_on_normal_exit(self) -> None:
        # The scheduler must be shut down before main() returns so any
        # in-flight workers (e.g. handlers a `--once` invocation just
        # submitted) complete cleanly. Without shutdown the daemon
        # executor threads could be torn down mid-handler at process
        # exit.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder()
            scheduler_factory = _helpers._SchedulerFactory(main_mod.IssueScheduler)

            with (
                patch.object(main_mod, "IssueScheduler", scheduler_factory),
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=recorder),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, 0)
            self.assertEqual(len(scheduler_factory.built), 1)
            self.assertIs(recorder.schedulers[0], scheduler_factory.built[0])
            # After main() returns, a follow-up submit must be rejected
            # because the scheduler has been closed.
            self.assertFalse(
                scheduler_factory.built[0].submit(
                    _helpers._LEGACY_REPO,
                    _helpers._UNUSED_ISSUE_NUMBER,
                    lambda: None,
                ),
                "scheduler was not shut down before main() returned",
            )

    def test_main_shuts_down_scheduler_on_signal_exit(self) -> None:
        # SIGINT/SIGTERM during a tick must still drain the scheduler
        # before main() returns -- otherwise a signal-induced exit
        # would strand in-flight workers and any late failures.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()
            recorder = _helpers._TickRecorder(
                on_tick=lambda gh, spec: main_mod._shutdown(
                    signal.SIGINT,
                    None,
                ),
            )
            scheduler_factory = _helpers._SchedulerFactory(main_mod.IssueScheduler)

            with (
                patch.object(main_mod, "IssueScheduler", scheduler_factory),
                patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=recorder),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, _helpers._SIGNAL_EXIT_BASE + signal.SIGINT)
            self.assertEqual(len(scheduler_factory.built), 1)
            self.assertFalse(
                scheduler_factory.built[0].submit(
                    _helpers._LEGACY_REPO,
                    _helpers._UNUSED_ISSUE_NUMBER,
                    lambda: None,
                ),
                "scheduler not shut down on signal-induced exit",
            )

    def test_signal_closes_active_submit_path(
        self,
    ) -> None:
        # `_shutdown` closes the scheduler's submit path immediately via
        # `scheduler.shutdown(wait=False)` when a signal fires. `running=False`
        # alone only stops at the next tick boundary, so a `workflow.tick`
        # still iterating its eligible-issue list would otherwise keep landing
        # fresh `scheduler.submit` calls for the rest of the dispatch loop and
        # grow the in-flight set after the user asked to stop. With the submit
        # path closed mid-tick, those late submits are refused and the
        # finally-block `shutdown(wait=True)` only waits on workers that
        # already started.
        with _helpers.reload_main(_helpers._LEGACY_ENV) as main_mod:
            clients = _helpers._ClientFactory()
            tick_probe = _helpers._SignalSubmitTick(main_mod)

            with (
                patch.object(
                    main_mod,
                    _helpers._GITHUB_CLIENT_ATTR,
                    side_effect=clients,
                ),
                patch.object(
                    main_mod.workflow,
                    _helpers._TICK_ATTR,
                    side_effect=tick_probe,
                ),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            # Signal exit code propagated AND the second mid-tick
            # submit was rejected.
            self.assertEqual(rc, _helpers._SIGNAL_EXIT_BASE + signal.SIGINT)
            self.assertEqual(tick_probe.submit_results, [True, False])

    def test_signal_closes_multi_repo_submit(
        self,
    ) -> None:
        # Same invariant as above but where both repos are already
        # iterating concurrently when the signal fires. The cross-repo
        # barrier ensures alpha and beta are BOTH past their
        # `_tick_one`-level `running` short-circuit before the signal
        # lands, so beta's post-signal `scheduler.submit` is the
        # observable canary: it must return False so the fan-out executor
        # cannot accept work after the user asked to stop.
        with (
            tempfile.TemporaryDirectory() as td,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{td}|main\nbeta/two|{td}|develop"),
                }
            ) as main_mod,
        ):
            tick_probe = _helpers._MultiRepoSignalTick(main_mod)
            clients = _helpers._ClientFactory()

            with (
                patch.object(
                    main_mod,
                    _helpers._GITHUB_CLIENT_ATTR,
                    side_effect=clients,
                ),
                patch.object(
                    main_mod.workflow,
                    _helpers._TICK_ATTR,
                    side_effect=tick_probe,
                ),
            ):
                rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, _helpers._SIGNAL_EXIT_BASE + signal.SIGINT)
            self.assertEqual(tick_probe.beta_results, [False])

    def test_global_cap_bounds_cross_repo_workers(
        self,
    ) -> None:
        # End-to-end coverage that the scheduler main built actually
        # bounds concurrent per-issue workers across repos. Three
        # tick threads (one per repo) each submit a worker to the
        # SAME scheduler with `parallel_limit=1` (per-repo cap is
        # always >= 1) and global_cap=2; only two of the three workers
        # may run in parallel -- the third must be skipped this tick.
        with (
            tempfile.TemporaryDirectory() as td,
            _helpers.reload_main(
                {
                    _helpers._REPOS_ENV: (f"alpha/one|{td}|main\nbeta/two|{td}|develop\ngamma/three|{td}|main"),
                    "MAX_PARALLEL_ISSUES_GLOBAL": "2",
                }
            ) as main_mod,
        ):
            cap_probe = _helpers._GlobalCapProbe()
            clients = _helpers._ClientFactory()
            with cap_probe.releasing():
                with (
                    patch.object(main_mod, _helpers._GITHUB_CLIENT_ATTR, side_effect=clients),
                    patch.object(main_mod.workflow, _helpers._TICK_ATTR, side_effect=cap_probe.tick),
                ):
                    rc = main_mod.main(_helpers._ONCE_ARGS)

            self.assertEqual(rc, 0)
            # All three repos saw the SAME scheduler instance.
            self.assertEqual(len(cap_probe.received), 3)
            self.assertEqual(len({id(sched) for sched in cap_probe.received}), 1)
            # Cap is 2: even though three repos submitted, never more
            # than 2 workers ran concurrently.
            self.assertEqual(cap_probe.max_in_flight, 2)
