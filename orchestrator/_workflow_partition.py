# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow partition."""
from __future__ import annotations

from orchestrator import _workflow_state as _state
from orchestrator import workflow as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
config = _owner.config
contextlib = _owner.contextlib
dataclass = _owner.dataclass
field = _owner.field
hard_skip_control_label = _owner.hard_skip_control_label
_CAP_EXEMPT_FAMILY_LABELS = _state._CAP_EXEMPT_FAMILY_LABELS
_FAMILY_AWARE_LABELS = _state._FAMILY_AWARE_LABELS
log = _state.log


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
        return _owner._read_issue_routing(gh, spec, issue)
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
        skip, label = _owner._classify_pollable_issue(gh, spec, issue)
        if skip:
            continue
        builder.add(int(issue.number), label, _owner._issue_is_closed(issue))
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

    ``semaphore_cm`` wraps the ``_process_issue`` call so the legacy parallel
    path can thread the cross-repo ``global_semaphore`` through here; the
    scheduler path leaves it ``None`` (a no-op) because the scheduler owns
    the cross-repo cap itself.
    """
    worker_gh = gh._for_worker_thread()
    worker_issue = worker_gh.get_issue(issue_number)
    cm = contextlib.nullcontext() if semaphore_cm is None else semaphore_cm
    with cm:
        _owner._process_issue(worker_gh, spec, worker_issue)
