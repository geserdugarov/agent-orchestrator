# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a split still owes a remote, and the one boundary that can settle it.

A split leaves two things behind: the branch its superseded candidate was
committed on, and the immutable ref that candidate was preserved under. Neither
is a precondition for the children -- children held back until a remote delete
succeeded would be work stalled on housekeeping -- so the transaction records
both and lets the children run whatever the first attempt said.

What that costs is an obligation nobody would otherwise come back to, because
the issue is an umbrella by then and an umbrella polls its children and nothing
else. So this owner is asked at the ONE boundary where an unsettled obligation
still matters and where the condition to settle it has just become true: the
umbrella's all-children-resolved branch.

**The branch is unconditional.** It is superseded the moment the split lands,
so every visit retries whatever is not yet reconciled -- and "the branch" is
every surface it exists on: the remote ref, the local ref, and the checkout
holding it. A remote delete that succeeded beside a worktree that would not
come down is not a settled obligation, because what is left behind is a
checkout the per-tick base refresh goes on merging into. So the entry reads
`reconciled` only once all three are provably gone, and the proof is a read
rather than an exit code: `git worktree remove` and `git branch -D` are
best-effort by design, and a caller that has to RECORD the teardown asks
afterwards instead of trusting them.

**Only a branch this generation owns is deleted.** The target comes off a
ledger a human can edit, and the entry is spent on a destructive call, so it
is checked against the namespace and the issue it must belong to before the
remote is touched at all. A target that is not one is recorded `failed` and
holds the terminal open for a human, which is the one answer that neither
deletes somebody's branch nor quietly forgets the obligation.

**The snapshot is not.** A ref may be deleted only once every recorded direct
consumer is terminal, and all-children-resolved is exactly when that becomes
true for the consumers this split created. The dispositions are read off the
scan the umbrella already took, so proving it costs no request of its own, and
anything that cannot be proved -- a consumer missing from the scan, one wearing
a label this binary does not know, a consumer ledger it could not type -- keeps
the ref rather than deleting an artifact somebody may still be cutting from.

**Nothing that cannot be proved settled lets a terminal fire.** An obligation
ledger this binary could not fully type blocks outright: the entries it could
not read are still obligations, and reclaiming around them would close an
umbrella over whatever they name. So does a ledger holding anything at all on
a record whose cycle identity is damaged -- there is nothing to correlate a
reclamation to, and no issue number to prove a branch belongs to this
generation, so the only safe answer is to say so loudly and stay open.

**`retained` never blocks the terminal; `failed` always does.** The asymmetry
is the safety argument. A ref kept because a consumer could not be proved
terminal is one a later sweep settles, and blocking on it would hold the
umbrella open for a condition nothing here can clear. A ref the remote REFUSED
to delete is a permission or ruleset problem an operator has to see, and the
parent staying open is how they see it.

Idempotent by construction, for the reason the transport underneath is: both
deletes treat an absent target as success, so a retry after a crash between the
call and the write that recorded it costs one request and reports the same
answer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType

from github.Issue import Issue

from orchestrator import config
from orchestrator.git.snapshots import refs as _snapshot_refs
from orchestrator.git.worktrees import cleanup as _worktree_cleanup
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github.client import GitHubClient
from orchestrator.github.issues import issue_is_closed
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import events as _events
from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.late_split import telemetry as _telemetry
from orchestrator.workflow.late_split.models import (
    LateFailure,
    LateGeneration,
    LateResource,
    LateResourceKind,
    LateResourceState,
)
from orchestrator.workflow.stages.decomposition import state as _state
from orchestrator.workflow.stages.decomposition.models import _ChildScan

log = logging.getLogger("orchestrator.workflow")

_UMBRELLA_STAGE = "umbrella"

_BRANCH = LateResourceKind.BRANCH

_SNAPSHOT = LateResourceKind.SNAPSHOT_REF

# What "the remote still has it" means for a branch: anything not confirmed
# gone.
_UNSETTLED = frozenset((
    LateResourceState.PENDING, LateResourceState.FAILED,
))

# The typed failure each refused reclamation is reported under.
_FAILURES = MappingProxyType({
    _BRANCH: LateFailure.BRANCH_CLEANUP_FAILED,
    _SNAPSHOT: LateFailure.SNAPSHOT_DELETE_FAILED,
})

# The dispositions that prove a direct consumer will never cut from the
# snapshot again. `done` covers a nested split too: a child that reached it has
# published, so its own descendants are past needing the ancestor.
_TERMINAL_CHILD = frozenset((_state._DONE, "rejected"))

# The namespace every branch this orchestrator publishes is inside, and the
# tail one belonging to a given issue ends with. Together they are what a
# recorded target has to satisfy before anything is deleted by it.
_OWNED_PREFIX = "orchestrator/"

_OWNED_TAIL = "/issue-{issue}"

# What a terminal is blocked by when the ledger itself is the thing that
# cannot be read. It names no resource because there is no resource to name --
# only the fact that what is owed is unknown.
_OPAQUE = "an obligation this orchestrator cannot read"

# The two transport answers that mean the ref is gone: one this call deleted,
# and one an earlier call already had.
_RECLAIMED = frozenset((
    _snapshot_refs.SnapshotOutcome.DELETED,
    _snapshot_refs.SnapshotOutcome.ABSENT,
))


@dataclass(frozen=True)
class _Reclamation:
    """What one pass over this issue's obligations settled, and what it did not.

    The two lists are what the sinks are told, and they are kept apart from
    the record because an entry that was ALREADY reconciled is not news: a
    tick that reports it again would put one `late_cleanup` per tick on an
    umbrella that is simply waiting for a sibling.
    """

    generation: LateGeneration
    reclaimed: tuple[tuple[LateResourceKind, str], ...] = ()
    refused: tuple[tuple[LateResourceKind, str], ...] = ()

    @property
    def attempted(self) -> bool:
        """Whether anything was asked of the remote at all."""
        return bool(self.reclaimed or self.refused)


def _owed_branches(generation: LateGeneration) -> tuple[str, ...]:
    """The superseded branches this generation has not seen reclaimed."""
    return tuple(
        entry.target
        for entry in generation.resources
        if entry.kind == _BRANCH and entry.resource_state in _UNSETTLED
    )


def _held_snapshots(generation: LateGeneration) -> tuple[str, ...]:
    """The snapshot refs this generation still holds the remote to."""
    return tuple(
        entry.target
        for entry in generation.resources
        if entry.kind == _SNAPSHOT
        and entry.resource_state != LateResourceState.RECONCILED
    )


def _reclaimable(generation: LateGeneration, scan: _ChildScan) -> bool:
    """Whether every direct consumer this snapshot records is terminal.

    Fail-closed: a consumer the scan does not carry, one wearing a label this
    binary does not recognize, or a consumer ledger it could not type is a
    consumer that may still be cutting from the ref, and deleting on the
    strength of a reading nobody gave would destroy the only copy of work a
    child was told to reuse.
    """
    if generation.has_opaque_ledger or not generation.consumers:
        return False
    return all(_is_terminal(scan, consumer) for consumer in generation.consumers)


def _is_terminal(scan: _ChildScan, consumer: int) -> bool:
    """Whether one recorded consumer has ended, however it ended.

    A terminal label is one way; a human closing the issue is the other, and
    it leaves whatever label the child was wearing untouched. The close is
    asked through the shared predicate rather than by reading an attribute
    here, because the only spelling a real issue carries it under is `state`
    -- and a consumer read as still running is one whose snapshot is never
    reclaimed.
    """
    number = int(consumer)
    if scan.labels.get(number) in _TERMINAL_CHILD:
        return True
    return issue_is_closed(scan.issues.get(number))


def _ours(generation: LateGeneration, branch: str) -> bool:
    """Whether a recorded target is a branch THIS generation could own.

    Asked before anything is deleted by it, because the target comes off a
    ledger a human can edit and the call it is spent on is destructive: an
    entry naming `main` would otherwise delete an unprotected `main`. Two
    conditions, and both are needed. The namespace is what every branch this
    orchestrator publishes is inside, so nothing outside it was ever ours; the
    issue tail is what keeps one generation from reclaiming another issue's
    branch, which is the same target in the same namespace.
    """
    if not isinstance(branch, str) or not branch.startswith(_OWNED_PREFIX):
        return False
    return branch.endswith(_OWNED_TAIL.format(issue=generation.current_issue))


def _reclaim_branch(
    gh: GitHubClient,
    spec: config.RepoSpec,
    generation: LateGeneration,
    branch: str,
) -> LateGeneration:
    """Take down every surface this branch exists on, and record the answer.

    Three surfaces, one obligation: the remote ref, the checkout holding the
    branch, and the local ref itself. A remote delete that succeeded beside a
    worktree that would not come down is not settled -- what is left is a
    checkout on a superseded branch that the per-tick base refresh treats as a
    pre-PR tree and goes on merging into.

    The local half is verified rather than trusted. Its two helpers are
    best-effort by design and report nothing, so what decides the entry is a
    read taken afterwards, and that read fails closed.

    Recorded whichever way it went: a `failed` obligation is still an
    obligation, and writing it is what keeps the retry pointed at the same
    branch rather than at whatever the resolver would name later.
    """
    if not _ours(generation, branch):
        log.error(
            "issue=#%d recorded branch %r is not one this generation owns; "
            "refusing to delete it", generation.current_issue, branch,
        )
        return _recorded(generation, _BRANCH, branch, deleted=False)
    try:
        deleted = gh.delete_remote_branch(branch)
    except Exception:
        log.exception("superseded branch %r delete raised", branch)
        deleted = False
    return _recorded(
        generation,
        _BRANCH,
        branch,
        deleted=deleted and _local_gone(spec, generation, branch),
    )


def _local_gone(
    spec: config.RepoSpec, generation: LateGeneration, branch: str,
) -> bool:
    """Take the local checkout and ref down, and say whether they are gone."""
    issue_number = generation.current_issue
    _worktree_cleanup._remove_issue_worktree(spec, issue_number)
    _worktree_cleanup._delete_local_issue_branch(spec, issue_number, branch)
    if _worktree_paths._worktree_path(spec, issue_number).exists():
        log.warning(
            "issue=#%d worktree is still on disk after the teardown; the "
            "branch obligation stays owed", issue_number,
        )
        return False
    if _worktree_cleanup._local_branch_present(spec, branch):
        log.warning(
            "issue=#%d local branch %r survived the teardown; the branch "
            "obligation stays owed", issue_number, branch,
        )
        return False
    return True


def _reclaim_snapshot(
    spec: config.RepoSpec, generation: LateGeneration, ref: str,
) -> LateGeneration:
    """Delete one snapshot ref and record whether the remote let it go.

    Named against the commit this generation preserved, so a ref somebody
    re-pointed is refused rather than reclaimed. An absent ref is a success,
    which is what makes the retry after a crash between the delete and this
    write cost one request rather than a mismatch.
    """
    outcome = _snapshot_refs.delete_snapshot_ref(
        spec, spec.target_root, ref=ref, sha=generation.candidate_sha,
    )
    return _recorded(
        generation, _SNAPSHOT, ref, deleted=outcome in _RECLAIMED,
    )


def _recorded(
    generation: LateGeneration,
    kind: LateResourceKind,
    target: str,
    *,
    deleted: bool,
) -> LateGeneration:
    """Move one obligation to what the remote just said about it."""
    settled = (
        LateResourceState.RECONCILED if deleted else LateResourceState.FAILED
    )
    try:
        return generation.with_resource(LateResource(
            kind=kind, target=target, resource_state=settled,
        ))
    except _formats.InvalidLateValue:
        log.exception("could not record the %s obligation %r", kind, target)
        return generation


def _record_branch_obligation(
    generation: LateGeneration, branch: str,
) -> LateGeneration:
    """Return this generation owing the remote one superseded branch.

    Written by the transaction before it attempts the delete, so a crash in
    between leaves the obligation for the umbrella above to retry rather than
    a branch nothing on the issue names.
    """
    return generation.with_resource(LateResource(
        kind=_BRANCH, target=branch, resource_state=LateResourceState.PENDING,
    ))


def _asked_of(
    generation: LateGeneration, scan: _ChildScan,
) -> tuple[tuple[LateResourceKind, str], ...]:
    """What this pass will ask the remote about, in the order it asks.

    A branch is asked about whenever it is owed; a snapshot only once every
    recorded direct consumer is terminal, which is the rule that owns it.

    Nothing at all while the ledger is opaque. The typed view is a projection
    of the entries this binary could read, and the write puts the verbatim
    copy back -- so a reclamation recorded against that view would be dropped
    at the next write and asked for again forever.
    """
    if generation.has_opaque_ledger:
        return ()
    owed = tuple((_BRANCH, target) for target in _owed_branches(generation))
    if not _reclaimable(generation, scan):
        return owed
    return owed + tuple(
        (_SNAPSHOT, target) for target in _held_snapshots(generation)
    )


def _reclaimed(
    gh: GitHubClient,
    spec: config.RepoSpec,
    generation: LateGeneration,
    scan: _ChildScan,
) -> _Reclamation:
    """Settle everything this issue owes that can be settled right now."""
    asked = _asked_of(generation, scan)
    settled = generation
    for kind, target in asked:
        if kind == _BRANCH:
            settled = _reclaim_branch(gh, spec, settled, target)
        else:
            settled = _reclaim_snapshot(spec, settled, target)
    outstanding = set(_owed_branches(settled)) | set(_held_snapshots(settled))
    return _Reclamation(
        generation=settled,
        reclaimed=tuple(
            owed for owed in asked if owed[1] not in outstanding
        ),
        refused=tuple(owed for owed in asked if owed[1] in outstanding),
    )


def _blocking(generation: LateGeneration) -> tuple[str, ...]:
    """What may not be left behind when this umbrella closes.

    A branch that is not reconciled, and a snapshot the remote REFUSED. A
    snapshot merely retained is not here: it is kept because a consumer could
    not be proved terminal, which is a condition a later sweep clears and this
    tick cannot.

    An opaque ledger blocks whatever the typed view says, and it has to: the
    entries this binary could not read are still obligations, and the typed
    entries beside them are not the whole of what is owed. Closing on the
    strength of a projection is exactly the reading the verbatim copy exists
    to prevent.
    """
    if generation.has_opaque_ledger:
        return (_OPAQUE,)
    refused = tuple(
        entry.target
        for entry in generation.resources
        if entry.kind == _SNAPSHOT
        and entry.resource_state == LateResourceState.FAILED
    )
    return _owed_branches(generation) + refused


def _settled_for_terminal(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    scan: _ChildScan,
) -> bool:
    """Whether this umbrella may complete, settling what it still owes.

    The one caller is the umbrella's all-children-resolved branch, and the
    answer is a decision rather than a report: False keeps the parent open on
    `workflow:umbrella` for the next tick to ask again, which is what makes an
    unreclaimed remote loud instead of silent. An issue with no recorded
    generation owes nothing and answers without a write.
    """
    generation = _late_state.read_late_generation(state)
    if not generation.is_present:
        return _owes_nothing_uncorrelated(issue, generation)
    settled = _reclaimed(gh, spec, generation, scan)
    if settled.attempted:
        _late_state.write_late_generation(state, settled.generation)
        gh.write_pinned_state(issue, state)
        _report(gh, issue, settled)
    return not _blocking(settled.generation)


def _owes_nothing_uncorrelated(
    issue: Issue, generation: LateGeneration,
) -> bool:
    """Whether an issue with no cycle identity may still close.

    An issue that never entered the late gate carries no ledger either, and
    answers True without a write -- which is every umbrella the initial
    decomposer made.

    A ledger with entries on a record whose identity is damaged is the other
    case, and it may not close. There is nothing to correlate a reclamation
    to, no issue number to prove a branch belongs to this generation, and no
    record either sink would accept -- so the only safe answer is to stay open
    and say so where an operator reads it. The write that damaged the identity
    kept the ledger on purpose; closing over it would finish the job.
    """
    if not generation.resources and not generation.has_opaque_ledger:
        return True
    log.error(
        "issue=#%d still records external obligations under a damaged late "
        "identity; holding the umbrella open rather than closing over them",
        issue.number,
    )
    return False


def _report(
    gh: GitHubClient, issue: Issue, settled: _Reclamation,
) -> None:
    """Say on both sinks what each attempted reclamation did."""
    for kind, target in settled.reclaimed:
        _emit_cleanup(gh, settled.generation, kind, target, deleted=True)
    for kind, target in settled.refused:
        _emit_cleanup(gh, settled.generation, kind, target, deleted=False)
        log.warning(
            "issue=#%d still owes the remote %s %r; holding the umbrella "
            "open until it is reclaimed", issue.number, kind, target,
        )


def _emit_cleanup(
    gh: GitHubClient,
    generation: LateGeneration,
    kind: LateResourceKind,
    target: str,
    *,
    deleted: bool,
) -> None:
    """Report what happened to one external resource, on both sinks."""
    if not deleted:
        _telemetry.emit_late_event(
            gh,
            _events.LateEvent(
                family=_events.LateEventFamily.FAILURE,
                failure=_FAILURES[kind],
            ),
            generation,
            stage=_UMBRELLA_STAGE,
        )
    _telemetry.emit_late_event(
        gh,
        _events.LateEvent(
            family=_events.LateEventFamily.CLEANUP,
            resource=LateResource(
                kind=kind,
                target=target,
                resource_state=(
                    LateResourceState.RECONCILED if deleted
                    else LateResourceState.FAILED
                ),
            ),
        ),
        generation,
        stage=_UMBRELLA_STAGE,
    )
