# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""How a tick's pollable issues become handler calls.

Everything between "the repo has open issues" and "one `_handle_<stage>` is
running" lives here, because the decisions are one chain and each link is only
safe given the one before it.

The chain starts with the hard-skip filter. `backlog` / `paused` park an issue
outside the state machine entirely, and the filter runs twice on purpose: once
in `_classify_pollable_issue` so a parked issue never reaches the partition,
and once in `_process_issue` so a directly dispatched one is still refused.
Dropping it early is not an optimization -- a parked issue carries no workflow
label, so leaving it in would fold it into the family bucket and flip that
bucket cap-counted, reserving the only per-repo slot under the default
`parallel_limit=1` and starving every fan-out issue behind a hold nobody is
working on.

The partition is the concurrency contract. Family-aware labels (`decomposing`
/ `blocked` / `umbrella`) and the unlabeled-pickup `None` are cross-issue
writers -- a parent's recovery seeds `parent_number` on a child whose own
handler would clobber the same pinned-state comment -- so they collect into one
bucket that drains sequentially, and everything else fans out. A label read
that raises is answered `(False, None)`, which routes that issue into the
family bucket rather than dropping it: the conservative side of an unreadable
label is the serialized one, where `_process_issue`'s per-issue exception
isolation picks up a sustained failure.

Cap exemption is what keeps the serialization from deadlocking. A bucket whose
every label is a no-agent, no-worktree handler (`_CAP_EXEMPT_FAMILY_LABELS`)
skips the per-repo and global caps, because a `blocked` parent polling its own
children would otherwise wait on the only slot those children need to finish.
Closed fan-out issues are exempt for the same reason at the other end: their
handler is a terminal finalize with no spawn, so it must not queue behind
active agent work.

Only issue NUMBERS cross the thread boundary. PyGithub's `Issue` and the
`GitHubClient` / `Repository` / `Requester` chain behind it hold mutable
per-request state that is not documented thread-safe, so `_refetch_and_process`
mints a per-worker client and refetches against it -- every in-flight call is
then the sole consumer of its own requester.

The handler for a label is reached by importing the module
`_STAGE_HANDLER_TARGETS` pairs it with, at call time: twelve of them are
conflicts, decomposition, discussion, documenting, fixing, implementing,
question, validating, and in_review owners under `workflow/stages/`, and the
thirteenth is the `pickup` sibling an unlabeled issue starts on -- and the stage
tree imports this subpackage, so binding any of them
at module scope would point that edge back at itself. Every entry names the
owner its handler lives on, so the patch that intercepts a dispatch is the one
against whichever module the table names.
"""
from __future__ import annotations

import contextlib
import functools
import importlib
import logging
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator.github.client import GitHubClient
from orchestrator.github.issues import issue_is_closed
from orchestrator.observability.analytics import recording
from orchestrator.github.labels import hard_skip_control_label
from orchestrator.scheduler import IssueScheduler
from orchestrator.workflow.state import WorkflowLabel, stage_name

log = logging.getLogger("orchestrator.workflow")

# Every isolated per-issue failure reports through one line so an operator
# grepping a tick's log sees the same shape whether the issue was dispatched
# sequentially, refetched on a worker, or drained from the family bucket.
_PROCESSING_FAILED_LOG = "repo=%s issue=#%s processing failed"

_FAMILY_AWARE_LABELS = frozenset((
    WorkflowLabel.DECOMPOSING, WorkflowLabel.BLOCKED, WorkflowLabel.UMBRELLA,
))

_CAP_EXEMPT_FAMILY_LABELS = frozenset((
    WorkflowLabel.BLOCKED, WorkflowLabel.UMBRELLA,
))

_FAMILY_BUCKET_ISSUE: int = 0

_CONFLICTS_PACKAGE = "orchestrator.workflow.stages.conflicts"
_DECOMPOSITION_PACKAGE = "orchestrator.workflow.stages.decomposition"
_LATE_RELABEL_OWNER = f"{_DECOMPOSITION_PACKAGE}.late_relabel"
_DISCUSSION_PACKAGE = "orchestrator.workflow.stages.discussion"
_DOCUMENTING_PACKAGE = "orchestrator.workflow.stages.documenting"
_FIXING_PACKAGE = "orchestrator.workflow.stages.fixing"
_IMPLEMENTING_PACKAGE = "orchestrator.workflow.stages.implementing"
_IN_REVIEW_PACKAGE = "orchestrator.workflow.stages.in_review"
_QUESTION_PACKAGE = "orchestrator.workflow.stages.question"
_VALIDATING_PACKAGE = "orchestrator.workflow.stages.validating"

_TERMINAL_LABELS = (WorkflowLabel.DONE, WorkflowLabel.REJECTED)

# Keyed by the member rather than the label string so the table cannot drift
# from the vocabulary it routes: a relabeled state is a lookup miss here, and a
# lookup miss is an issue nobody handles.
_STAGE_HANDLER_TARGETS: Mapping[Optional[str], tuple[str, str]] = MappingProxyType({
    None: ("orchestrator.workflow.engine.pickup", "_handle_pickup"),
    WorkflowLabel.DECOMPOSING: (f"{_DECOMPOSITION_PACKAGE}.run", "_handle_decomposing"),
    WorkflowLabel.READY: (f"{_DECOMPOSITION_PACKAGE}.blocked", "_handle_ready"),
    WorkflowLabel.BLOCKED: (f"{_DECOMPOSITION_PACKAGE}.blocked", "_handle_blocked"),
    WorkflowLabel.UMBRELLA: (f"{_DECOMPOSITION_PACKAGE}.umbrella", "_handle_umbrella"),
    WorkflowLabel.IMPLEMENTING: (f"{_IMPLEMENTING_PACKAGE}.handler", "_handle_implementing"),
    WorkflowLabel.DOCUMENTING: (f"{_DOCUMENTING_PACKAGE}.handler", "_handle_documenting"),
    WorkflowLabel.VALIDATING: (f"{_VALIDATING_PACKAGE}.handler", "_handle_validating"),
    WorkflowLabel.IN_REVIEW: (f"{_IN_REVIEW_PACKAGE}.handler", "_handle_in_review"),
    WorkflowLabel.FIXING: (f"{_FIXING_PACKAGE}.handler", "_handle_fixing"),
    WorkflowLabel.RESOLVING_CONFLICT: (
        f"{_CONFLICTS_PACKAGE}.handler", "_handle_resolving_conflict",
    ),
    WorkflowLabel.QUESTION: (f"{_QUESTION_PACKAGE}.handler", "_handle_question"),
    WorkflowLabel.DISCUSSION: (f"{_DISCUSSION_PACKAGE}.handler", "_handle_discussion"),
})


def _late_adjudication_holds_the_label(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, label: Optional[str],
) -> bool:
    """True when a live late adjudication forbids this label's handler.

    An oversized committed candidate is adjudicated under
    ``workflow:decomposing``, and while that question is open the label is not
    a state anything else may set. A hand relabel cannot be refused where it is
    written -- the orchestrator never sees that write -- so it is caught here,
    the one place a label becomes a handler call: the issue is put back and
    left for the next tick rather than dispatched to whichever stage the new
    label named, which for ``ready`` or ``implementing`` would publish a
    candidate nobody adjudicated.

    The pinned read this costs is skipped for the label the adjudication
    actually sits on, which is where every one of its own ticks is spent; the
    owner beside it explains what the rest is paid for. Imported at call time
    like the handlers below, since the stage tree imports this module.
    """
    if label == WorkflowLabel.DECOMPOSING:
        return False
    late_relabel = importlib.import_module(_LATE_RELABEL_OWNER)
    if not late_relabel._refuses_dispatch(gh, issue):
        return False
    log.warning(
        "repo=%s issue=#%s was relabelled %r while its committed candidate "
        "was under adjudication; not dispatching it",
        spec.slug, issue.number, label,
    )
    return True


def _route_issue_to_handler(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, label: Optional[str],
) -> None:
    """Dispatch one issue to its stage handler by workflow label.

    The module the table names is imported at call time and the handler read
    off it as an attribute, so the patch that intercepts a dispatch is the one
    against that module -- the stage's own handler owner.
    ``done`` / ``rejected`` are terminal no-ops; an unrecognized label is
    logged and left alone for a human. Timing and the ``stage_evaluation``
    analytics record stay in ``_process_issue``, which wraps this call in its
    try / except / finally.

    An issue whose label a human moved out from under a live late adjudication
    is put back instead of dispatched -- see the owner above.
    """
    if _late_adjudication_holds_the_label(gh, spec, issue, label):
        return
    target = _STAGE_HANDLER_TARGETS.get(label)
    if target is not None:
        module_name, handler_name = target
        issue_handler = getattr(importlib.import_module(module_name), handler_name)
        issue_handler(gh, spec, issue)
    elif label not in _TERMINAL_LABELS:
        log.warning(
            "repo=%s issue=#%s label=%r not implemented yet; leaving alone",
            spec.slug, issue.number, label,
        )


def _process_issue(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    # Postponed-task hold: applying `backlog` (or `paused`) parks the issue
    # outside the state machine entirely until the label is removed. Checked
    # before reading the workflow label so the orchestrator never decomposes,
    # spawns an agent, or otherwise reacts while the operator is using the
    # label as a "not yet" signal. Hard-skips are NOT counted as a stage
    # evaluation: no handler runs and there is nothing to time.
    skip_label = hard_skip_control_label(issue)
    if skip_label is not None:
        log.info(
            "repo=%s issue=#%s has %r; skipping",
            spec.slug, issue.number, skip_label,
        )
        return
    label = gh.workflow_label(issue)
    log.info("repo=%s issue=#%s label=%r", spec.slug, issue.number, label)
    # Time the handler dispatch and append a single `stage_evaluation`
    # analytics record on exit. `evaluation_result` flips to "error" inside the
    # except clause so an unhandled exception still produces a timing
    # record before propagating -- the tick loop's per-issue try/except
    # already logs and isolates the failure, so re-raising here keeps
    # the existing dispatch / exception contract intact. The append
    # itself is internally hardened against OSError; an analytics
    # misconfiguration cannot stop the per-issue tick from advancing.
    start = time.monotonic()
    evaluation_result = "ok"
    try:
        _route_issue_to_handler(gh, spec, issue, label)
    except Exception:
        evaluation_result = "error"
        raise
    finally:
        duration_s = round(time.monotonic() - start, 3)
        recording.record_stage_evaluation(
            repo=getattr(gh, "_repo_slug", None) or "",
            issue=issue.number,
            stage=stage_name(label),
            duration_s=duration_s,
            result=evaluation_result,
        )


@dataclass(frozen=True)
class _PollablePartition:
    """Family / fanout split of one repo's pollable issues for a single tick.

    ``family_numbers`` and ``family_labels`` are index-aligned so the
    cap-exempt decision (`_family_bucket_cap_exempt`) can read each
    family-aware issue's workflow label. ``fanout_closed`` is the subset of
    ``fanout_numbers`` whose issue is already closed -- a cheap terminal
    finalize the dispatcher submits cap-exempt.
    """
    family_numbers: list[int]
    family_labels: list[Optional[str]]
    fanout_numbers: list[int]
    fanout_closed: set[int]


@dataclass
class _PollablePartitionBuilder:
    family_numbers: list[int] = field(default_factory=list)
    family_labels: list[Optional[str]] = field(default_factory=list)
    fanout_numbers: list[int] = field(default_factory=list)
    fanout_closed: set[int] = field(default_factory=set)

    def add(self, issue_number: int, label: Optional[str], closed: bool) -> None:
        if label is None or label in _FAMILY_AWARE_LABELS:
            self.family_numbers.append(issue_number)
            self.family_labels.append(label)
        else:
            self.fanout_numbers.append(issue_number)
            if closed:
                self.fanout_closed.add(issue_number)

    def build(self) -> _PollablePartition:
        return _PollablePartition(
            self.family_numbers,
            self.family_labels,
            self.fanout_numbers,
            self.fanout_closed,
        )


def _read_issue_routing(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue,
) -> tuple[bool, Optional[str]]:
    """Return ``(skip, label)`` from the issue's control / workflow labels."""
    skip_label = hard_skip_control_label(issue)
    if skip_label is not None:
        log.info(
            "repo=%s issue=#%s has %r; skipping",
            spec.slug, issue.number, skip_label,
        )
        return True, None
    return False, gh.workflow_label(issue)


def _classify_pollable_issue(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue,
) -> tuple[bool, Optional[str]]:
    """Read one pollable issue's workflow label for the family / fanout split.

    Returns ``(skip, label)``. ``skip=True`` marks a hard-skip control label
    (``backlog`` / ``paused``): the operator parked the issue outside the
    state machine, so the caller drops it BEFORE the partition -- a parked,
    workflow-label-less issue folded into the family bucket would flip the
    whole bucket cap-counted and starve fanout under ``parallel_limit=1``
    (``_process_issue`` skips it anyway).

    A label-read failure (including one raised by ``hard_skip_control_label``
    itself) is reported as ``(False, None)`` so the issue is conservatively
    routed into the family bucket, where ``_process_issue``'s own per-issue
    exception isolation picks up any sustained failure. The label read runs
    on the caller thread so bucketing needs no extra worker-side round-trip.
    """
    try:
        return _read_issue_routing(gh, spec, issue)
    except Exception:
        log.exception(
            "repo=%s issue=#%s label read failed; routing to family bucket "
            "so per-issue exception isolation can pick up any sustained "
            "failure", spec.slug, issue.number,
        )
        return False, None


def _partition_pollable_issues(
    gh: GitHubClient, spec: config.RepoSpec,
) -> _PollablePartition:
    """Split this tick's pollable issues into the family and fanout buckets.

    Family-aware labels (``decomposing`` / ``blocked`` / ``umbrella``) and
    the unlabeled-pickup ``None`` are cross-issue writers -- a parent's
    ``_handle_decomposing`` recovery seeds ``parent_number`` on a child
    while the child's ``_handle_blocked`` would otherwise clobber the same
    pinned-state comment -- so they must never run two at a time and are
    collected into ``family_numbers`` (with index-aligned ``family_labels``).
    Every other label touches only its own per-issue state and fans out; a
    closed fanout issue is additionally recorded in ``fanout_closed`` because
    its handler is a cheap terminal finalize submitted cap-exempt. Hard-skip
    (``backlog`` / ``paused``) issues are dropped entirely.
    """
    builder = _PollablePartitionBuilder()
    for issue in gh.list_pollable_issues():
        skip, label = _classify_pollable_issue(gh, spec, issue)
        if skip:
            continue
        builder.add(int(issue.number), label, issue_is_closed(issue))
    return builder.build()


def _family_bucket_cap_exempt(family_labels: list[Optional[str]]) -> bool:
    """True when a family bucket may skip the per-repo / global caps.

    A bucket is cap-exempt only when EVERY issue in it this tick runs a
    no-agent / no-worktree handler -- all labels in ``_CAP_EXEMPT_FAMILY_LABELS``
    (``blocked`` / ``umbrella``, pure dep-graph walks). Such a bucket must
    always get its turn even when the parallel caps are saturated by real
    implementation work: a ``blocked`` parent polling its children, or an
    ``umbrella`` aggregating them, would otherwise be starved of the only
    per-repo slot under the default ``parallel_limit=1`` -- and a ``blocked``
    parent waiting on its own children would deadlock them. A bucket
    containing ``decomposing`` (spawns the decomposer agent) or an
    unlabeled-pickup ``None`` (routes through ``_handle_pickup``, may spawn an
    agent) stays cap-counted.
    """
    return all(lbl in _CAP_EXEMPT_FAMILY_LABELS for lbl in family_labels)


def _refetch_and_process(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue_number: int,
    *,
    semaphore_cm: Optional[contextlib.AbstractContextManager] = None,
) -> None:
    """Mint a per-worker client, refetch the Issue, and run its handler.

    Only issue NUMBERS cross the thread boundary. PyGithub's ``Issue`` and
    the parent ``GitHubClient`` / ``Repository`` / ``Requester`` chain hold
    mutable per-request state that is not documented thread-safe, so each
    worker calls ``gh._for_worker_thread()`` to mint a fresh client and
    refetches its Issue against THAT client -- every in-flight HTTP call is
    then the sole consumer of its requester's state.

    ``semaphore_cm`` wraps the ``_process_issue`` call so the in-tick parallel
    path can thread the cross-repo ``global_semaphore`` through here; the
    scheduler path leaves it ``None`` (a no-op) because the scheduler owns
    the cross-repo cap itself.
    """
    worker_gh = gh._for_worker_thread()
    worker_issue = worker_gh.get_issue(issue_number)
    cm = contextlib.nullcontext() if semaphore_cm is None else semaphore_cm
    with cm:
        _process_issue(worker_gh, spec, worker_issue)


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
                _refetch_and_process(gh, spec, issue_number)
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
            _drain_scheduler_family_bucket, gh, spec, scheduler, family_numbers,
        ),
        family=True,
        cap_exempt=_family_bucket_cap_exempt(partition.family_labels),
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
            functools.partial(_refetch_and_process, gh, spec, issue_number),
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
    worker thread; non-family issues are submitted individually. The
    in-tick parallel path in ``tick()`` partitions the same way (one
    drain task for the family bucket, per-issue futures for fanout).

    One bucket per repo is what keeps the family mutex from starving
    itself. The scheduler grants the family slot to the first accepted
    ``family=True`` submit and silently skips every later one this tick,
    so a per-issue family submit would let a stale ``blocked`` child
    take the slot while the parent ``umbrella`` that should relabel it
    never runs -- and the pair would trade the slot back and forth
    forever. Draining every family issue inside the one accepted submit
    means the umbrella always gets its turn within the same tick.

    The bucket task uses ``scheduler.track_active`` around each
    per-issue iteration so ``scheduler.is_active(repo, n)`` reports True
    for the issue currently being processed inside the bucket -- the
    pre-tick base refresh relies on that signal to avoid rebasing a
    worktree under a running agent. Without per-iteration tracking,
    only the bucket's sentinel key would appear in the in-flight set
    and a concurrent refresh would race the agent.

    Each per-issue callable mirrors the in-tick parallel path: mint a
    fresh ``GitHubClient`` via ``gh._for_worker_thread()`` and refetch
    the Issue against that client so the worker drives its own
    Requester chain (PyGithub is not documented thread-safe).

    Completion reaping is the polling loop's job, not this function's.
    ``runtime.ticks.run_tick`` calls ``scheduler.reap()`` exactly once
    after every configured repo's tick returns, which is the cadence surfaced
    to operators and documented in ``docs/observability.md`` ("one reap
    per polling pass"). Reaping here as well would multiply that to N+1
    reaps per pass under ``REPOS``.

    ``spec.parallel_limit`` is forwarded as the scheduler's per-call cap
    override so a per-repo configuration tighter than the scheduler
    default still binds. Label-read failures route the offending issue
    into the family bucket so ``_process_issue``'s own exception
    isolation picks up any sustained failure -- the same recovery the
    in-tick parallel path uses.

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
    per_repo_cap = _scheduler_per_repo_cap(spec)
    # `_partition_pollable_issues` owns the skip-label filtering, per-issue
    # label-read isolation, and the family/fanout split (including the closed
    # fan-out set). `backlog` / `paused` issues are dropped there so a parked,
    # workflow-label-less issue never folds into the bucket and flips it
    # cap-counted, which would reserve the only per-repo slot and starve
    # fanout under `parallel_limit=1`.
    partition = _partition_pollable_issues(gh, spec)

    # One `family=True` submit per repo drains every family-aware issue
    # sequentially (see `_drain_scheduler_family_bucket`). The bucket is
    # cap-exempt only when every family issue runs a no-agent handler
    # (`_family_bucket_cap_exempt`); the helper keeps the exempt probe and
    # the submit off the no-family path entirely.
    _submit_scheduler_family_bucket(gh, spec, scheduler, partition, per_repo_cap)
    _submit_scheduler_fanout_issues(gh, spec, scheduler, partition, per_repo_cap)
