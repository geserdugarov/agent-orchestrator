# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The pinned fields a late generation round-trips through.

Every late field is additive: an issue that never entered the late gate
carries none of them, reads back as an absent generation, and is written back
untouched, so no migration reaches a live issue and an older pinned comment
stays exactly as valid as it was. The key spellings are the compatibility
contract live issues would carry, so they are spelled once here and named
nowhere else -- `LATE_STATE_KEYS` is the whole of what one GENERATION owns
inside the pinned comment, and clearing late mode is defined as dropping
exactly it.

A record with no cycle identity is not a generation, so the write clears the
late fields rather than recording a half-record no later tick could correlate
to an audit line or a child's lineage. The two external ledgers are what it
does not clear: an obligation the remote is owed does not stop being owed
because the identity beside it was damaged, and dropping it would leave a
snapshot or a branch with nothing on the issue to reclaim it by. So an
uncorrelatable record still writes what it owes, and nothing else. Past that
gate each field says for itself what "unset" means: an identity or a SHA at
its empty value is dropped, a lineage depth of 0 is a root and is kept, and
the three flags are written only while they are set. What survives the round
trip is therefore exactly what a caller put in.

The two external ledgers are the one pair of fields this owner does not
rewrite from the typed record. A ledger the reader could not fully type comes
back verbatim beside the typed view, and the verbatim copy is what is written:
an obligation an older or newer binary recorded is still owed, and a write
that reduced the ledger to the entries this binary understood would delete it
-- leaving a cleanup looking complete and a snapshot looking reclaimable. The
`restart` owner beside this one moves the pending marker; the marker is a
pinned field, but minting and validating an identity is its own contract. The
`exemption` owner beside it holds the one late key that is deliberately NOT in
this group: the commit an accepted candidate publishes under has to survive
the clear that ends the generation which earned it, so it is spelled there and
this list drops it no more than it drops another stage's keys.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import ledgers as _ledgers
from orchestrator.workflow.late_split import payloads as _payloads
from orchestrator.workflow.late_split import restart as _restart
from orchestrator.workflow.late_split.models import LateGeneration, LatePhase

_CYCLE_ID = "late_cycle_id"
_GENERATION = "late_generation"
_ROOT_ISSUE = "late_root_issue"
_CURRENT_ISSUE = "late_current_issue"
_LINEAGE_DEPTH = "late_lineage_depth"
_SCOPE = "late_scope"
_CANDIDATE_SHA = "late_candidate_sha"
_BASE_SHA = "late_base_sha"
_THRESHOLD = "late_threshold"
_ADDITIONS = "late_additions"
_PHASE = "late_phase"
_TITLE_BODY_HASH = "late_title_body_hash"
_COMMENT_HASH = "late_comment_hash"
_COMMENT_WATERMARK_ID = "late_comment_watermark_id"
_PLAN_PR_NUMBER = "late_plan_pr_number"
_PLAN_PR_BODY = "late_plan_pr_body"
_RESOURCES = "late_resources"
_CONSUMERS = "late_consumers"
_SPLIT_CHILDREN = "late_split_children"
_LINKS_ANNOUNCED = "late_links_announced"
_OWNER_CHECK_PENDING = "late_owner_check_pending"
_CANCELLED = "late_cancelled"
_CANCELLED_AT = "late_cancelled_at"
_RESTART_PENDING = "late_restart_pending"
_RESTART_TARGET = "late_restart_target"
_RESTART_CYCLE_ID = "late_restart_cycle_id"
_RESTART_PREDECESSOR = "late_restart_predecessor"

LATE_STATE_KEYS = (
    _CYCLE_ID,
    _GENERATION,
    _ROOT_ISSUE,
    _CURRENT_ISSUE,
    _LINEAGE_DEPTH,
    _SCOPE,
    _CANDIDATE_SHA,
    _BASE_SHA,
    _THRESHOLD,
    _ADDITIONS,
    _PHASE,
    _TITLE_BODY_HASH,
    _COMMENT_HASH,
    _COMMENT_WATERMARK_ID,
    _PLAN_PR_NUMBER,
    _PLAN_PR_BODY,
    _RESOURCES,
    _CONSUMERS,
    _SPLIT_CHILDREN,
    _LINKS_ANNOUNCED,
    _OWNER_CHECK_PENDING,
    _CANCELLED,
    _CANCELLED_AT,
    _RESTART_PENDING,
    _RESTART_TARGET,
    _RESTART_CYCLE_ID,
    _RESTART_PREDECESSOR,
)


def read_late_generation(state: PinnedState) -> LateGeneration:
    """Return the late generation a pinned comment records.

    An issue with no late fields reads back as the record's defaults, which
    `LateGeneration.is_present` answers False on -- the one reading that keeps
    a legacy issue out of every late decision without a migration.

    Which reader a field is read through is the field's contract, not its
    Python type: an identity has to be positive, a measurement non-negative, a
    frozen commit a whole object id and a fingerprint a whole digest, a flag
    literally `true`, and a restart target one of the two labels a restart may
    apply. Anything else reads back
    absent, so a hand-edited or older value never becomes live state -- a
    threshold of -1 does not make an unmeasured candidate oversized, and a
    `"false"` string does not arm a cancellation or a pending restart.

    The lineage depth is the one field with no safe substitute for an
    unreadable value, so it has none: a damaged or missing depth on a recorded
    cycle reads back as unknown rather than as the root's 0, and a lineage
    already at the bound therefore stays unsplittable while its field is
    damaged. The write leaves it unknown too, so nothing normalizes the gap
    away on the next pass.
    """
    resources, opaque_resources = _ledgers.read_resources(
        state.get(_RESOURCES),
    )
    consumers, opaque_consumers = _ledgers.read_consumers(
        state.get(_CONSUMERS),
    )
    return LateGeneration(
        cycle_id=_payloads.as_identity(state.get(_CYCLE_ID)) or 0,
        generation=_payloads.as_count(state.get(_GENERATION)) or 0,
        root_issue=_payloads.as_identity(state.get(_ROOT_ISSUE)) or 0,
        current_issue=_payloads.as_identity(state.get(_CURRENT_ISSUE)) or 0,
        lineage_depth=_payloads.as_depth(state.get(_LINEAGE_DEPTH)),
        scope=_payloads.as_text(state.get(_SCOPE)) or "",
        candidate_sha=_payloads.as_hex(
            state.get(_CANDIDATE_SHA), _formats.COMMIT_LENGTHS,
        ) or "",
        base_sha=_payloads.as_hex(
            state.get(_BASE_SHA), _formats.COMMIT_LENGTHS,
        ) or "",
        threshold=_payloads.as_count(state.get(_THRESHOLD)),
        additions=_payloads.as_count(state.get(_ADDITIONS)),
        phase=_payloads.as_member(LatePhase, state.get(_PHASE)),
        title_body_hash=_payloads.as_hex(
            state.get(_TITLE_BODY_HASH), _formats.DIGEST_LENGTHS,
        ),
        comment_hash=_payloads.as_hex(
            state.get(_COMMENT_HASH), _formats.DIGEST_LENGTHS,
        ),
        comment_watermark_id=_payloads.as_identity(
            state.get(_COMMENT_WATERMARK_ID),
        ),
        plan_pr_number=_payloads.as_identity(state.get(_PLAN_PR_NUMBER)),
        plan_pr_body=_payloads.as_text(state.get(_PLAN_PR_BODY)),
        resources=resources,
        consumers=consumers,
        split_children=_ledgers.read_register(state.get(_SPLIT_CHILDREN)),
        links_announced=_payloads.as_flag(state.get(_LINKS_ANNOUNCED)),
        opaque_resources=opaque_resources,
        opaque_consumers=opaque_consumers,
        owner_check_pending=_payloads.as_flag(
            state.get(_OWNER_CHECK_PENDING),
        ),
        cancelled=_payloads.as_flag(state.get(_CANCELLED)),
        cancelled_at=_payloads.as_text(state.get(_CANCELLED_AT)),
        restart_pending=_payloads.as_flag(state.get(_RESTART_PENDING)),
        restart_target=_restart.restart_target(state.get(_RESTART_TARGET)),
        restart_cycle_id=_payloads.as_identity(
            state.get(_RESTART_CYCLE_ID),
        ),
        restart_predecessor=_payloads.as_identity(
            state.get(_RESTART_PREDECESSOR),
        ),
    )


def write_late_generation(
    state: PinnedState,
    generation: LateGeneration,
) -> None:
    """Record one late generation, replacing whatever late fields were there.

    Every late key is dropped first, so a field a caller cleared leaves no
    stale value behind for the next tick to reconcile against. Keys outside
    this domain are not read or written: the pinned comment is shared with
    every other stage, and a late write is only ever about its own fields.
    """
    clear_late_generation(state)
    for key, written in _written_fields(generation).items():
        state.set(key, written)


def clear_late_generation(state: PinnedState) -> None:
    """Drop every late field, leaving the rest of the pinned state alone."""
    for key in LATE_STATE_KEYS:
        state.data.pop(key, None)


def _written_fields(generation: LateGeneration) -> dict[str, Any]:
    """Return the pinned fields this generation records, unset ones out.

    A record with no cycle identity records only what it owes: the two
    ledgers, if either holds anything. Everything else on such a record is a
    half-record nothing could correlate, while an obligation stays an
    obligation whatever happened to the identity that was written beside it.

    A field at its own empty value -- an absent identity, an empty SHA, a
    ledger with nothing in it, a flag that is not set -- names itself None
    here and is dropped, so the pinned comment carries what this generation
    actually knows. A lineage depth of 0 is not one of them: it is the root of
    a lineage, and it is written as itself. What is dropped there is an
    unknown depth, which is not the same thing and must not be recorded as if
    it were.
    """
    ledgers = _ledger_fields(generation)
    if not generation.is_present:
        return ledgers
    fields = {
        **_evidence_fields(generation),
        **ledgers,
        _SPLIT_CHILDREN: list(generation.split_children) or None,
        _LINKS_ANNOUNCED: generation.links_announced or None,
        _OWNER_CHECK_PENDING: generation.owner_check_pending or None,
        _CANCELLED: generation.cancelled or None,
        _CANCELLED_AT: generation.cancelled_at,
        _RESTART_PENDING: generation.restart_pending or None,
        _RESTART_TARGET: generation.restart_target,
        _RESTART_CYCLE_ID: generation.restart_cycle_id,
        _RESTART_PREDECESSOR: generation.restart_predecessor,
    }
    return {
        key: written
        for key, written in fields.items()
        if written is not None
    }


def _evidence_fields(generation: LateGeneration) -> dict[str, Any]:
    """Return the identity, the frozen commits, and what they measured.

    A lineage depth of 0 is written as itself: it is the root of a lineage,
    and what is dropped instead is an unknown depth, which is not the same
    thing and must not be recorded as if it were.
    """
    phase: Optional[str] = None
    if generation.phase is not None:
        phase = str(generation.phase)
    return {
        _CYCLE_ID: generation.cycle_id or None,
        _GENERATION: generation.generation or None,
        _ROOT_ISSUE: generation.root_issue or None,
        _CURRENT_ISSUE: generation.current_issue or None,
        _LINEAGE_DEPTH: generation.lineage_depth,
        _SCOPE: generation.scope or None,
        _CANDIDATE_SHA: generation.candidate_sha or None,
        _BASE_SHA: generation.base_sha or None,
        _THRESHOLD: generation.threshold,
        _ADDITIONS: generation.additions,
        _PHASE: phase,
        _TITLE_BODY_HASH: generation.title_body_hash,
        _COMMENT_HASH: generation.comment_hash,
        _COMMENT_WATERMARK_ID: generation.comment_watermark_id,
        _PLAN_PR_NUMBER: generation.plan_pr_number,
        _PLAN_PR_BODY: generation.plan_pr_body,
    }


def _ledger_fields(generation: LateGeneration) -> dict[str, Any]:
    """Return what the two external ledgers are written back as, unset out."""
    owed = {
        _RESOURCES: _ledger_written(
            generation.opaque_resources,
            _resource_payloads(generation.resources),
        ),
        _CONSUMERS: _ledger_written(
            generation.opaque_consumers, list(generation.consumers),
        ),
    }
    return {key: ledger for key, ledger in owed.items() if ledger is not None}


def _ledger_written(opaque: Optional[str], typed: list) -> Any:
    """Return what one external ledger is written back as.

    The verbatim copy outranks the typed view wherever there is one: the typed
    view is only the entries this binary could make sense of, and writing that
    in place of the ledger is how an obligation nobody here understands would
    disappear from the issue that still owes it.
    """
    if opaque is not None:
        return json.loads(opaque)
    return typed or None


def _resource_payloads(resources: tuple) -> list:
    """Return the JSON entries a typed obligation ledger is written as."""
    return [
        {
            _ledgers.KIND_KEY: str(resource.kind),
            _ledgers.TARGET_KEY: resource.target,
            _ledgers.STATE_KEY: str(resource.resource_state),
        }
        for resource in resources
    ]
