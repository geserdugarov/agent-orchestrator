# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow tick."""
from __future__ import annotations

from orchestrator import _workflow_state as _state
from orchestrator import workflow as _owner

_PollablePartition = _owner._PollablePartition
Any = _owner.Any
GitHubClient = _owner.GitHubClient
IssueScheduler = _owner.IssueScheduler
Optional = _owner.Optional
ThreadPoolExecutor = _owner.ThreadPoolExecutor
as_completed = _owner.as_completed
config = _owner.config
contextlib = _owner.contextlib
dataclass = _owner.dataclass
threading = _owner.threading
_PROCESSING_FAILED_LOG = _state._PROCESSING_FAILED_LOG
log = _state.log


def _run_sequential_tick(
    gh: GitHubClient,
    spec: config.RepoSpec,
    semaphore_cm: contextlib.AbstractContextManager,
) -> None:
    """Process this tick's pollable issues one at a time on the caller thread.

    `parallel_limit == 1` (the legacy default) streams directly over
    `gh.list_pollable_issues()` rather than materializing the list first.
    Materializing would change observable behavior on a partial enumeration
    failure (e.g. a PyGithub pagination error mid-sweep): the sequential loop
    processes everything yielded BEFORE the failure, but a `list(...)` upfront
    would lose every already-yielded issue when the generator raises. Each
    `_process_issue` is wrapped in its own try/except so one raising issue
    cannot stop the rest.
    """
    for issue in gh.list_pollable_issues():
        try:
            with semaphore_cm:
                _owner._process_issue(gh, spec, issue)
        except Exception:
            log.exception(
                _PROCESSING_FAILED_LOG,
                spec.slug, issue.number,
            )


def _drain_family_bucket(
    gh: GitHubClient,
    spec: config.RepoSpec,
    family_numbers: list[int],
    *,
    semaphore_cm: contextlib.AbstractContextManager,
) -> None:
    """Process this tick's family-aware issues sequentially on one thread.

    The parallel path submits the whole family bucket as ONE executor task so
    its footprint stays at exactly one worker slot regardless of how many
    family-aware issues are pending, leaving the other `limit - 1` slots free
    for fanout. Per-issue exception isolation lives INSIDE this loop (one
    try/except per issue) so the bucket keeps draining if any single family
    handler raises; the function itself never raises, so the caller's
    `fut.result()` only ever surfaces a programming-level failure.
    """
    for issue_number in family_numbers:
        try:
            _owner._refetch_and_process(
                gh, spec, issue_number, semaphore_cm=semaphore_cm,
            )
        except Exception:
            log.exception(
                _PROCESSING_FAILED_LOG,
                spec.slug, issue_number,
            )


@dataclass(frozen=True)
class _ParallelTickPlan:
    gh: GitHubClient
    spec: config.RepoSpec
    partition: _PollablePartition
    semaphore_cm: contextlib.AbstractContextManager

    @property
    def task_count(self) -> int:
        family_count = 1 if self.partition.family_numbers else 0
        return family_count + len(self.partition.fanout_numbers)

    def submit(self, executor) -> tuple[dict[Any, Any], object]:
        family_sentinel: object = object()
        futures: dict[Any, Any] = {}
        if self.partition.family_numbers:
            futures[
                executor.submit(
                    _owner._drain_family_bucket,
                    self.gh,
                    self.spec,
                    self.partition.family_numbers,
                    semaphore_cm=self.semaphore_cm,
                )
            ] = family_sentinel
        for issue_number in self.partition.fanout_numbers:
            futures[
                executor.submit(
                    _owner._refetch_and_process,
                    self.gh,
                    self.spec,
                    issue_number,
                    semaphore_cm=self.semaphore_cm,
                )
            ] = issue_number
        return futures, family_sentinel


def _drain_parallel_futures(
    spec: config.RepoSpec,
    futures: dict[Any, Any],
    family_sentinel: object,
) -> None:
    for future in as_completed(futures):
        tag = futures[future]
        try:
            future.result()
        except Exception:
            if tag is family_sentinel:
                # Per-issue failures are caught by the family drain itself;
                # only a programming-level drain failure reaches this path.
                log.exception(
                    "repo=%s family bucket drain raised (programming "
                    "error -- per-issue exceptions are handled inside "
                    "the drain)", spec.slug,
                )
            else:
                log.exception(
                    _PROCESSING_FAILED_LOG, spec.slug, tag,
                )


def _run_parallel_tick(
    gh: GitHubClient,
    spec: config.RepoSpec,
    limit: int,
    semaphore_cm: contextlib.AbstractContextManager,
) -> None:
    """Fan this tick's pollable issues out across a bounded thread pool.

    Family-aware (cross-issue writer) work is partitioned off from fanout so
    the family bucket drains sequentially inside ONE task while the rest fan
    out; `_partition_pollable_issues` owns the skip-label filtering, per-issue
    label-read isolation, and the family/fanout split. Each `_process_issue`
    is independent (per-issue worktree, PinnedState, GitHub label/comment
    surface) so worker threads serialize only at the PyGithub HTTP layer,
    which is already thread-safe.

    The executor needs the full submission set up front to bound
    `max_workers`, so the generator is materialized in `_partition_pollable_issues`;
    on an enumeration failure the whole tick aborts and the next tick's
    enumeration retries. Folding the whole family bucket into one drain task
    caps its footprint at exactly one executor slot regardless of how many
    family-aware issues there are, leaving the other `limit - 1` slots free
    for fanout -- submitting per-family-issue futures with a shared lock would
    instead let a waiting family future occupy the other worker slot and
    starve fanout under a small `limit`.
    """
    plan = _ParallelTickPlan(
        gh, spec, _owner._partition_pollable_issues(gh, spec), semaphore_cm,
    )
    if plan.task_count == 0:
        return
    slug_token = spec.slug.replace("/", "__")
    # max_workers is capped at `limit` AND at the submitted-task count so a
    # quiet tick (e.g. one fan-out issue) does not spin up idle worker threads.
    with ThreadPoolExecutor(
        max_workers=min(limit, plan.task_count),
        thread_name_prefix=f"orch-{slug_token}",
    ) as executor:
        futures, family_sentinel = plan.submit(executor)
        # `as_completed` so a slow issue does not delay logging the failures
        # of faster ones. Each `fut.result()` is wrapped individually so one
        # raising issue cannot abort the remaining futures' result drain.
        _owner._drain_parallel_futures(spec, futures, family_sentinel)


def tick(
    gh: GitHubClient,
    spec: config.RepoSpec,
    *,
    global_semaphore: Optional[threading.BoundedSemaphore] = None,
    scheduler: Optional[IssueScheduler] = None,
) -> None:
    """Drive a single tick for one repo.

    `global_semaphore` is the cross-repo bound on concurrent per-issue
    handlers (`MAX_PARALLEL_ISSUES_GLOBAL`). It is acquired around every
    `_process_issue` call so workers from different repo ticks running
    concurrently contend on the same semaphore. None falls back to a
    no-op context manager so direct test invocations of `tick(gh, spec)`
    keep working unchanged; production code threads the shared semaphore
    in from `main._run_tick` so the cap is actually enforced.

    `scheduler`, when supplied, takes over per-issue dispatch entirely.
    The polling pass still refreshes base/worktrees and enumerates
    pollable issues, but instead of running the handlers in-tick (legacy
    in-thread loop or per-tick ThreadPoolExecutor) each accepted
    per-issue callable is submitted to the scheduler and the tick
    returns without waiting for completion. The scheduler owns the
    cross-repo in-flight cap, the per-repo cap (`spec.parallel_limit`
    is threaded in as the per-call override), the "duplicate active
    issue" skip, and the family-aware mutex. `global_semaphore` is
    ignored on this path -- the scheduler's `global_cap` is the
    authoritative cross-repo bound. None preserves the legacy in-tick
    behavior so existing direct invocations are unchanged.
    """
    try:
        # Threading the scheduler in here is what keeps an "active
        # issue" actually inert across the whole tick. The dispatch
        # path skips a duplicate submit at `scheduler.submit`, but the
        # base refresh would otherwise rebase the pre-PR worktree
        # under a still-running agent or relabel/state-mutate a
        # PR-having worktree while its handler is mid-write. The
        # refresh helper consults `scheduler.is_active` per worktree
        # so an in-flight issue's worktree and pinned state are left
        # alone until the worker exits.
        _owner._refresh_base_and_worktrees(gh, spec, scheduler=scheduler)
    except Exception:
        log.exception(
            "repo=%s pre-tick base refresh failed; continuing", spec.slug,
        )
    # Per-tick: label any open PR from an outsider author and ping HITL once.
    # Independent from the per-issue dispatch (PRs not driven by the
    # orchestrator have no pinned state to consult), so failures inside the
    # sweep are swallowed by the helper itself and cannot stop the tick.
    _owner._sweep_community_contribution_prs(gh, spec)
    # Per-tick: snapshot the target repo's skill catalog into analytics.
    # Runs after the base refresh above has fetched
    # `<remote_name>/<base_branch>` so the ls-tree reads the current base
    # ref. Producer-side observability only and internally fail-open, so a
    # missing clone / git error never stops the tick; placed before the
    # scheduler/legacy split so it fires once per tick on both paths.
    _owner._emit_repo_skill_catalog(spec)
    if scheduler is not None:
        _owner._dispatch_via_scheduler(gh, spec, scheduler)
        return
    # `parallel_limit` is the local cap on worker threads this tick spins up.
    # The host-wide `MAX_PARALLEL_ISSUES_GLOBAL` cap is enforced by
    # `global_semaphore` around each `_process_issue` call, not by shrinking
    # the worker pool: with multiple repos ticking in parallel, workers from
    # different repos may queue on the semaphore until a global slot frees up,
    # which is the whole point of a cross-repo cap. None falls back to a no-op
    # context manager so a direct test invocation of `tick(gh, spec)` keeps
    # working unchanged. `limit == 1` (the legacy default) stays sequential
    # and in-thread; `limit > 1` fans out across a bounded pool.
    limit = max(1, int(getattr(spec, "parallel_limit", 1) or 1))
    semaphore_cm = (
        contextlib.nullcontext() if global_semaphore is None else global_semaphore
    )
    if limit == 1:
        _owner._run_sequential_tick(gh, spec, semaphore_cm)
    else:
        _owner._run_parallel_tick(gh, spec, limit, semaphore_cm)
