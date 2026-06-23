# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop entry point.

Run with `python -m orchestrator.main` (or `--once` for a single tick).

The loop self-exits when it detects a merge to origin/main that touches its
own source files, so the wrapper script can pick up the new code.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from . import agents, analytics, config, workflow
from .github import GitHubClient
from .scheduler import IssueScheduler

log = logging.getLogger("orchestrator")

_running = True
_received_signal: Optional[int] = None
_scheduler: Optional[IssueScheduler] = None
# Set once `main`'s shutdown drain has finished. The shutdown watchdog waits
# on this with a timeout so it only force-exits when the drain genuinely
# overran the grace window -- a clean fast drain sets it and the watchdog
# returns without touching the process.
_shutdown_complete = threading.Event()


def _shutdown(signum, _frame) -> None:
    """Stop after the current tick, and re-arm the kernel default handler so a
    second Ctrl+C kills the process immediately. Recording `_received_signal`
    lets `main()` return `128 + signum`, which `run.sh` keys on to skip the
    restart loop -- otherwise a graceful SIGINT exit (code 0) is
    indistinguishable from a self-modifying-merge restart.

    Also calls `scheduler.shutdown(wait=False)` so the scheduler's submit
    path is closed BEFORE the in-progress tick returns. `_running=False`
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
    global _running, _received_signal
    if _received_signal is not None:
        return
    _received_signal = signum
    log.info("signal %s received; will stop after this tick", signum)
    _running = False
    sched = _scheduler
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
    return min(5.0, config.SHUTDOWN_GRACE_SECONDS / 2)


def _run_shutdown_watchdog(signum: int) -> None:
    # Returns immediately once the drain completes; only force-exits when the
    # grace window elapses first. Daemon thread, so a clean exit tears it down
    # before it can fire. The drain gets `SHUTDOWN_GRACE_SECONDS` minus the
    # reserved terminate grace so that the subsequent `_force_exit` sweep fits
    # INSIDE the ceiling -- total signal->exit stays within
    # `SHUTDOWN_GRACE_SECONDS` even when an agent ignores SIGTERM.
    drain_budget = max(
        0.0, config.SHUTDOWN_GRACE_SECONDS - _shutdown_terminate_grace()
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
        os._exit(128 + signum)


def _configure_logging(level: str) -> None:
    # stderr stays for live tailing in `run.sh`'s terminal; the file handler
    # is what survives terminal close. RotatingFileHandler caps disk use
    # without needing logrotate on the host.
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                config.LOG_DIR / "orchestrator.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )
    except OSError as e:
        # Don't refuse to start just because the log dir is unwritable;
        # stderr alone keeps the loop usable. Surface the reason once.
        logging.basicConfig(level=level, format=fmt, handlers=handlers)
        logging.getLogger("orchestrator").warning(
            "file logging disabled: %s (%s)", config.LOG_DIR, e
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
    r = _git("rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


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


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Agent orchestrator polling loop.")
    p.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    _configure_logging(args.log_level)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # `default_repo_specs()` returns one element for legacy single-repo
    # deployments and N elements when `REPOS` is configured. Connecting and
    # ensuring labels happens once per spec at startup; the polling loop
    # then fans `workflow.tick(gh, spec)` out across the precomputed
    # client list every tick.
    specs = config.default_repo_specs()
    clients: list[tuple[config.RepoSpec, GitHubClient]] = []
    for spec in specs:
        gh = GitHubClient(repo_spec=spec)
        log.info("connected: repo=%s", spec.slug)
        gh.ensure_workflow_labels()
        clients.append((spec, gh))

    # One `IssueScheduler` is built once at startup and reused across every
    # tick: it owns the cross-repo in-flight cap
    # (`MAX_PARALLEL_ISSUES_GLOBAL`), the default per-repo cap
    # (`MAX_PARALLEL_ISSUES_PER_REPO`, overridable per spec via
    # `parallel_limit`), the duplicate-active-issue skip, and the
    # family-aware mutex. The polling tick submits per-issue work to
    # this scheduler and returns immediately; worker threads run on the
    # scheduler's internal executor. Replaces the older
    # `BoundedSemaphore` cross-repo gate -- the scheduler's `global_cap`
    # is the authoritative bound. Shut down in the `finally` below so
    # in-flight workers complete cleanly (and any late failures are
    # logged) regardless of how the loop exits.
    scheduler = IssueScheduler(
        global_cap=config.MAX_PARALLEL_ISSUES_GLOBAL,
        per_repo_cap=config.MAX_PARALLEL_ISSUES_PER_REPO,
        thread_name_prefix="orch-issue",
    )
    # Publish the scheduler to `_shutdown` BEFORE the first tick runs so
    # a signal that arrives during tick 1 can close the submit path
    # immediately instead of waiting for `_run_tick` to return. The
    # signal handlers themselves were registered earlier; until this
    # assignment lands an early signal still sets `_received_signal` and
    # `_running=False` but cannot close the scheduler -- that window is
    # the brief gap between scheduler construction and this line and is
    # acceptable because no tick has dispatched anything yet.
    global _scheduler
    _scheduler = scheduler

    try:
        if args.once:
            _run_tick(clients, scheduler)
        else:
            own_sha = _own_head_sha()
            log.info("own HEAD=%s", own_sha)

            while _running:
                if own_sha and _self_modifying_merge_happened(own_sha):
                    log.info("self-modifying merge detected; exiting for restart")
                    return 0
                _run_tick(clients, scheduler)
                for _ in range(config.POLL_INTERVAL):
                    if not _running:
                        break
                    time.sleep(1)
    finally:
        # `wait=True` so any in-flight worker (e.g. a `--once` invocation
        # that just submitted long-running handlers) finishes before the
        # process returns. Without this, `--once` could exit while
        # workers were still executing and the executor's daemon threads
        # would be torn down mid-handler; the polling loop case is the
        # same property under SIGTERM/SIGINT shutdown. Safe to call even
        # when `_shutdown` already ran a `wait=False` shutdown -- the
        # scheduler's `shutdown` is documented as repeatable, the second
        # call still waits for in-flight workers to exit, and the
        # trailing reap drains any completion that landed in between.
        if _received_signal is not None:
            # Signal-initiated stop runs under systemd's `TimeoutStopSec`.
            # Kill in-flight agent subprocesses up front so their worker
            # threads unwind now instead of holding the drain below for up
            # to `AGENT_TIMEOUT` -- this is what makes the common
            # "restart while an agent is running" case exit in seconds
            # rather than timing out into a SIGKILL. Idempotent with the
            # watchdog's own sweep.
            agents.terminate_all_running()
        scheduler.shutdown(wait=True)
        _scheduler = None
        # Release the watchdog: the drain is done, so a clean exit must not
        # be pre-empted by a force-exit.
        _shutdown_complete.set()

    if _received_signal is not None:
        return 128 + _received_signal
    return 0


def _run_tick(
    clients: list[tuple[config.RepoSpec, GitHubClient]],
    scheduler: IssueScheduler,
) -> None:
    """Drive a single tick across every configured repo.

    With one configured repo the call stays in-thread to keep the legacy
    single-repo deployment unchanged (no extra repo-fanout executor; the
    scheduler still drives per-issue work on its own internal threads).
    With multiple configured repos the per-repo `workflow.tick`
    invocations are fanned out across a ThreadPoolExecutor so a slow
    repo does not delay the others' progress -- the orchestrator's
    whole point is to keep advancing every configured repo each tick.

    Per-repo exceptions are caught and logged so one failing repo cannot
    stop the others from advancing this tick; `scheduler` is threaded
    through so the cross-repo / per-repo caps, duplicate-active-issue
    skip, and family-aware mutex stay enforced across concurrent
    per-repo ticks. Shared between `--once` and the polling loop so
    both paths fan out identically.
    """
    if not clients:
        return

    if len(clients) == 1:
        spec, gh = clients[0]
        if not _running:
            log.info("shutdown requested; skipping tick")
            return
        log.info("tick: repo=%s", spec.slug)
        try:
            workflow.tick(gh, spec, scheduler=scheduler)
        except Exception:
            log.exception("tick failed for repo=%s; continuing", spec.slug)
        # Reap any worker completions that landed since the last poll.
        # `workflow.tick` itself returns as soon as it has submitted the
        # eligible-issue callables, so the loop below would otherwise see
        # worker failures only on the polling pass that happens to share
        # a tick with the worker exit. Draining once per polling pass
        # makes "submitted on tick N, failed before tick N+1" surface on
        # tick N+1 deterministically, alongside the analytics retention
        # pass below.
        scheduler.reap()
        analytics.prune_with_retention_logging()
        return

    def _tick_one(spec: config.RepoSpec, gh: GitHubClient) -> None:
        # Re-check shutdown inside the worker: a signal that arrived
        # between submission and the worker actually starting still skips
        # the tick instead of forcing the user to wait through a slow
        # `workflow.tick` after they hit Ctrl+C.
        if not _running:
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

    with ThreadPoolExecutor(
        max_workers=len(clients),
        thread_name_prefix="orch-repo",
    ) as ex:
        futures = {
            ex.submit(_tick_one, spec, gh): spec.slug for spec, gh in clients
        }
        # `as_completed` so the loop logs a stuck repo as soon as the
        # others finish, instead of waiting for the slowest. Each future's
        # body already catches its own exceptions; reaching the
        # `fut.result()` raise here indicates a programming-level failure
        # in `_tick_one` itself, which we still want to log loudly.
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                log.exception(
                    "repo=%s tick worker raised unexpectedly",
                    futures[fut],
                )
    # Reap any worker completions that landed since the last poll.
    # `_dispatch_via_scheduler` deliberately does NOT reap, so this is
    # the single per-polling-pass drain point: one reap per tick
    # regardless of repo count, paired with the analytics retention
    # call below for the same cadence.
    scheduler.reap()
    analytics.prune_with_retention_logging()


if __name__ == "__main__":
    sys.exit(main())
