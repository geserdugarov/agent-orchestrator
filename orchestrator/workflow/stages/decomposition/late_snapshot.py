# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The immutable copy every child of a split is cut from, taken before any.

Snapshot-first is the whole ordering rule of the split transaction, and it is
not a preference. A split ends with the parent's branch superseded, its pull
request closed, and the parent itself an umbrella that implements nothing --
so once children exist, the only thing standing between them and the work they
were told to reuse is this ref. A child created ahead of it would be a child
pointed at a branch a merge, a force-push, or an auto-delete may already have
taken away.

Three steps, and each is durable before the one after it:

**The intent goes down first.** The ref this generation will write is derived
from its own identity and recorded on the obligation ledger BEFORE anything is
pushed, because a push that landed and a process that died a statement later
are indistinguishable from the outside. What the ledger holds is what a
reclamation walks, and an obligation nobody recorded is a ref nothing on the
issue can ever name again.

**The create is create-or-verify.** The transport asks the remote first, so a
retry finds the ref it already pushed and spends a read instead of a write.
There is no overwrite: a ref already carrying another commit is reported as a
mismatch and the transaction parks, because the automatic alternative is
destroying the only copy of somebody else's candidate.

**Proof is a fetch, not a read.** Every child is about to be told it can obtain
this candidate, so the ref is fetched back into the clone the worktrees share
and resolved there. A namespace a token may write and not read would otherwise
pass every check here and fail the first child that tried to use it.

All of it runs against `target_root` rather than the candidate's worktree.
The object being preserved is in the store the linked worktrees share, so the
push has it either way -- and the clone is the operator-owned one, which is
where the verifying fetch has to land anyway and is there whether or not this
host still has the developer's checkout.

Every failure is the same shape: the ledger entry is written `failed`, a typed
`snapshot_failed` is emitted, and the issue parks. Nothing is created,
superseded, or activated on the strength of a snapshot nobody could prove, and
the retry costs no agent -- the verdict is already recorded, so the next
eligible tick reconciles this same step against the same frozen commit.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from types import MappingProxyType
from typing import Optional

from orchestrator.git.snapshots import namespace as _namespace
from orchestrator.git.snapshots import refs as _snapshot_refs
from orchestrator.workflow.late_split import events as _events
from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import telemetry as _telemetry
from orchestrator.workflow.late_split.models import (
    LateFailure,
    LatePhase,
    LateResource,
    LateResourceKind,
    LateResourceState,
)
from orchestrator.workflow.stages.decomposition import (
    late_outcome as _late_outcome,
)
from orchestrator.workflow.stages.decomposition.late_models import _LateContext

log = logging.getLogger("orchestrator.workflow")

_DECOMPOSING_STAGE = "decomposing"

# The two answers that mean the candidate is preserved. They are kept apart by
# the transport so an operator can tell a write from a read, and folded here
# because what the transaction does next is identical.
_ESTABLISHED = frozenset((
    _snapshot_refs.SnapshotOutcome.CREATED,
    _snapshot_refs.SnapshotOutcome.PRESENT,
))

_SNAPSHOT_FAILED_PARK = (
    "the committed candidate for this issue was adjudicated as a split, but "
    "the immutable snapshot its children would be cut from could not be "
    "established ({reason}). No child issue was created, nothing was "
    "superseded, and the recorded verdict still stands -- the next tick "
    "retries the same snapshot against the same frozen commit without "
    "re-running any agent."
)

_UNRECORDABLE_REASON = (
    "this issue's obligation ledger holds an entry this orchestrator cannot "
    "read, so the snapshot could not be recorded on it"
)

_MISMATCH_REASON = (
    "the snapshot ref already exists at a different commit and is never "
    "overwritten"
)

_UNREADABLE_REASON = "the remote could not be asked what the ref is at"

_REFUSED_REASON = "the remote refused the ref, or would not serve it back"

# What each transport answer means to a human reading the park. The two
# successes are not here: they never reach the sentence.
_REASONS = MappingProxyType({
    _snapshot_refs.SnapshotOutcome.MISMATCH: _MISMATCH_REASON,
    _snapshot_refs.SnapshotOutcome.UNREADABLE: _UNREADABLE_REASON,
    _snapshot_refs.SnapshotOutcome.REFUSED: _REFUSED_REASON,
})


def _snapshot_for_split(context: _LateContext) -> Optional[str]:
    """Preserve this generation's candidate, and name the ref that holds it.

    Answers the ref on success and None on every failure, having already
    parked the issue and emitted the typed failure -- so a caller reads one
    value and creates nothing when it is absent.
    """
    ref = _intended_ref(context)
    if ref is None:
        return None
    if not _recorded(context, ref, LateResourceState.PENDING):
        return None
    outcome = _snapshot_refs.create_snapshot_ref(
        context.spec,
        context.spec.target_root,
        ref=ref,
        sha=context.generation.candidate_sha,
    )
    if outcome not in _ESTABLISHED:
        return _refused(context, ref, outcome)
    proven = _snapshot_refs.prove_snapshot_ref(
        context.spec,
        context.spec.target_root,
        ref=ref,
        sha=context.generation.candidate_sha,
    )
    if proven != _snapshot_refs.SnapshotOutcome.PROVEN:
        return _refused(context, ref, proven)
    return ref if _retained(context, ref) else None


def _intended_ref(context: _LateContext) -> Optional[str]:
    """The ref this generation's snapshot is written under, or a park.

    Built from the identities rather than from anything a human wrote, and
    refused rather than approximated: the fields it is assembled from come out
    of a pinned comment, so a damaged one would otherwise become a ref this
    orchestrator pushed and could not recognize again.
    """
    generation = context.generation
    try:
        return _namespace.snapshot_ref(
            issue_number=generation.current_issue,
            cycle_id=generation.cycle_id,
            generation=generation.generation,
        )
    except _namespace.InvalidSnapshotRef as refused:
        _parked(context, str(refused))
        return None


def _recorded(
    context: _LateContext, ref: str, resource_state: LateResourceState,
) -> bool:
    """Write what this generation owes the remote for the snapshot.

    Ahead of the push for `pending` and after the proof for `retained`, so the
    ledger is never behind the remote: an obligation recorded for a ref that
    was never created costs a reclamation one read, while a ref created with
    no obligation recorded is an object nothing on the issue can name.

    Refused outright when the ledger is opaque. An entry this binary cannot
    type is preserved verbatim by the write, so an update merged into the
    typed view would be dropped at the next one -- and a snapshot obligation
    that quietly disappears is a ref nobody reclaims.
    """
    try:
        updated = context.generation.with_resource(LateResource(
            kind=LateResourceKind.SNAPSHOT_REF,
            target=ref,
            resource_state=resource_state,
        ))
    except _formats.InvalidLateValue:
        log.error(
            "issue=#%d cannot record the snapshot obligation: %s",
            context.issue.number, _UNRECORDABLE_REASON,
        )
        _parked(context, _UNRECORDABLE_REASON)
        return False
    context.generation = replace(
        updated, phase=LatePhase.SNAPSHOTTING,
    )
    _late_outcome._persist(context)
    return True


def _retained(context: _LateContext, ref: str) -> bool:
    """Record the proved snapshot as one the remote is deliberately holding.

    `retained` rather than `reconciled`, because the obligation is not over: a
    snapshot is kept exactly as long as its direct consumers are live, and the
    entry has to say the difference between one being held on purpose and one
    whose reclamation was refused.
    """
    if not _recorded(context, ref, LateResourceState.RETAINED):
        return False
    _emit(context, ref, LateResourceState.RETAINED)
    log.info(
        "issue=#%d preserved candidate %s under %s before creating any child",
        context.issue.number, context.generation.candidate_sha, ref,
    )
    return True


def _refused(
    context: _LateContext,
    ref: str,
    outcome: _snapshot_refs.SnapshotOutcome,
) -> None:
    """Record and announce a snapshot that could not be established.

    The ledger entry is left behind on purpose. A `failed` obligation is still
    an obligation -- the create may have landed and the verification may be
    what failed -- so a reclamation has a ref to ask about rather than a gap.
    """
    _recorded(context, ref, LateResourceState.FAILED)
    _emit(context, ref, LateResourceState.FAILED)
    _parked(context, _REASONS.get(outcome, _REFUSED_REASON))


def _emit(
    context: _LateContext, ref: str, resource_state: LateResourceState,
) -> None:
    """Report what happened to this one external resource, on both sinks."""
    _telemetry.emit_late_event(
        context.gh,
        _events.LateEvent(
            family=_events.LateEventFamily.SNAPSHOT,
            resource=LateResource(
                kind=LateResourceKind.SNAPSHOT_REF,
                target=ref,
                resource_state=resource_state,
            ),
        ),
        context.generation,
        stage=_DECOMPOSING_STAGE,
    )


def _parked(context: _LateContext, reason: str) -> None:
    """Hand the issue back, saying which part of the snapshot did not hold."""
    _late_outcome._emit_failure(context, LateFailure.SNAPSHOT_FAILED)
    _late_outcome._park(
        context,
        _SNAPSHOT_FAILED_PARK.format(reason=reason),
        reason=_late_outcome.PARK_SNAPSHOT_FAILED,
    )
