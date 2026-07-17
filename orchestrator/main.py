# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop entry point.

Run with `python -m orchestrator.main` (or `--once` for a single tick).

The loop self-exits when it detects a merge to origin/main that touches its
own source files, so the wrapper script can pick up the new code.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

from orchestrator import agents, analytics, config, workflow
from orchestrator.github import GitHubClient
from orchestrator.scheduler import IssueScheduler

log = logging.getLogger("orchestrator")

running = True
received_signal: Optional[int] = None
active_scheduler: Optional[IssueScheduler] = None
# Set once `main`'s shutdown drain has finished. The shutdown watchdog waits
# on this with a timeout so it only force-exits when the drain genuinely
# overran the grace window -- a clean fast drain sets it and the watchdog
# returns without touching the process.
_shutdown_complete = threading.Event()


def _shutdown(signum, _frame) -> None:
    """Stop after the current tick, and re-arm the kernel default handler so a
    second Ctrl+C kills the process immediately. Recording `received_signal`
    lets `main()` return `128 + signum`, which `run.sh` keys on to skip the
    restart loop -- otherwise a graceful SIGINT exit (code 0) is
    indistinguishable from a self-modifying-merge restart.

    Also calls `scheduler.shutdown(wait=False)` so the scheduler's submit
    path is closed BEFORE the in-progress tick returns. `running=False`
    alone only stops the next tick boundary -- an iterating
    `workflow.tick` would otherwise keep calling `scheduler.submit` for
    the remainder of its dispatch loop after the signal already fired,
    enqueueing per-issue handlers we are about to wait on in the
    finally block. With the early shutdown those submits flip to
    `reason=closed` and the tick drains what it has instead of growing
    the in-flight set after the user already asked to stop. The
    follow-up `scheduler.shutdown(wait=True)` in `main`'s finally still
    blocks on the executor + runs the trailing reap, so failures from
    the workers that DID start are still logged.
    """
    global running, received_signal
    if received_signal is not None:
        return
    received_signal = signum
    log.info("signal %s received; will stop after this tick", signum)
    running = False
    sched = active_scheduler
    if sched is not None:
        try:
            sched.shutdown(wait=False)
        except Exception:
            # Signal handlers must not raise -- a failure here would
            # leave the process in a partially-shutdown state with the
            # default handler already re-armed (see below). Surface the
            # reason and continue; the finally-block `shutdown(wait=True)`
            # in `main` will retry the close + drain.
            log.exception(
                "signal handler scheduler.shutdown(wait=False) failed",
            )
    # Arm the bounded-exit backstop. The cooperative drain in `main` only
    # advances at tick boundaries and then blocks on `scheduler.shutdown`,
    # so a tick wedged in a long GitHub retry loop or a worker parked in a
    # 30-minute agent subprocess would otherwise hold the process well past
    # systemd's `TimeoutStopSec` and earn a SIGKILL. The watchdog guarantees
    # we exit within `SHUTDOWN_GRACE_SECONDS` no matter what any thread is
    # blocked on.
    _arm_shutdown_watchdog(signum)
    try:
        signal.signal(signum, signal.SIG_DFL)
    except (OSError, ValueError):
        pass


def _arm_shutdown_watchdog(signum: int) -> None:
    """Start the daemon watchdog that force-exits if the drain overruns."""
    threading.Thread(
        target=_run_shutdown_watchdog,
        args=(signum,),
        name="shutdown-watchdog",
        daemon=True,
    ).start()


# Hard cap on the shutdown-budget slice reserved for the forced terminate
# sweep (`_shutdown_terminate_grace`). Matches the default grace
# `agents.terminate_all_running` gives a SIGTERM'd group before it SIGKILLs,
# so the reserve covers one full sweep of a child that ignores SIGTERM.
_TERMINATE_SWEEP_RESERVE_CAP_SECONDS = 5.0

# Shell convention for a process killed by a signal: exit code 128 + signal
# number. `run.sh` reads this to distinguish a signalled stop from a clean one.
_SIGNAL_EXIT_BASE = 128


def _shutdown_terminate_grace() -> float:
    """Slice of `SHUTDOWN_GRACE_SECONDS` reserved for the forced terminate sweep.

    `SHUTDOWN_GRACE_SECONDS` is documented and configured as a HARD ceiling on
    total signal->exit time. `_force_exit`'s `terminate_all_running` sweep
    itself takes up to its own grace to SIGTERM-then-SIGKILL a child that
    ignores SIGTERM, so that time must come OUT OF the budget rather than be
    added on top -- otherwise actual exit is `SHUTDOWN_GRACE_SECONDS` + sweep
    grace, overrunning the ceiling. The watchdog spends the remainder on the
    cooperative drain; this reserve bounds the sweep. Capped at half the
    budget so a small `SHUTDOWN_GRACE_SECONDS` still leaves the drain the
    larger share, and is never the full budget (which would leave the drain
    no window at all).
    """
    return min(_TERMINATE_SWEEP_RESERVE_CAP_SECONDS, config.SHUTDOWN_GRACE_SECONDS / 2)


def _run_shutdown_watchdog(signum: int) -> None:
    # Returns immediately once the drain completes; only force-exits when the
    # grace window elapses first. Daemon thread, so a clean exit tears it down
    # before it can fire. The drain gets `SHUTDOWN_GRACE_SECONDS` minus the
    # reserved terminate grace so that the subsequent `_force_exit` sweep fits
    # INSIDE the ceiling -- total signal->exit stays within
    # `SHUTDOWN_GRACE_SECONDS` even when an agent ignores SIGTERM.
    drain_budget = max(
        0, config.SHUTDOWN_GRACE_SECONDS - _shutdown_terminate_grace()
    )
    if _shutdown_complete.wait(timeout=drain_budget):
        return
    _force_exit(signum)


def _force_exit(signum: int) -> None:
    """Last resort: kill in-flight agents, then hard-exit with the signal code.

    `os._exit` skips interpreter cleanup (atexit, buffer flush) on purpose --
    the point is to leave even if a thread is wedged in an uninterruptible
    C call. Agent and verify process groups are terminated first so they are
    not orphaned past the parent. The sweep is bounded by
    `_shutdown_terminate_grace()`, the slice of the budget the watchdog held
    back, so this path cannot push total exit beyond `SHUTDOWN_GRACE_SECONDS`.
    """
    log.warning(
        "shutdown grace (%ss) expired; terminating agents and forcing exit",
        config.SHUTDOWN_GRACE_SECONDS,
    )
    try:
        agents.terminate_all_running(grace=_shutdown_terminate_grace())
    finally:
        os._exit(_SIGNAL_EXIT_BASE + signum)


def _rotating_file_handler() -> logging.Handler:
    """Build the rotating file handler, creating `config.LOG_DIR` first."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    return logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "orchestrator.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )


def _configure_logging(level: str) -> None:
    # stderr stays for live tailing in `run.sh`'s terminal; the file handler
    # is what survives terminal close. RotatingFileHandler caps disk use
    # without needing logrotate on the host.
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(_rotating_file_handler())
    except OSError as err:
        # Don't refuse to start just because the log dir is unwritable;
        # stderr alone keeps the loop usable. Surface the reason once.
        logging.basicConfig(level=level, format=fmt, handlers=handlers)
        logging.getLogger("orchestrator").warning(
            "file logging disabled: %s (%s)", config.LOG_DIR, err
        )
        return
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(config.REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _own_head_sha() -> Optional[str]:
    head_rev = _git("rev-parse", "HEAD")
    return head_rev.stdout.strip() if head_rev.returncode == 0 else None


def _self_modifying_merge_happened(start_sha: str) -> bool:
    """Detect that origin/<orchestrator-base> has moved FORWARD from start_sha
    and the new commits touch orchestrator/. Watches the orchestrator's own
    repo (REPO_ROOT), not the target repo, so a separately-configured target
    branch (e.g. `master`) does not interfere with self-update detection.
    """
    _git("fetch", "--quiet", "origin", config.ORCHESTRATOR_BASE_BRANCH)
    cur = _git("rev-parse", f"origin/{config.ORCHESTRATOR_BASE_BRANCH}").stdout.strip()
    if not cur or cur == start_sha:
        return False
    # start_sha must be an ancestor of origin/main for this to be a merge that
    # advanced the upstream ref past where we started.
    if _git("merge-base", "--is-ancestor", start_sha, cur).returncode != 0:
        return False
    diff = _git("diff", "--name-only", start_sha, cur).stdout
    return any(line.startswith("orchestrator/") for line in diff.splitlines())


@dataclass(frozen=True)
class _MainOptions:
    once: bool
    log_level: str


def _parse_main_options(argv: Optional[list[str]]) -> _MainOptions:
    parser = argparse.ArgumentParser(
        description="Agent orchestrator polling loop."
    )
    parser.add_argument(
        "--once", action="store_true", help="Run a single tick and exit."
    )
    parser.add_argument("--log-level", default="INFO")
    parsed = parser.parse_args(argv)
    return _MainOptions(once=parsed.once, log_level=parsed.log_level)


def _connect_clients() -> list[tuple[config.RepoSpec, GitHubClient]]:
    """Connect once per configured repository and ensure workflow labels."""
    clients: list[tuple[config.RepoSpec, GitHubClient]] = []
    for spec in config.default_repo_specs():
        gh = GitHubClient(repo_spec=spec)
        log.info("connected: repo=%s", spec.slug)
        gh.ensure_workflow_labels()
        clients.append((spec, gh))
    return clients


def _create_scheduler() -> IssueScheduler:
    """Build the process-wide scheduler shared by every polling tick."""
    return IssueScheduler(
        global_cap=config.MAX_PARALLEL_ISSUES_GLOBAL,
        per_repo_cap=config.MAX_PARALLEL_ISSUES_PER_REPO,
        thread_name_prefix="orch-issue",
    )


def _activate_scheduler(scheduler: IssueScheduler) -> None:
    # Publish the scheduler to `_shutdown` BEFORE the first tick runs so
    # a signal that arrives during tick 1 can close the submit path
    # immediately instead of waiting for `_run_tick` to return. The
    # signal handlers themselves were registered earlier; until this
    # assignment lands an early signal still sets `received_signal` and
    # `running=False` but cannot close the scheduler -- that window is
    # the brief gap between scheduler construction and this line and is
    # acceptable because no tick has dispatched anything yet.
    global active_scheduler
    active_scheduler = scheduler


def _wait_for_next_tick() -> None:
    for _ in range(config.POLL_INTERVAL):
        if not running:
            return
        time.sleep(1)


def _run_polling_loop(
    clients: list[tuple[config.RepoSpec, GitHubClient]],
    scheduler: IssueScheduler,
) -> Optional[int]:
    own_sha = _own_head_sha()
    log.info("own HEAD=%s", own_sha)
    while running:
        if own_sha and _self_modifying_merge_happened(own_sha):
            log.info("self-modifying merge detected; exiting for restart")
            return 0
        _run_tick(clients, scheduler)
        _wait_for_next_tick()
    return None


def _drive_main_loop(
    options: _MainOptions,
    clients: list[tuple[config.RepoSpec, GitHubClient]],
    scheduler: IssueScheduler,
) -> Optional[int]:
    if options.once:
        _run_tick(clients, scheduler)
        return None
    return _run_polling_loop(clients, scheduler)


def _drain_scheduler(scheduler: IssueScheduler) -> None:
    """Stop agent groups when signaled, then wait for every worker."""
    global active_scheduler
    if received_signal is not None:
        # Worker threads cannot drain while their agent subprocess is still
        # allowed to run up to `AGENT_TIMEOUT`.
        agents.terminate_all_running()
    # Repeatable after `_shutdown`'s `wait=False` close; this call owns the
    # final worker wait and completion reap.
    scheduler.shutdown(wait=True)
    active_scheduler = None
    _shutdown_complete.set()


def _signal_exit_code() -> int:
    if received_signal is not None:
        return _SIGNAL_EXIT_BASE + received_signal
    return 0


@contextlib.contextmanager
def _scheduler_drained(scheduler: IssueScheduler):
    """Guarantee the scheduler is drained once the wrapped block exits, even
    if the main loop raises."""
    try:
        yield
    finally:
        _drain_scheduler(scheduler)


def main(argv: Optional[list[str]] = None) -> int:
    options = _parse_main_options(argv)
    _configure_logging(options.log_level)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    clients = _connect_clients()
    scheduler = _create_scheduler()
    _activate_scheduler(scheduler)
    restart_exit_code: Optional[int] = None
    with _scheduler_drained(scheduler):
        restart_exit_code = _drive_main_loop(options, clients, scheduler)
    if restart_exit_code is not None:
        return restart_exit_code
    return _signal_exit_code()


def _tick_one_repo(
    spec: config.RepoSpec, gh: GitHubClient, scheduler: IssueScheduler,
) -> None:
    """Drive one repo's `workflow.tick`, isolating shutdown and failures.

    Re-checks `running` first so a signal that arrived between submission
    and this call actually starting still skips the tick instead of forcing
    the user to wait through a slow `workflow.tick` after they hit Ctrl+C. A
    per-repo exception is caught and logged so one failing repo cannot stop
    the others from advancing this tick. Shared by the single-repo in-thread
    path and every multi-repo fan-out worker.
    """
    if not running:
        log.info(
            "repo=%s shutdown requested before tick start; skipping",
            spec.slug,
        )
        return
    log.info("tick: repo=%s", spec.slug)
    try:
        workflow.tick(gh, spec, scheduler=scheduler)
    except Exception:
        log.exception("tick failed for repo=%s; continuing", spec.slug)


def _fan_out_repo_ticks(
    clients: list[tuple[config.RepoSpec, GitHubClient]],
    scheduler: IssueScheduler,
) -> None:
    """Run every configured repo's tick concurrently across a thread pool.

    A slow repo does not delay the others' progress -- the orchestrator's
    whole point is to keep advancing every configured repo each tick. Each
    worker (`_tick_one_repo`) already catches its own exceptions; reaching
    the `fut.result()` raise here indicates a programming-level failure in
    the worker itself, which we still want to log loudly.
    """
    with ThreadPoolExecutor(
        max_workers=len(clients),
        thread_name_prefix="orch-repo",
    ) as ex:
        futures = {
            ex.submit(_tick_one_repo, spec, gh, scheduler): spec.slug
            for spec, gh in clients
        }
        # `as_completed` so the loop logs a stuck repo as soon as the others
        # finish, instead of waiting for the slowest.
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                log.exception(
                    "repo=%s tick worker raised unexpectedly",
                    futures[fut],
                )


def _run_tick(
    clients: list[tuple[config.RepoSpec, GitHubClient]],
    scheduler: IssueScheduler,
) -> None:
    """Drive a single tick across every configured repo.

    With one configured repo the call stays in-thread to keep the legacy
    single-repo deployment unchanged (no extra repo-fanout executor; the
    scheduler still drives per-issue work on its own internal threads). With
    multiple configured repos the per-repo `workflow.tick` invocations are
    fanned out across a ThreadPoolExecutor. `scheduler` is threaded through
    either way so the cross-repo / per-repo caps, duplicate-active-issue skip,
    and family-aware mutex stay enforced across concurrent per-repo ticks;
    shared between `--once` and the polling loop so both paths behave
    identically.

    `scheduler.reap()` and `analytics.prune_with_retention_logging()` run
    exactly once at the end of the pass regardless of repo count.
    `workflow.tick` returns as soon as it has submitted the eligible-issue
    callables, so this single drain is what surfaces "submitted on tick N,
    failed before tick N+1" worker failures on the next pass -- keeping the
    documented "one reap per polling pass" cadence, alongside the analytics
    retention pass. `_dispatch_via_scheduler` deliberately does NOT reap.
    """
    if not clients:
        return
    if len(clients) == 1:
        spec, gh = clients[0]
        _tick_one_repo(spec, gh, scheduler)
    else:
        _fan_out_repo_ticks(clients, scheduler)
    scheduler.reap()
    analytics.prune_with_retention_logging()


if __name__ == "__main__":
    sys.exit(main())
