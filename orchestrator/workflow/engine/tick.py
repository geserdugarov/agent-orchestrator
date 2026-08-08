# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""One repo's polling pass: the order it drives, and how its issues execute.

`tick` is the whole per-repo unit of work, and the order of the passes it
drives is the contract. The base refresh goes first because everything after it
reads what that fetch left behind -- a handler would otherwise rebase onto the
base SHA its worktree was created at, and the skill catalog would ls-tree a
stale `<remote_name>/<base_branch>`. It is also the only pass whose failure is
caught here, because a fetch that fails must not cost the tick its issues; the
sweep and the catalog are internally fail-open and cannot raise at all.

The community sweep sits with the tick rather than in the stage tree because it
is the one pass with no per-issue home: a PR the orchestrator never opened
carries no pinned state for a handler to consult, so nothing dispatches it. The
skill-catalog emission is producer-side observability with the same shape. Both
run before the scheduler / in-tick split so they fire exactly once per tick on
either path.

Past that split the tick either hands every issue to the scheduler and returns
without waiting, or runs them itself under `parallel_limit`. The two in-tick
modes are not one loop at two widths. `limit == 1` streams
`list_pollable_issues()` directly, because materializing it first would lose
every already-yielded issue when a pagination error raises mid-sweep;
`limit > 1` must materialize (the executor needs the submission count up front
to bound `max_workers`) and accepts that an enumeration failure costs the whole
tick, which the next one retries. Both wrap each issue in its own try/except,
so one raising handler never stops the rest.

The family bucket the partition hands over is submitted as exactly ONE task no
matter how many family-aware issues are pending, so it occupies a single worker
slot and leaves the other `limit - 1` free for fanout. Per-family-issue futures
behind a shared lock would instead let a waiting family future hold a second
slot and starve fanout under a small `limit`.

Every collaborator is named on the owner that defines it, including the two
passes a test has to replace to drive a tick without a git remote or a clone:
`_refresh_base_and_worktrees` on `git/base_sync/refresh.py` and the catalog
emission on `orchestrator/skills/catalog.py`. A mock aimed at either lands on
that owner; one left on the `orchestrator.workflow` facade would let the real
fetch run.
"""
from __future__ import annotations

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional

from orchestrator import config
from orchestrator._workflow_state import _PROCESSING_FAILED_LOG, log
from orchestrator.git.base_sync import refresh as _base_refresh
from orchestrator.github.client import GitHubClient
from orchestrator.github.labels import COMMUNITY_CONTRIBUTION_LABEL
from orchestrator.scheduler import IssueScheduler
from orchestrator.skills import catalog as _catalog
from orchestrator.workflow.engine import dispatch as _dispatch


@dataclass(frozen=True)
class _CommunityContribution:
    author: str


def _community_contribution_for_pr(
    gh: GitHubClient, pr, allowed_lower: set[str],
) -> Optional[_CommunityContribution]:
    user = getattr(pr, "user", None)
    if getattr(user, "type", None) == "Bot":
        return None
    author = getattr(user, "login", None) or ""
    if author.lower() in allowed_lower:
        return None
    if gh.pr_has_label(pr, COMMUNITY_CONTRIBUTION_LABEL):
        return None
    return _CommunityContribution(author)


def _label_community_contribution(
    gh: GitHubClient,
    spec: config.RepoSpec,
    pr,
    contribution: _CommunityContribution,
) -> None:
    # The label is the dedup marker, so the ping must land first. A label
    # failure may repeat a ping; a comment failure must not suppress one.
    author = contribution.author or "unknown"
    gh.pr_comment(
        pr.number,
        f"{config.HITL_MENTIONS} community contribution from "
        f"@{author} -- please review this PR.",
    )
    gh.add_pr_label(pr, COMMUNITY_CONTRIBUTION_LABEL)
    log.info(
        "repo=%s pr=#%s author=%r pinged HITL and labeled %r",
        spec.slug, pr.number, contribution.author, COMMUNITY_CONTRIBUTION_LABEL,
    )


def _sweep_pr_contribution(
    gh: GitHubClient, spec: config.RepoSpec, pr, allowed_lower: set,
) -> None:
    """Label one open PR when its author is an outside community contributor."""
    contribution = _community_contribution_for_pr(gh, pr, allowed_lower)
    if contribution is not None:
        _label_community_contribution(gh, spec, pr, contribution)


def _sweep_community_contribution_prs(
    gh: GitHubClient, spec: config.RepoSpec
) -> None:
    """Label open PRs from authors outside ALLOWED_ISSUE_AUTHORS and ping HITL.

    No-op when ALLOWED_ISSUE_AUTHORS is empty (the default) so a single-user
    deployment keeps the legacy "anyone is trusted" behavior. When the list
    is populated, every open PR whose author is not in it earns the
    `community_contribution` label and a one-shot HITL ping comment; the
    label is idempotent (already-labeled PRs are skipped) so the comment
    fires exactly once per PR.

    Bot-authored PRs (Dependabot, Renovate, CI bots) are skipped by
    GitHub's `user.type == "Bot"` flag -- they open PRs structurally and
    are not community contributions, so they never earn the label or ping.

    All errors are caught and logged: a PyGithub lazy-load failure on one
    PR must not abort the rest of the sweep, and the sweep itself must not
    abort the polling tick.
    """
    allowed = config.ALLOWED_ISSUE_AUTHORS
    if not allowed:
        return
    allowed_lower = {github_handle.lower() for github_handle in allowed}
    try:
        prs = list(gh.iter_open_prs())
    except Exception:
        log.exception(
            "repo=%s community-contribution sweep: open-PR enumeration failed",
            spec.slug,
        )
        return
    for pr in prs:
        try:
            _sweep_pr_contribution(gh, spec, pr, allowed_lower)
        except Exception:
            log.exception(
                "repo=%s pr=#%s community-contribution sweep step failed; continuing",
                spec.slug, getattr(pr, "number", "?"),
            )


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
                _dispatch._process_issue(gh, spec, issue)
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
            _dispatch._refetch_and_process(
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
    partition: _dispatch._PollablePartition
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
                    _drain_family_bucket,
                    self.gh,
                    self.spec,
                    self.partition.family_numbers,
                    semaphore_cm=self.semaphore_cm,
                )
            ] = family_sentinel
        for issue_number in self.partition.fanout_numbers:
            futures[
                executor.submit(
                    _dispatch._refetch_and_process,
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
        gh, spec, _dispatch._partition_pollable_issues(gh, spec), semaphore_cm,
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
        _drain_parallel_futures(spec, futures, family_sentinel)


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
        _base_refresh._refresh_base_and_worktrees(gh, spec, scheduler=scheduler)
    except Exception:
        log.exception(
            "repo=%s pre-tick base refresh failed; continuing", spec.slug,
        )
    # Per-tick: label any open PR from an outsider author and ping HITL once.
    # Independent from the per-issue dispatch (PRs not driven by the
    # orchestrator have no pinned state to consult), so failures inside the
    # sweep are swallowed by the helper itself and cannot stop the tick.
    _sweep_community_contribution_prs(gh, spec)
    # Per-tick: snapshot the target repo's skill catalog into analytics.
    # Runs after the base refresh above has fetched
    # `<remote_name>/<base_branch>` so the ls-tree reads the current base
    # ref. Producer-side observability only and internally fail-open, so a
    # missing clone / git error never stops the tick; placed before the
    # scheduler/legacy split so it fires once per tick on both paths.
    _catalog._emit_repo_skill_catalog(spec)
    if scheduler is not None:
        _dispatch._dispatch_via_scheduler(gh, spec, scheduler)
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
        _run_sequential_tick(gh, spec, semaphore_cm)
    else:
        _run_parallel_tick(gh, spec, limit, semaphore_cm)
