# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow scheduler."""
from __future__ import annotations

from orchestrator import _workflow_state as _state
from orchestrator import workflow as _owner

_PollablePartition = _owner._PollablePartition
GitHubClient = _owner.GitHubClient
IssueScheduler = _owner.IssueScheduler
config = _owner.config
functools = _owner.functools
_FAMILY_BUCKET_ISSUE = _state._FAMILY_BUCKET_ISSUE
_ISSUE_STATE_CLOSED = _state._ISSUE_STATE_CLOSED
_ISSUE_STATE_OPEN = _state._ISSUE_STATE_OPEN
_PROCESSING_FAILED_LOG = _state._PROCESSING_FAILED_LOG
_STATE_ATTR = _state._STATE_ATTR
log = _state.log


def _issue_is_closed(issue) -> bool:
    """True when the issue is closed.

    Tolerant of both shapes the dispatcher sees: PyGithub's ``Issue.state``
    (``"open"`` / ``"closed"``) and the in-memory fake's ``closed`` bool.
    """
    return bool(getattr(issue, "closed", False)) or (
        getattr(issue, _STATE_ATTR, _ISSUE_STATE_OPEN) == _ISSUE_STATE_CLOSED
    )


def _drain_scheduler_family_bucket(
    gh: GitHubClient,
    spec: config.RepoSpec,
    scheduler: IssueScheduler,
    family_numbers: list[int],
) -> None:
    """Drain this tick's family-aware issues sequentially under one bucket.

    Runs as the single ``family=True`` scheduler submit per repo, so the
    family slot is held for the whole drain: a concurrent tick mid-drain
    cannot squeeze a second family worker past the gate and no two
    family-aware handlers ever run at once. ``scheduler.track_active`` wraps
    each iteration so ``is_active(repo, n)`` reports True for the issue
    currently being processed inside the bucket -- the pre-tick base refresh
    relies on that signal to avoid rebasing a worktree under a running agent;
    without the per-iteration claim only the bucket's sentinel key would
    appear in the in-flight set and a concurrent refresh would race the agent.

    ``track_active`` yields a ``claimed`` bool: when False the issue is
    already in flight on another worker (e.g. a fanout submit accepted on a
    previous tick before this issue was relabeled into the family bucket), so
    the drain skips ``_process_issue`` for that iteration and the next polling
    pass picks it up once the other worker exits -- two workers running the
    same handler concurrently would race the worktree and pinned state.
    Per-issue exception isolation lives inside the loop so one raising family
    handler does not abort the rest of the bucket.

    Each per-issue call mirrors the fanout path: ``_refetch_and_process``
    mints a fresh ``GitHubClient`` via ``gh._for_worker_thread()`` and
    refetches the Issue against it (PyGithub is not documented thread-safe).
    """
    for issue_number in family_numbers:
        try:
            with scheduler.track_active(spec.slug, issue_number) as claimed:
                if not claimed:
                    log.info(
                        "repo=%s issue=#%s already in flight; "
                        "family bucket skipping this iteration",
                        spec.slug, issue_number,
                    )
                    continue
                _owner._refetch_and_process(gh, spec, issue_number)
        except Exception:
            log.exception(
                _PROCESSING_FAILED_LOG,
                spec.slug, issue_number,
            )


def _scheduler_per_repo_cap(spec: config.RepoSpec) -> int:
    return max(1, int(getattr(spec, "parallel_limit", 1) or 1))


def _submit_scheduler_family_bucket(
    gh: GitHubClient,
    spec: config.RepoSpec,
    scheduler: IssueScheduler,
    partition: _PollablePartition,
    per_repo_cap: int,
) -> None:
    family_numbers = partition.family_numbers
    if not family_numbers:
        return

    submitted = scheduler.submit(
        spec.slug,
        _FAMILY_BUCKET_ISSUE,
        functools.partial(
            _owner._drain_scheduler_family_bucket, gh, spec, scheduler, family_numbers,
        ),
        family=True,
        cap_exempt=_owner._family_bucket_cap_exempt(partition.family_labels),
        per_repo_cap=per_repo_cap,
    )
    if submitted:
        return

    # The scheduler logs the precise skip reason (closed, family_slot_held,
    # cap, ...) inside `submit`; this line gives the dispatch-layer context
    # -- which issues were waiting on this bucket -- so an operator can
    # correlate "umbrella not advancing" with a previous tick's bucket
    # still in flight.
    log.info(
        "repo=%s family bucket (%d issues) not submitted this "
        "tick; next polling pass retries",
        spec.slug, len(family_numbers),
    )


def _submit_scheduler_fanout_issues(
    gh: GitHubClient,
    spec: config.RepoSpec,
    scheduler: IssueScheduler,
    partition: _PollablePartition,
    per_repo_cap: int,
) -> None:
    for issue_number in partition.fanout_numbers:
        scheduler.submit(
            spec.slug,
            issue_number,
            functools.partial(_owner._refetch_and_process, gh, spec, issue_number),
            family=False,
            # A closed issue's handler is a cheap terminal finalization with
            # no agent spawn -- exempt it from the per-repo / global caps so
            # a merged-PR or closed-question issue flips to `done` promptly
            # instead of being starved behind active agent work under
            # `parallel_limit=1` (mirrors the `_CAP_EXEMPT_FAMILY_LABELS`
            # exemption for `blocked` / `umbrella`).
            cap_exempt=(issue_number in partition.fanout_closed),
            per_repo_cap=per_repo_cap,
        )


def _dispatch_via_scheduler(
    gh: GitHubClient, spec: config.RepoSpec, scheduler: IssueScheduler,
) -> None:
    """Enumerate pollable issues this tick and hand work to the scheduler.

    Family-aware work (unlabeled pickup + decomposing / blocked /
    umbrella -- the cross-issue writers) is folded into ONE bucket
    submit per repo that drains its issues sequentially on a single
    worker thread; non-family issues are submitted individually. This
    mirrors the legacy parallel-tick partition in ``tick()`` (one drain
    task for the family bucket, per-issue futures for fanout).

    Per-submitting family-aware issues with `family=True` (the prior
    behavior) lets the first accepted family submit hold the family
    slot and silently starve every subsequent family submit this tick.
    The starvation was the issue #326 bug: a stale backlog/blocked
    child took the slot and the parent umbrella that should have
    relabeled it never ran. Folding family work into one bucket means
    the umbrella always gets its turn within the same tick.

    The bucket task uses ``scheduler.track_active`` around each
    per-issue iteration so ``scheduler.is_active(repo, n)`` reports True
    for the issue currently being processed inside the bucket -- the
    pre-tick base refresh relies on that signal to avoid rebasing a
    worktree under a running agent. Without per-iteration tracking,
    only the bucket's sentinel key would appear in the in-flight set
    and a concurrent refresh would race the agent.

    Each per-issue callable mirrors the legacy parallel path: mint a
    fresh ``GitHubClient`` via ``gh._for_worker_thread()`` and refetch
    the Issue against that client so the worker drives its own
    Requester chain (PyGithub is not documented thread-safe).

    Completion reaping is the polling loop's job, not this function's.
    ``main._run_tick`` calls ``scheduler.reap()`` exactly once after
    every configured repo's tick returns, so the contract surfaced to
    operators and documented in ``docs/observability.md`` ("one reap
    per polling pass") holds in multi-repo mode too. An earlier draft
    reaped here as well; that produced N+1 reaps per polling pass
    under ``REPOS`` and contradicted the documented cadence.

    ``spec.parallel_limit`` is forwarded as the scheduler's per-call cap
    override so a per-repo configuration tighter than the scheduler
    default still binds. Label-read failures route the offending issue
    into the family bucket so ``_process_issue``'s own exception
    isolation picks up any sustained failure -- same recovery the
    legacy parallel path uses.

    When every family-aware issue this tick runs a no-agent handler
    (label in ``_CAP_EXEMPT_FAMILY_LABELS`` -- ``blocked`` or
    ``umbrella``, both pure label/dep-graph walks), the bucket submit is
    marked ``cap_exempt=True`` so it does not consume a
    ``MAX_PARALLEL_ISSUES_PER_REPO`` or ``MAX_PARALLEL_ISSUES_GLOBAL``
    slot. Such a bucket must always get its turn even when the caps are
    saturated by ordinary implementation work -- otherwise a ``blocked``
    parent polling its own children would be starved of the only
    per-repo slot (under the default ``parallel_limit=1``) and deadlock
    the very children it waits on. A bucket containing ``decomposing``
    (spawns the decomposer agent) or an unlabeled-pickup ``None`` stays
    cap-counted. ``backlog`` / ``paused`` issues are filtered out before
    this split -- a parked issue carries no workflow label, so leaving it in
    would fold it into the bucket and force ``cap_exempt=False``, starving
    fanout behind a hard-skip hold under ``parallel_limit=1``. The family mutex
    still applies, so a follow-up tick that finds another family issue
    still serializes against this bucket.

    Closed fan-out issues are likewise submitted ``cap_exempt=True``: a
    closed issue carrying a sweep label (``in_review`` / ``fixing`` /
    ``resolving_conflict`` / ``question`` / ...) only runs a terminal
    finalization (flip to ``done`` / ``rejected`` + branch cleanup) with no
    agent spawn, so it must not be starved behind active agent work -- a
    merged-PR issue could otherwise sit closed-but-labeled for many ticks
    while a sibling ``validating`` / ``documenting`` agent holds the only
    per-repo slot.
    """
    per_repo_cap = _owner._scheduler_per_repo_cap(spec)
    # `_partition_pollable_issues` owns the skip-label filtering, per-issue
    # label-read isolation, and the family/fanout split (including the closed
    # fan-out set). `backlog` / `paused` issues are dropped there so a parked,
    # workflow-label-less issue never folds into the bucket and flips it
    # cap-counted, which would reserve the only per-repo slot and starve
    # fanout under `parallel_limit=1`.
    partition = _owner._partition_pollable_issues(gh, spec)

    # One `family=True` submit per repo drains every family-aware issue
    # sequentially (see `_drain_scheduler_family_bucket`). The bucket is
    # cap-exempt only when every family issue runs a no-agent handler
    # (`_family_bucket_cap_exempt`); the helper keeps the exempt probe and
    # the submit off the no-family path entirely.
    _owner._submit_scheduler_family_bucket(gh, spec, scheduler, partition, per_repo_cap)
    _owner._submit_scheduler_fanout_issues(gh, spec, scheduler, partition, per_repo_cap)
