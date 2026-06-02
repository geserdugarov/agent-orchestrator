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


if __name__ == "__main__":
    unittest.main()
