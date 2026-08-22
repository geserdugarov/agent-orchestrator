# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The typed vocabularies a late generation is described by, and its record.

Every value a late field can hold is spelled once here, because each of them
is durable: a phase, a verdict, a typed failure, and a ledger entry's kind and
state are written into the pinned comment and read back by a later tick, so a
renamed member is a migration rather than a refactor. They are `StrEnum`
members for the same reason the workflow labels are -- a member IS its wire
string, so the pinned JSON, the audit payload, and a comparison against a
plain string all read the same value.

`LateGeneration` is the whole record one generation is reconciled from, held
frozen because every field on it is evidence: the SHAs a reconciliation is
allowed to act on, the measurement a verdict answers, and the resources the
remote still owes are what a crashed tick reads back instead of re-deriving
from a moving branch. The three transforms that need to change one --
recording an obligation, recording a consumer, and cancelling -- return a new
record rather than mutating this one, so a caller cannot half-apply a change
it then fails to persist.

The lineage cap is here rather than beside a caller because it is the record's
own invariant: `MAX_LINEAGE_DEPTH` bounds how deep automatic splitting may go,
and a depth at or past it (a hand-edited pinned comment included) reads as
"may not split" rather than as an error to recover from. A depth that is not
known at all is the same answer: it is `None` rather than 0, because a
generation whose depth could not be read is not a root, and a damaged field on
a lineage already at the bound must not read back as one free to split again.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Optional

from orchestrator.workflow.late_split import formats as _formats

# How deep automatic splitting may go. The root issue of a lineage is depth 0,
# so a generation may only split while its own depth is strictly below this:
# the deepest child a split can create sits exactly at the bound and must
# resolve as one change or ask a human. It is a safety invariant, not a knob,
# which is why no configuration reads it.
MAX_LINEAGE_DEPTH = 3

# How long a resource target may be. It is never recorded -- only digested
# into an identifier -- but a ref, a branch, or an issue number that does not
# fit here is not one.
MAX_RESOURCE_TARGET = 512

# What a caller is told when it tries to update a ledger the write would not
# carry its update into. Spelled once because both transforms refuse alike.
_OPAQUE_LEDGER = "{0} cannot be updated while the ledger is opaque"


class LatePhase(StrEnum):
    """The reconciliation boundary a generation last reached.

    Each member names a step that persists before it acts, so a tick that
    crashed mid-step reads the phase back and reconciles the same step rather
    than starting a new one.
    """

    MEASURING = "measuring"
    HOLDING_PLAN_PR = "holding_plan_pr"
    ADJUDICATING = "adjudicating"
    OWNER_CHECK = "owner_check"
    SNAPSHOTTING = "snapshotting"
    SPLITTING = "splitting"
    SUPERSEDING = "superseding"
    CLEANING_UP = "cleaning_up"
    CANCELLING = "cancelling"
    RESTARTING = "restarting"


class LateVerdict(StrEnum):
    """What one late adjudication decided about an oversized candidate."""

    SINGLE = "single"
    SPLIT = "split"
    QUESTION = "question"


class LateFailure(StrEnum):
    """The typed failures a late reconciliation records instead of guessing.

    None of them is "small": each names the step that could not be completed,
    so the retry that follows reconciles that step rather than re-running the
    agent whose work it was about to publish.
    """

    MEASUREMENT_FAILED = "measurement_failed"
    PLAN_PR_HOLD_FAILED = "plan_pr_hold_failed"
    OWNER_READ_FAILED = "owner_read_failed"
    PR_RECONCILE_FAILED = "pr_reconcile_failed"
    SNAPSHOT_FAILED = "snapshot_failed"
    CHILD_CREATE_FAILED = "child_create_failed"
    SUPERSESSION_FAILED = "supersession_failed"
    BRANCH_CLEANUP_FAILED = "branch_cleanup_failed"
    SNAPSHOT_DELETE_FAILED = "snapshot_delete_failed"
    RESTART_FAILED = "restart_failed"


class LateResourceKind(StrEnum):
    """What kind of external thing a ledger entry holds the generation to."""

    SNAPSHOT_REF = "snapshot_ref"
    BRANCH = "branch"
    PLAN_PR = "plan_pr"
    CHILD = "child"


class LateResourceState(StrEnum):
    """How far one recorded external obligation has been reconciled.

    `RETAINED` is not a failure: a snapshot whose direct consumers are still
    live is deliberately kept, and saying so is what keeps a retained ref
    apart from one whose deletion was refused.
    """

    PENDING = "pending"
    RETAINED = "retained"
    RECONCILED = "reconciled"
    FAILED = "failed"


@dataclass(frozen=True)
class LateResource:
    """One external resource this generation owes the remote.

    `target` is the resource's own identifier -- a ref, a branch, a pull
    request number, an issue number -- and is recorded so a reconciliation
    acts on the exact thing the generation created rather than on whatever
    currently looks like it.
    """

    kind: LateResourceKind
    target: str
    resource_state: LateResourceState = LateResourceState.PENDING


@dataclass(frozen=True)
class LateGeneration:
    """One late generation's whole durable record.

    An issue that never entered the late gate reads back as this record's
    defaults, which is what `is_present` answers on: the fields are additive,
    so a legacy pinned comment needs no migration and writing an absent
    generation back adds no key to it.

    The two `opaque_*` fields are the ledgers this binary could not fully
    type, kept verbatim rather than reduced to what it understood. An
    obligation dropped on read would be an obligation dropped on the next
    write, and a snapshot whose consumer ledger was silently emptied reads as
    one nobody is waiting on -- so what cannot be typed is carried through
    untouched and `has_opaque_ledger` says so out loud.

    `split_children` and `links_announced` are the split transaction's own
    receipts, and they live on the generation rather than beside the stage's
    shared keys precisely because they have to be scoped to ONE adjudication.
    The stage's `children` list belongs to whichever decomposition last wrote
    it -- an issue that was decomposed, saw its children resolve, and then
    implemented an oversized candidate still carries the old one -- so a
    transaction reading it would adopt completed issues by manifest index.
    `split_children` is ordered and positional for the same reason: entry `i`
    is the child that owns slice `i` of this manifest.

    `owner_check_pending` is the one field that records an unfinished READ
    rather than a fact about the candidate: a completed run whose owner could
    not be re-read leaves it set, and while it is set no later tick may treat
    this generation as settled, however small, decided, or parked it looks.
    It is durable because nothing else would bring the workflow back to that
    read -- a below-threshold revision and an issue parked for a human both
    stop the tick long before the guard would run again.
    """

    cycle_id: int = 0
    generation: int = 0
    root_issue: int = 0
    current_issue: int = 0
    lineage_depth: Optional[int] = None
    scope: str = ""
    candidate_sha: str = ""
    base_sha: str = ""
    threshold: Optional[int] = None
    additions: Optional[int] = None
    phase: Optional[LatePhase] = None
    title_body_hash: Optional[str] = None
    comment_hash: Optional[str] = None
    comment_watermark_id: Optional[int] = None
    plan_pr_number: Optional[int] = None
    plan_pr_body: Optional[str] = None
    resources: tuple[LateResource, ...] = ()
    consumers: tuple[int, ...] = ()
    split_children: tuple[int, ...] = ()
    links_announced: bool = False
    opaque_resources: Optional[str] = None
    opaque_consumers: Optional[str] = None
    owner_check_pending: bool = False
    cancelled: bool = False
    cancelled_at: Optional[str] = None
    restart_pending: bool = False
    restart_target: Optional[str] = None
    restart_cycle_id: Optional[int] = None
    restart_predecessor: Optional[int] = None

    @property
    def is_present(self) -> bool:
        """Whether a late cycle was ever recorded on this issue."""
        return self.cycle_id > 0

    @property
    def is_oversized(self) -> bool:
        """Whether the measurement is strictly past the threshold it named.

        Strictly: a candidate exactly at the configured value is accepted, so
        the trigger cannot move by one line when the threshold is retuned. An
        unmeasured generation is not oversized -- a missing measurement is a
        typed failure to reconcile, never a small candidate.
        """
        if self.threshold is None or self.additions is None:
            return False
        return self.additions > self.threshold

    @property
    def may_split(self) -> bool:
        """Whether this generation is allowed to create another one.

        Read fail-closed, so every depth that is not a real one below the
        bound refuses the split rather than unlocking a generation the cap
        exists to forbid: a depth at or past the bound, a negative one, one
        that is not a whole number at all, and an unknown one -- which is what
        a damaged or missing field on a recorded cycle reads back as -- all
        answer False.
        """
        if not _formats.whole_number(self.lineage_depth):
            return False
        return 0 <= self.lineage_depth < MAX_LINEAGE_DEPTH

    @property
    def has_opaque_ledger(self) -> bool:
        """Whether an external obligation here is one this binary cannot type.

        The one answer a reclamation may not read past: an unknown consumer or
        an unknown resource is still an obligation, so nothing may treat the
        cleanup as complete or the snapshot as reclaimable while this holds.
        """
        return (
            self.opaque_resources is not None
            or self.opaque_consumers is not None
        )

    def with_resource(self, resource: LateResource) -> LateGeneration:
        """Return this record with one external obligation recorded.

        Keyed on kind and target, so a reconciliation that repeats after a
        crash updates the entry it already wrote instead of appending a second
        one -- the ledger stays as bounded as the resources actually created.

        Refused while the resource ledger is opaque. What gets written back
        then is the verbatim copy, so the update would be returned here and
        lost at the next write -- and merging into a ledger this binary could
        not read is exactly the rewrite the verbatim copy exists to prevent. A
        caller that reaches this has a ledger a human has to settle first.
        """
        if self.opaque_resources is not None:
            raise _formats.InvalidLateValue(_OPAQUE_LEDGER.format("resources"))
        kept = tuple(
            entry for entry in self.resources
            if (entry.kind, entry.target) != (resource.kind, resource.target)
        )
        return replace(self, resources=(*kept, resource))

    def with_consumers(self, numbers: tuple[int, ...]) -> LateGeneration:
        """Return this record with direct snapshot consumers recorded.

        Deduplicated and ordered, because the ledger is what a reclamation
        sweep walks: a child recorded twice would be asked about twice, and
        the order it was created in is not what decides anything.

        Only a positive whole number is an issue: converting anything else
        would put a consumer nobody can ask about into the one ledger that
        decides whether a snapshot may be reclaimed -- `True` is not issue 1,
        2.5 is not issue 2, and "7" is a string somebody hand-edited.

        Refused while the consumer ledger is opaque, for the reason
        `with_resource` is: the verbatim copy is what a write puts back, so an
        update accepted here would disappear at the next one.
        """
        if self.opaque_consumers is not None:
            raise _formats.InvalidLateValue(_OPAQUE_LEDGER.format("consumers"))
        for number in numbers:
            if not _formats.whole_number(number) or number <= 0:
                raise _formats.InvalidLateValue(
                    f"consumer is not an issue ({type(number).__name__})",
                )
        merged = set(self.consumers) | set(numbers)
        return replace(self, consumers=tuple(sorted(merged)))

    def with_split_children(self, numbers: tuple[int, ...]) -> LateGeneration:
        """Return this record with the ordered child register replaced.

        Replaced rather than merged, because the register is positional and a
        caller rebuilding it walks the whole manifest: merging would leave a
        stale tail behind whenever a re-run shortened it. Only positive whole
        numbers are children, for the reason the consumer ledger says so --
        a value nobody can ask GitHub about is not one to adopt by index.
        """
        for number in numbers:
            if not _formats.whole_number(number) or number <= 0:
                raise _formats.InvalidLateValue(
                    f"child is not an issue ({type(number).__name__})",
                )
        return replace(self, split_children=tuple(numbers))

    def cancel(self, stamp: str) -> LateGeneration:
        """Return this record marked cancelled, keeping the first stamp.

        Cancellation is irreversible within a cycle: once the owner has been
        observed closed, a later tick that observes it reopened re-runs this
        and must not move the moment the cleanup obligation was taken on.
        """
        return replace(
            self,
            cancelled=True,
            cancelled_at=self.cancelled_at or stamp,
        )
