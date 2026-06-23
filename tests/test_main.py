"""Tests for the polling-loop entry point.

The multi-repo polling loop must call `workflow.tick(gh, spec)` for every
configured spec on every tick. A per-repo exception in `tick` must not
prevent the remaining specs from running -- the orchestrator's whole point
is to keep advancing other repos when one is stuck.

The loop fans repo ticks out across a thread pool when more than one repo
is configured, so cross-repo fan-out, the global per-issue cap, and
signal handling all need to keep working under concurrent ticks.
"""
from __future__ import annotations

import importlib
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch


@contextmanager
def _reload_main(env: dict[str, str]):
    """Reload `orchestrator.config` + `orchestrator.main` with `env` patched
    over the process environment, so module-level `REPOS` parsing actually
    sees the test value. Yields the freshly imported `main` module.

    `importlib.import_module` is used instead of `from orchestrator import
    main` because the latter falls back to the parent package's cached
    `main` attribute even after the submodule is popped from `sys.modules`,
    which leaks state across tests.
    """
    full_env = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        "GITHUB_TOKEN": "ghp-test-secret",
    }
    full_env.update(env)
    with patch.dict(os.environ, full_env, clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.main", None)
        # Force config to re-run module-level REPOS parsing first, then
        # main, so main_mod.config is the freshly imported module.
        importlib.import_module("orchestrator.config")
        main_mod = importlib.import_module("orchestrator.main")
        # Skip signal-handler registration and the file-handler setup so
        # the test does not touch shared process state or filesystem.
        with patch.object(main_mod, "_configure_logging"), \
             patch.object(main_mod.signal, "signal"):
            yield main_mod


class PollingLoopFanOutTest(unittest.TestCase):
    def test_once_calls_tick_for_every_configured_spec(self) -> None:
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop"
            ),
        }) as main_mod:
            tick_calls: list[tuple[str, str]] = []
            calls_lock = threading.Lock()

            def fake_tick(gh, spec, *, scheduler=None):
                # Record the spec slug + whichever client main.py paired it
                # with, so a regression that crossed wires (spec for alpha
                # paired with beta's gh) would surface here. Calls happen
                # on worker threads so the list needs a lock.
                with calls_lock:
                    tick_calls.append((spec.slug, gh.slug))

            clients_by_slug: dict[str, MagicMock] = {}

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                clients_by_slug[repo_spec.slug] = m
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            # Parallel fan-out makes the call order non-deterministic; the
            # invariant is that every (spec, paired client) tuple appears
            # exactly once and the pairing is correct.
            self.assertEqual(
                set(tick_calls),
                {("alpha/one", "alpha/one"), ("beta/two", "beta/two")},
            )
            self.assertEqual(len(tick_calls), 2)
            for slug in ("alpha/one", "beta/two"):
                clients_by_slug[slug].ensure_workflow_labels.assert_called_once()

    def test_per_repo_tick_exception_does_not_block_other_repos(self) -> None:
        # The whole point of catching per-repo failures: one repo wedged in
        # an unhandled error must not stop the others from advancing. With
        # parallel fan-out the exception is isolated inside the per-repo
        # worker, so the surviving repos still complete their ticks even
        # though the failing repo's worker raised.
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop\n"
                f"gamma/three|{td}|main"
            ),
        }) as main_mod:
            ticked: list[str] = []
            ticked_lock = threading.Lock()

            def fake_tick(gh, spec, *, scheduler=None):
                with ticked_lock:
                    ticked.append(spec.slug)
                if spec.slug == "alpha/one":
                    raise RuntimeError("simulated alpha failure")

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            # Returned 0 (loop swallowed the per-repo exception) and every
            # spec was attempted -- order is non-deterministic under
            # parallel fan-out, so assert on the set.
            self.assertEqual(rc, 0)
            self.assertEqual(
                set(ticked), {"alpha/one", "beta/two", "gamma/three"},
            )
            self.assertEqual(len(ticked), 3)

    def test_legacy_single_repo_still_works(self) -> None:
        # No REPOS set: main.py must still run a single tick using the
        # legacy REPO/TARGET_REPO_ROOT/BASE_BRANCH trio. The single-repo
        # path stays in-thread (no executor) so a deployment that does
        # not use REPOS sees no behavior change.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            tick_calls: list[str] = []
            tick_threads: list[int] = []

            def fake_tick(gh, spec, *, scheduler=None):
                tick_calls.append(spec.slug)
                tick_threads.append(threading.get_ident())

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            self.assertEqual(tick_calls, ["owner/legacy"])
            # No executor: the tick runs on the same thread `main` was
            # called from. A regression that always spawned a worker
            # thread (even for one repo) would show a different tid here.
            self.assertEqual(tick_threads, [threading.get_ident()])

    def test_repos_run_concurrently(self) -> None:
        # The whole point of fan-out: configured repos must overlap. A
        # `Barrier(N)` requires every worker to arrive before any can
        # leave, so it deadlocks under sequential iteration and the
        # bounded timeout surfaces that regression as a test failure.
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop\n"
                f"gamma/three|{td}|main"
            ),
        }) as main_mod:
            barrier = threading.Barrier(3, timeout=5.0)
            completed: list[str] = []
            completed_lock = threading.Lock()

            def fake_tick(gh, spec, *, scheduler=None):
                # If ticks ran sequentially, the first arrival would wait
                # forever for the second / third and the barrier would
                # time out (BrokenBarrierError surfaces as test failure).
                barrier.wait()
                with completed_lock:
                    completed.append(spec.slug)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            self.assertEqual(
                set(completed),
                {"alpha/one", "beta/two", "gamma/three"},
            )

    def test_label_initialization_happens_once_per_spec_at_startup(self) -> None:
        # `ensure_workflow_labels` must run exactly once per configured
        # repo at startup -- not on every tick. Re-running the label
        # bootstrap on each tick would burn API calls on a no-op and
        # change behavior on label edits between ticks.
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop"
            ),
        }) as main_mod:
            clients_by_slug: dict[str, MagicMock] = {}

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                clients_by_slug[repo_spec.slug] = m
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick"):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            self.assertEqual(set(clients_by_slug), {"alpha/one", "beta/two"})
            for client in clients_by_slug.values():
                client.ensure_workflow_labels.assert_called_once()


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

    def test_main_builds_one_scheduler_and_passes_it_to_every_tick(
        self,
    ) -> None:
        # The polling loop must build one IssueScheduler at startup
        # sized to (MAX_PARALLEL_ISSUES_GLOBAL, MAX_PARALLEL_ISSUES_PER_REPO)
        # and pass the SAME instance to every `workflow.tick` call so
        # cross-repo workers actually contend on the same caps.
        # Building a fresh scheduler per repo would isolate each repo
        # to its own caps and defeat the global ceiling.
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop"
            ),
            "MAX_PARALLEL_ISSUES_GLOBAL": "4",
            "MAX_PARALLEL_ISSUES_PER_REPO": "3",
        }) as main_mod:
            received: list[object] = []
            received_lock = threading.Lock()

            def fake_tick(gh, spec, *, scheduler=None):
                with received_lock:
                    received.append(scheduler)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            self.assertEqual(len(received), 2)
            self.assertIsNotNone(received[0])
            # Same instance for every spec -- a per-repo scheduler would
            # let every repo independently saturate the global cap.
            self.assertIs(received[0], received[1])
            # Caps derived from the env vars.
            sched = received[0]
            self.assertEqual(sched.global_cap, 4)
            self.assertEqual(sched.per_repo_cap, 3)

    def test_main_uses_same_scheduler_across_legacy_single_repo_path(
        self,
    ) -> None:
        # The legacy single-repo path must also receive a real scheduler
        # (not None) -- production "normal `python -m orchestrator.main`"
        # invocations would otherwise fall back to the in-tick dispatch
        # and wait for handler completion on the caller thread.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            received: list[object] = []

            def fake_tick(gh, spec, *, scheduler=None):
                received.append(scheduler)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            self.assertEqual(len(received), 1)
            self.assertIsNotNone(received[0])
            self.assertIsInstance(received[0], main_mod.IssueScheduler)

    def test_main_shuts_down_scheduler_on_normal_exit(self) -> None:
        # The scheduler must be shut down before main() returns so any
        # in-flight workers (e.g. handlers a `--once` invocation just
        # submitted) complete cleanly. Without shutdown the daemon
        # executor threads could be torn down mid-handler at process
        # exit.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            captured: list[object] = []

            def fake_tick(gh, spec, *, scheduler=None):
                captured.append(scheduler)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            real_scheduler_init = main_mod.IssueScheduler.__init__
            built: list[object] = []

            def tracking_init(self, *args, **kwargs):
                real_scheduler_init(self, *args, **kwargs)
                built.append(self)

            with patch.object(main_mod.IssueScheduler, "__init__", tracking_init), \
                 patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            self.assertEqual(len(built), 1)
            sched = built[0]
            self.assertIs(captured[0], sched)
            # After main() returns, a follow-up submit must be rejected
            # because the scheduler has been closed.
            self.assertFalse(
                sched.submit("owner/legacy", 999, lambda: None),
                "scheduler was not shut down before main() returned",
            )

    def test_main_shuts_down_scheduler_on_signal_exit(self) -> None:
        # SIGINT/SIGTERM during a tick must still drain the scheduler
        # before main() returns -- otherwise a signal-induced exit
        # would strand in-flight workers and any late failures.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            built: list[object] = []
            real_scheduler_init = main_mod.IssueScheduler.__init__

            def tracking_init(self, *args, **kwargs):
                real_scheduler_init(self, *args, **kwargs)
                built.append(self)

            def fake_tick(gh, spec, *, scheduler=None):
                main_mod._shutdown(signal.SIGINT, None)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod.IssueScheduler, "__init__", tracking_init), \
                 patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 128 + signal.SIGINT)
            self.assertEqual(len(built), 1)
            self.assertFalse(
                built[0].submit("owner/legacy", 999, lambda: None),
                "scheduler not shut down on signal-induced exit",
            )

    def test_signal_during_active_tick_closes_scheduler_submit_path(
        self,
    ) -> None:
        # Regression for issue #316 review feedback: `_running=False`
        # alone only stops the next tick boundary. A `workflow.tick`
        # that is still iterating its eligible-issue list when SIGINT/
        # SIGTERM fires used to keep landing fresh `scheduler.submit`
        # calls for the remainder of the dispatch loop after the user
        # already asked to stop -- the in-flight set kept growing
        # post-signal and the finally-block `shutdown(wait=True)` had
        # to wait on workers the signal handler should have refused
        # to enqueue. The fix routes `_shutdown` through
        # `scheduler.shutdown(wait=False)` so the submit path is
        # closed IMMEDIATELY, mid-tick.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            submit_results: list[bool] = []

            def fake_tick(gh, spec, *, scheduler=None):
                # Pre-signal submit lands normally.
                submit_results.append(
                    scheduler.submit(spec.slug, 1, lambda: None)
                )
                # SIGINT arrives WHILE the tick is still iterating.
                main_mod._shutdown(signal.SIGINT, None)
                # The next submit in the same tick MUST be rejected
                # because the signal handler closed the scheduler's
                # submit path. Without the fix the lambda would
                # actually run -- the assertion below is the canary.
                submit_results.append(
                    scheduler.submit(
                        spec.slug, 2,
                        lambda: self.fail(
                            "post-signal submit must not dispatch",
                        ),
                    )
                )

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(
                main_mod, "GitHubClient", side_effect=fake_client,
            ), patch.object(
                main_mod.workflow, "tick", side_effect=fake_tick,
            ):
                rc = main_mod.main(["--once"])

            # Signal exit code propagated AND the second mid-tick
            # submit was rejected.
            self.assertEqual(rc, 128 + signal.SIGINT)
            self.assertEqual(submit_results, [True, False])

    def test_signal_during_active_tick_closes_submit_path_multi_repo(
        self,
    ) -> None:
        # Same invariant as above but where both repos are already
        # iterating concurrently when the signal fires. The cross-repo
        # barrier ensures alpha and beta are BOTH past their
        # `_tick_one`-level `_running` short-circuit before the signal
        # lands, so beta's post-signal `scheduler.submit` is the
        # observable canary: with the fix it returns False; without the
        # fix the scheduler still accepts work on the fan-out executor
        # after the user asked to stop.
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop"
            ),
        }) as main_mod:
            both_inside = threading.Barrier(2, timeout=5.0)
            signal_fired = threading.Event()
            beta_submit_after_signal: list[bool] = []
            beta_lock = threading.Lock()

            def fake_tick(gh, spec, *, scheduler=None):
                # Wait until both repos are iterating concurrently so a
                # signal arriving now cannot be deflected by the
                # `_tick_one` "shutdown before tick start" guard for
                # beta -- beta is already past it.
                both_inside.wait()
                if spec.slug == "alpha/one":
                    main_mod._shutdown(signal.SIGINT, None)
                    signal_fired.set()
                    return
                # beta waits for the signal handler to actually fire,
                # then tries to submit; with the fix the scheduler is
                # already closed and the submit is rejected.
                self.assertTrue(signal_fired.wait(timeout=5.0))
                result = scheduler.submit(
                    spec.slug, 7,
                    lambda: self.fail(
                        "post-signal submit must not dispatch",
                    ),
                )
                with beta_lock:
                    beta_submit_after_signal.append(result)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(
                main_mod, "GitHubClient", side_effect=fake_client,
            ), patch.object(
                main_mod.workflow, "tick", side_effect=fake_tick,
            ):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 128 + signal.SIGINT)
            self.assertEqual(beta_submit_after_signal, [False])

    def test_scheduler_global_cap_bounds_concurrent_workers_across_repos(
        self,
    ) -> None:
        # End-to-end coverage that the scheduler main built actually
        # bounds concurrent per-issue workers across repos. Three
        # tick threads (one per repo) each submit a worker to the
        # SAME scheduler with `parallel_limit=1` (per-repo cap is
        # always >= 1) and global_cap=2; only two of the three workers
        # may run in parallel -- the third must be skipped this tick.
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop\n"
                f"gamma/three|{td}|main"
            ),
            "MAX_PARALLEL_ISSUES_GLOBAL": "2",
        }) as main_mod:
            received: list[object] = []
            received_lock = threading.Lock()
            in_flight = 0
            max_in_flight = 0
            counter_lock = threading.Lock()
            admitted = threading.Semaphore(0)
            release = threading.Event()

            def _worker() -> None:
                nonlocal in_flight, max_in_flight
                with counter_lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                admitted.release()
                release.wait(timeout=5.0)
                with counter_lock:
                    in_flight -= 1

            def fake_tick(gh, spec, *, scheduler=None):
                # Submit a worker to the production scheduler; the
                # scheduler's global_cap enforces the cross-repo cap.
                with received_lock:
                    received.append(scheduler)
                # Try repeatedly to land within this repo's chance
                # (the global cap may reject the third submitter).
                scheduler.submit(spec.slug, 1, _worker)

            def release_when_two_admitted() -> None:
                for _ in range(2):
                    self.assertTrue(
                        admitted.acquire(timeout=5.0),
                        "fewer than 2 workers admitted within timeout",
                    )
                time.sleep(0.1)
                release.set()

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            releaser = threading.Thread(target=release_when_two_admitted)
            releaser.start()
            try:
                with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                     patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                    rc = main_mod.main(["--once"])
            finally:
                release.set()
                releaser.join(timeout=5.0)

            self.assertEqual(rc, 0)
            # All three repos saw the SAME scheduler instance.
            self.assertEqual(len(received), 3)
            self.assertEqual(len({id(s) for s in received}), 1)
            # Cap is 2: even though three repos submitted, never more
            # than 2 workers ran concurrently.
            self.assertEqual(max_in_flight, 2)


class SignalHandlingTest(unittest.TestCase):
    """A signal that arrives mid-tick must propagate as a non-zero exit
    code so `run.sh` skips its restart loop. With parallel fan-out the
    in-flight repo ticks finish what they started (interrupting a
    `workflow.tick` mid-flight could leave a worktree half-rebased), but
    the loop exits after the current tick instead of continuing to the
    next poll iteration.
    """

    def test_sigint_during_tick_yields_signal_exit_code(self) -> None:
        # The first repo to start triggers SIGINT. Both repos may
        # complete (parallel ticks can't be cancelled mid-run without
        # leaving worktrees inconsistent), but the loop must exit with
        # the signal-aware code so `run.sh` keys on it to skip restart.
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop"
            ),
        }) as main_mod:
            shutdown_done = threading.Event()

            def fake_tick(gh, spec, *, scheduler=None):
                # The first arrival simulates the user pressing Ctrl+C
                # mid-tick. Subsequent arrivals are no-ops; the
                # `_shutdown` handler is itself idempotent.
                if not shutdown_done.is_set():
                    shutdown_done.set()
                    main_mod._shutdown(signal.SIGINT, None)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            # 128 + SIGINT(2) = 130. run.sh keys on this to skip restart.
            self.assertEqual(rc, 128 + signal.SIGINT)

    def test_shutdown_flag_preempts_single_repo_tick(self) -> None:
        # The single-repo path stays in-thread and checks `_running`
        # before invoking `workflow.tick`. A shutdown that already
        # arrived (e.g. between poll iterations) must therefore skip
        # the tick entirely instead of running one more before the
        # process exits.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            ticked: list[str] = []

            def fake_tick(gh, spec, *, scheduler=None):
                ticked.append(spec.slug)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            # Pre-set the shutdown flag so the `--once` tick observes
            # `_running=False` immediately when `_run_tick` is entered.
            main_mod._running = False
            main_mod._received_signal = signal.SIGINT

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            # No tick ran AND the exit code carried the signal forward.
            self.assertEqual(ticked, [])
            self.assertEqual(rc, 128 + signal.SIGINT)

    def test_sigterm_yields_signal_exit_code(self) -> None:
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            def fake_tick(gh, spec, *, scheduler=None):
                main_mod._shutdown(signal.SIGTERM, None)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 128 + signal.SIGTERM)


class AnalyticsRetentionLoopWiringTest(unittest.TestCase):
    """`main._run_tick` calls `analytics.prune_with_retention_logging`
    once per tick so retention is actually applied. The wrapper itself
    (exception swallow, log message, no-GitHub-writes guarantee) is
    tested at the analytics boundary in `tests/test_analytics.py`; the
    tests here only verify the wiring: main calls the wrapper exactly
    once per polling iteration regardless of repo count.
    """

    def test_prune_called_each_tick_in_single_repo_mode(self) -> None:
        # The legacy single-repo path stays in-thread and must still
        # call the prune wrapper so retention is actually applied.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            def fake_tick(gh, spec, *, scheduler=None):
                pass

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick), \
                 patch.object(
                     main_mod.analytics, "prune_with_retention_logging",
                 ) as prune:
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            prune.assert_called_once_with()

    def test_prune_called_once_per_tick_in_multi_repo_mode(self) -> None:
        # The multi-repo path fans repo ticks out across a thread pool;
        # the wrapper runs once at the end (not once per repo) so the
        # observability sink is processed exactly once per polling
        # iteration regardless of how many repos are configured.
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop"
            ),
        }) as main_mod:
            def fake_tick(gh, spec, *, scheduler=None):
                pass

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick), \
                 patch.object(
                     main_mod.analytics, "prune_with_retention_logging",
                 ) as prune:
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            prune.assert_called_once_with()


class AsyncPollingDispatchTest(unittest.TestCase):
    """`_run_tick` hands work to the shared scheduler and returns as soon
    as the eligible-issue callables are submitted; long-running per-issue
    handlers run on the scheduler's executor threads, not on the tick
    thread. The tests below drive `_run_tick(clients, scheduler)`
    directly across multiple polling passes (the polling loop does this
    every `POLL_INTERVAL`) so the cross-poll behaviour the issue calls
    out is observable: a slow handler in one repo cannot block the next
    poll from dispatching a different repo's work, the same issue is not
    launched twice concurrently across polls (the scheduler's
    duplicate-active gate rejects the re-submit), and worker completion
    clears the in-flight marker so a follow-up poll can re-dispatch the
    same key.
    """

    def _build_clients(self, main_mod, slugs):
        # Mirror `main`'s startup: build one MagicMock GitHubClient per
        # slug and pair it with the matching RepoSpec. The tests below
        # never call `ensure_workflow_labels`, so the mock surface is
        # intentionally minimal.
        from pathlib import Path

        from orchestrator.config import RepoSpec
        clients = []
        for slug in slugs:
            spec = RepoSpec(
                slug=slug,
                target_root=Path("/tmp"),
                base_branch="main",
            )
            gh = MagicMock()
            gh.slug = slug
            clients.append((spec, gh))
        return clients

    def test_long_running_handler_does_not_block_next_poll_for_other_repo(
        self,
    ) -> None:
        # Pass 1 submits a blocking worker for repo alpha; the tick
        # itself must return promptly because the scheduler dispatch is
        # nonblocking. Pass 2 then submits a worker for repo beta even
        # though alpha's worker is still in flight. Without the async
        # dispatch the second `_run_tick` would queue behind the
        # blocking alpha handler and the beta worker would never start
        # before the test timeout.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            sched = main_mod.IssueScheduler(
                global_cap=4, per_repo_cap=4,
            )
            self.addCleanup(sched.shutdown)
            clients = self._build_clients(
                main_mod, ["alpha/one", "beta/two"],
            )

            alpha_started = threading.Event()
            alpha_release = threading.Event()
            beta_done = threading.Event()
            current_pass = {"n": 0}

            def slow_alpha() -> None:
                alpha_started.set()
                alpha_release.wait(timeout=5.0)

            def quick_beta() -> None:
                beta_done.set()

            def fake_tick(gh, spec, *, scheduler=None):
                # Pass 1: only alpha submits a worker. Pass 2: only
                # beta submits one. The contract under test is that
                # pass 2 runs at all while alpha's worker is blocked.
                if current_pass["n"] == 1 and spec.slug == "alpha/one":
                    scheduler.submit(spec.slug, 1, slow_alpha)
                elif current_pass["n"] == 2 and spec.slug == "beta/two":
                    scheduler.submit(spec.slug, 2, quick_beta)

            try:
                with patch.object(
                    main_mod.workflow, "tick", side_effect=fake_tick,
                ):
                    current_pass["n"] = 1
                    t0 = time.monotonic()
                    main_mod._run_tick(clients, sched)
                    pass1_elapsed = time.monotonic() - t0
                    self.assertTrue(
                        alpha_started.wait(timeout=2.0),
                        "alpha worker should have started during pass 1",
                    )
                    # Pass 1 returned without waiting for alpha — the
                    # blocking worker is still running. 2.0s is far
                    # below the 5.0s worker hold to leave headroom for
                    # CI noise without being so loose the regression
                    # ("tick blocks on the handler") could sneak past.
                    self.assertLess(
                        pass1_elapsed, 2.0,
                        f"pass 1 took {pass1_elapsed:.2f}s -- _run_tick "
                        "should not wait for handler completion",
                    )

                    current_pass["n"] = 2
                    main_mod._run_tick(clients, sched)
                    self.assertTrue(
                        beta_done.wait(timeout=2.0),
                        "beta worker did not run during pass 2 while "
                        "alpha's worker was still in flight",
                    )
                    # Alpha is still mid-flight; releasing now lets it
                    # exit cleanly before the scheduler shutdown.
                    self.assertTrue(sched.is_active("alpha/one", 1))
            finally:
                alpha_release.set()

    def test_same_issue_not_launched_twice_concurrently_across_polls(
        self,
    ) -> None:
        # Pass 1 submits a blocking worker for issue #7. Pass 2 sees the
        # same issue still in flight and the scheduler's duplicate-active
        # gate rejects the re-submit so the handler is not run a second
        # time concurrently. The test asserts BOTH the call counter
        # (only one worker started) and the explicit skip return from
        # `scheduler.submit` (the contract the dispatch layer relies on).
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            sched = main_mod.IssueScheduler(
                global_cap=4, per_repo_cap=4,
            )
            self.addCleanup(sched.shutdown)
            clients = self._build_clients(main_mod, ["owner/repo"])

            started = threading.Event()
            release = threading.Event()
            run_count = {"n": 0}
            run_lock = threading.Lock()
            submit_results: list[bool] = []

            def slow_worker() -> None:
                with run_lock:
                    run_count["n"] += 1
                started.set()
                release.wait(timeout=5.0)

            def fake_tick(gh, spec, *, scheduler=None):
                submit_results.append(
                    scheduler.submit(spec.slug, 7, slow_worker)
                )

            try:
                with patch.object(
                    main_mod.workflow, "tick", side_effect=fake_tick,
                ):
                    main_mod._run_tick(clients, sched)
                    self.assertTrue(started.wait(timeout=2.0))
                    main_mod._run_tick(clients, sched)
            finally:
                release.set()

            # Pass 1 was accepted; pass 2 was rejected by the
            # duplicate-active gate.
            self.assertEqual(submit_results, [True, False])
            # The handler only ran once -- no second concurrent worker
            # was ever started while the first was still in flight.
            with run_lock:
                self.assertEqual(run_count["n"], 1)

    def test_worker_completion_clears_in_flight_marker_for_next_poll(
        self,
    ) -> None:
        # Pass 1 dispatches a worker that finishes promptly. The
        # done-callback clears the in-flight marker, so pass 2 can
        # re-dispatch the same (repo, issue) key. Two workers must run
        # over the two polls -- the first synchronously inside pass 1
        # and the second from pass 2 after the marker cleared.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            sched = main_mod.IssueScheduler(
                global_cap=4, per_repo_cap=4,
            )
            self.addCleanup(sched.shutdown)
            clients = self._build_clients(main_mod, ["owner/repo"])

            done_events: list[threading.Event] = []
            run_count = {"n": 0}
            run_lock = threading.Lock()
            submit_results: list[bool] = []

            def quick_worker() -> None:
                with run_lock:
                    run_count["n"] += 1
                done_events[-1].set()

            def fake_tick(gh, spec, *, scheduler=None):
                done_events.append(threading.Event())
                submit_results.append(
                    scheduler.submit(spec.slug, 3, quick_worker)
                )

            with patch.object(
                main_mod.workflow, "tick", side_effect=fake_tick,
            ):
                main_mod._run_tick(clients, sched)
                self.assertTrue(done_events[-1].wait(timeout=2.0))
                # Spin briefly for the done-callback to clear the
                # marker; with the callback running on a background
                # thread, "worker finished" and "marker cleared" are
                # distinct events.
                deadline = time.monotonic() + 2.0
                while sched.is_active("owner/repo", 3):
                    if time.monotonic() > deadline:
                        self.fail(
                            "in-flight marker not cleared after worker "
                            "exit",
                        )
                    time.sleep(0.01)
                self.assertFalse(sched.is_active("owner/repo", 3))
                main_mod._run_tick(clients, sched)
                self.assertTrue(done_events[-1].wait(timeout=2.0))

            self.assertEqual(submit_results, [True, True])
            with run_lock:
                self.assertEqual(run_count["n"], 2)

    def test_reap_called_once_per_polling_pass_in_single_repo_mode(
        self,
    ) -> None:
        # Failures recorded in the scheduler's completion queue between
        # polling passes must be drained on the next pass so they
        # actually reach the orchestrator log. The cadence is
        # symmetrical with the analytics retention pass: exactly one
        # `scheduler.reap()` per `_run_tick` regardless of how many
        # repos are configured.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            sched = main_mod.IssueScheduler(
                global_cap=4, per_repo_cap=4,
            )
            self.addCleanup(sched.shutdown)
            clients = self._build_clients(main_mod, ["owner/legacy"])

            with patch.object(
                main_mod.workflow, "tick",
            ) as fake_tick, patch.object(sched, "reap") as reap:
                fake_tick.return_value = None
                main_mod._run_tick(clients, sched)

            # Exactly one reap per polling pass.
            self.assertEqual(reap.call_count, 1)

    def test_reap_called_once_per_polling_pass_in_multi_repo_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop"
            ),
        }) as main_mod:
            sched = main_mod.IssueScheduler(
                global_cap=4, per_repo_cap=4,
            )
            self.addCleanup(sched.shutdown)
            clients = self._build_clients(
                main_mod, ["alpha/one", "beta/two"],
            )

            with patch.object(
                main_mod.workflow, "tick",
            ) as fake_tick, patch.object(sched, "reap") as reap:
                fake_tick.return_value = None
                main_mod._run_tick(clients, sched)

            # One reap for the whole polling pass, not per repo.
            self.assertEqual(reap.call_count, 1)

    def test_reap_called_once_across_real_workflow_dispatch_multi_repo(
        self,
    ) -> None:
        # Regression for the multi-repo reap-cadence violation: the
        # tests above stub out `main_mod.workflow.tick`, so the real
        # `_dispatch_via_scheduler` never runs and a reap call hiding
        # inside the production workflow path would not be counted.
        # This test exercises the real `workflow.tick` (with the
        # pollable-issue list patched to empty and the pre-tick refresh
        # stubbed to a no-op so no GitHub I/O fires) and asserts the
        # total reap count is still exactly one. Before the fix, an
        # earlier draft also reaped inside `_dispatch_via_scheduler`,
        # which produced N+1 reaps under multi-repo `REPOS` and
        # contradicted the documented "one reap per polling pass"
        # contract.
        from orchestrator import workflow as workflow_mod
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop"
            ),
        }) as main_mod:
            sched = main_mod.IssueScheduler(
                global_cap=4, per_repo_cap=4,
            )
            self.addCleanup(sched.shutdown)
            clients = self._build_clients(
                main_mod, ["alpha/one", "beta/two"],
            )
            for _spec, gh in clients:
                gh.list_pollable_issues.return_value = iter([])

            with patch.object(
                workflow_mod, "_refresh_base_and_worktrees",
            ), patch.object(sched, "reap") as reap:
                main_mod._run_tick(clients, sched)

            # The real `_dispatch_via_scheduler` is exercised this time;
            # only `_run_tick`'s own reap should be counted.
            self.assertEqual(reap.call_count, 1)


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

    _LEGACY = {
        "REPO": "owner/legacy",
        "TARGET_REPO_ROOT": "/tmp",
        "BASE_BRANCH": "trunk",
    }

    def test_shutdown_arms_watchdog(self) -> None:
        with _reload_main(self._LEGACY) as main_mod:
            with patch.object(main_mod, "_arm_shutdown_watchdog") as arm:
                main_mod._shutdown(signal.SIGTERM, None)
            arm.assert_called_once_with(signal.SIGTERM)

    def test_watchdog_force_exits_when_drain_overruns(self) -> None:
        with _reload_main(self._LEGACY) as main_mod:
            main_mod._shutdown_complete.clear()
            forced: list[int] = []
            with patch.object(
                main_mod, "_force_exit",
                side_effect=lambda s: forced.append(s),
            ), patch.object(main_mod.config, "SHUTDOWN_GRACE_SECONDS", 0.05):
                main_mod._run_shutdown_watchdog(signal.SIGTERM)
            self.assertEqual(forced, [signal.SIGTERM])

    def test_watchdog_returns_clean_when_drain_completes(self) -> None:
        with _reload_main(self._LEGACY) as main_mod:
            # Drain already finished: the watchdog must return without ever
            # touching the process even though grace has not elapsed.
            main_mod._shutdown_complete.set()
            forced: list[int] = []
            with patch.object(
                main_mod, "_force_exit",
                side_effect=lambda s: forced.append(s),
            ), patch.object(main_mod.config, "SHUTDOWN_GRACE_SECONDS", 5):
                main_mod._run_shutdown_watchdog(signal.SIGTERM)
            self.assertEqual(forced, [])

    def test_force_exit_terminates_agents_then_hard_exits(self) -> None:
        with _reload_main(self._LEGACY) as main_mod:
            with patch.object(
                main_mod.agents, "terminate_all_running",
            ) as term, patch.object(
                main_mod.os, "_exit", side_effect=RuntimeError("exit"),
            ) as os_exit:
                with self.assertRaises(RuntimeError):
                    main_mod._force_exit(signal.SIGTERM)
            # The sweep is bounded by the reserved terminate grace -- NOT the
            # default 5s -- so the watchdog path cannot push total exit past
            # `SHUTDOWN_GRACE_SECONDS` (the Finding-1 ceiling contract).
            term.assert_called_once_with(
                grace=main_mod._shutdown_terminate_grace(),
            )
            os_exit.assert_called_once_with(128 + signal.SIGTERM)

    def test_watchdog_drain_window_reserves_terminate_grace(self) -> None:
        # The hard ceiling is `SHUTDOWN_GRACE_SECONDS`. `_force_exit`'s own
        # SIGTERM->SIGKILL sweep takes up to `_shutdown_terminate_grace()`, so
        # the watchdog must wait only `grace - reserve` for the drain; adding
        # the sweep on top of the full grace would overrun the ceiling by the
        # sweep's grace. Capture the timeout the watchdog waits on to prove
        # drain_window + sweep_reserve == SHUTDOWN_GRACE_SECONDS.
        with _reload_main(self._LEGACY) as main_mod:
            captured: dict[str, float] = {}
            fake_event = MagicMock()

            def fake_wait(timeout=None):
                captured["timeout"] = timeout
                return True  # drain reported complete; no force-exit

            fake_event.wait.side_effect = fake_wait
            with patch.object(main_mod, "_shutdown_complete", fake_event), \
                 patch.object(main_mod.config, "SHUTDOWN_GRACE_SECONDS", 30):
                main_mod._run_shutdown_watchdog(signal.SIGTERM)
            reserve = main_mod._shutdown_terminate_grace()
            self.assertEqual(captured["timeout"], 30 - reserve)
            self.assertLessEqual(captured["timeout"] + reserve, 30)

    def test_terminate_grace_capped_and_within_budget(self) -> None:
        # The reserve is a slice of the budget, never the whole of it (which
        # would starve the drain) and never more than 5s for a large grace.
        with _reload_main(self._LEGACY) as main_mod:
            for grace in (1, 2, 10, 30, 3600):
                with patch.object(
                    main_mod.config, "SHUTDOWN_GRACE_SECONDS", grace,
                ):
                    reserve = main_mod._shutdown_terminate_grace()
                self.assertGreater(reserve, 0.0)
                self.assertLess(reserve, grace)
                self.assertLessEqual(reserve, 5.0)

    def test_signal_exit_terminates_in_flight_agents(self) -> None:
        with _reload_main(self._LEGACY) as main_mod:
            def fake_tick(gh, spec, *, scheduler=None):
                main_mod._shutdown(signal.SIGTERM, None)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "_arm_shutdown_watchdog"), \
                 patch.object(
                     main_mod.agents, "terminate_all_running",
                 ) as term, \
                 patch.object(
                     main_mod, "GitHubClient", side_effect=fake_client,
                 ), \
                 patch.object(
                     main_mod.workflow, "tick", side_effect=fake_tick,
                 ):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 128 + signal.SIGTERM)
            term.assert_called_once_with()

    def test_normal_exit_does_not_terminate_agents(self) -> None:
        # The non-signal paths (`--once` finishing, self-modifying-merge
        # restart) must keep the existing "let in-flight work finish" drain
        # -- only a signal stop, which is under the systemd deadline, kills
        # agents up front.
        with _reload_main(self._LEGACY) as main_mod:
            def fake_tick(gh, spec, *, scheduler=None):
                pass

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(
                main_mod.agents, "terminate_all_running",
            ) as term, \
                 patch.object(
                     main_mod, "GitHubClient", side_effect=fake_client,
                 ), \
                 patch.object(
                     main_mod.workflow, "tick", side_effect=fake_tick,
                 ):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            term.assert_not_called()


if __name__ == "__main__":
    unittest.main()
