# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a child born of a late split inherits, and where it reads it back.

A generation is a record about an issue's OWN candidate; this is the record
about the one it came out of. They are separate fields because they answer
separate questions and have separate lifetimes: a generation is minted,
adjudicated, and retired inside one issue, while an ancestry is written once
when the child is created and is still true after that child has been
implemented, split again, and closed.

Four things travel, and each has a reader that cannot do without it:

- **The lineage.** The root issue and the depth this child is born at are what
  the child's own size gate mints its generation from, so automatic splitting
  stops at the same bound whether an issue is the root or three generations
  down. A child that could not say how deep it is would read as a root and buy
  the lineage another generation.
- **The ancestor's identity.** The cycle, the generation, and the issue that
  split are what a telemetry record about this child correlates to the
  adjudication that created it, and what a human reading a stuck child follows
  back.
- **The snapshot.** The ref and the exact commit under it are the only durable
  pointer to the work this child is meant to reuse -- the branch it was
  committed on is superseded, and the pull request that carried it is closed.
- **The declared scope.** The slice of the parent's scope this child owns, in
  the words the adjudication used. It is what the child's own late prompt
  states, so an indivisible slice that is still large gets a fast `single`
  rather than being re-split against a scope nobody wrote down.

Everything is additive and read fail-closed, exactly as the generation's own
fields are: an issue that was never born of a split carries none of these
keys and reads back as the record's defaults, and a hand-edited field reads
back absent rather than becoming a lineage nobody wrote. The snapshot ref is
checked against the namespace that owns it rather than merely for being a
string, because a child pointed at a ref outside it would be pointed at a
branch, a tag, or nothing at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from orchestrator.git.snapshots import namespace as _namespace
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import payloads as _payloads

_ROOT_ISSUE = "late_ancestry_root_issue"
_DEPTH = "late_ancestry_depth"
_PARENT_ISSUE = "late_ancestry_parent"
_CYCLE_ID = "late_ancestry_cycle_id"
_GENERATION = "late_ancestry_generation"
_SNAPSHOT_REF = "late_ancestry_snapshot_ref"
_SNAPSHOT_SHA = "late_ancestry_snapshot_sha"
_BASE_BRANCH = "late_ancestry_base_branch"
_SCOPE = "late_declared_scope"

LATE_ANCESTRY_KEYS = (
    _ROOT_ISSUE,
    _DEPTH,
    _PARENT_ISSUE,
    _CYCLE_ID,
    _GENERATION,
    _SNAPSHOT_REF,
    _SNAPSHOT_SHA,
    _BASE_BRANCH,
    _SCOPE,
)


@dataclass(frozen=True)
class LateAncestry:
    """Where one issue came from, when it came from a late split.

    Frozen for the reason the generation is: every field is evidence a later
    tick acts on rather than re-derives. The snapshot the child reuses, the
    depth its own splitting is bounded by, and the adjudication its records
    correlate to are all facts about an event that has already happened.
    """

    root_issue: int = 0
    lineage_depth: Optional[int] = None
    parent_issue: int = 0
    cycle_id: int = 0
    generation: int = 0
    snapshot_ref: str = ""
    snapshot_sha: str = ""
    base_branch: str = ""
    scope: str = ""

    @property
    def is_present(self) -> bool:
        """Whether this issue was born of a late split at all."""
        return self.parent_issue > 0 and self.cycle_id > 0

    @property
    def has_snapshot(self) -> bool:
        """Whether a usable pointer to the preserved candidate survived.

        Both halves or neither: a ref with no commit beside it cannot be
        verified against anything, and a commit with no ref names work nothing
        can fetch. A child that reads False here has lost the artifact it was
        meant to reuse, which is a thing to say out loud rather than to
        reconstruct.
        """
        return bool(self.snapshot_ref) and bool(self.snapshot_sha)


def read_late_ancestry(state: PinnedState) -> LateAncestry:
    """Return the ancestry a pinned comment records for this issue.

    An issue that was never split into reads back as the defaults, which
    `is_present` answers False on -- the one reading that keeps every issue
    that reached this workflow another way out of every lineage decision
    without a migration.
    """
    return LateAncestry(
        root_issue=_payloads.as_identity(state.get(_ROOT_ISSUE)) or 0,
        lineage_depth=_payloads.as_depth(state.get(_DEPTH)),
        parent_issue=_payloads.as_identity(state.get(_PARENT_ISSUE)) or 0,
        cycle_id=_payloads.as_identity(state.get(_CYCLE_ID)) or 0,
        generation=_payloads.as_count(state.get(_GENERATION)) or 0,
        snapshot_ref=_snapshot_ref(state.get(_SNAPSHOT_REF)),
        snapshot_sha=_payloads.as_hex(
            state.get(_SNAPSHOT_SHA), _formats.COMMIT_LENGTHS,
        ) or "",
        base_branch=_payloads.as_text(state.get(_BASE_BRANCH)) or "",
        scope=_payloads.as_text(state.get(_SCOPE)) or "",
    )


def write_late_ancestry(state: PinnedState, ancestry: LateAncestry) -> None:
    """Record one ancestry, replacing whatever ancestry keys were there.

    Every key is dropped first, so a field a caller cleared leaves no stale
    value for the next tick to read: a child re-seeded against a snapshot that
    no longer exists must not keep pointing at the old one. Keys outside this
    group are untouched -- the pinned comment is shared with every stage, and
    this write is only ever about its own fields.
    """
    clear_late_ancestry(state)
    for key, written in _written_fields(ancestry).items():
        state.set(key, written)


def clear_late_ancestry(state: PinnedState) -> None:
    """Drop every ancestry field, leaving the rest of the state alone."""
    for key in LATE_ANCESTRY_KEYS:
        state.data.pop(key, None)


def _snapshot_ref(raw: Any) -> str:
    """Return a recorded snapshot ref, or "" unless it is one of ours.

    Checked against the namespace rather than for being a string, because what
    this field is FOR is telling a child which ref to fetch: a value outside
    the namespace names a branch, a tag, or nothing, and handing one to a
    child is worse than handing it none.
    """
    return raw if _namespace.is_snapshot_ref(raw) else ""


def _written_fields(ancestry: LateAncestry) -> dict[str, Any]:
    """Return the pinned fields this ancestry records, unset ones out.

    A field at its own empty value names itself None here and is dropped, so
    the pinned comment carries what the split actually knew. A lineage depth
    of 0 is not one of them: it is the root of a lineage and is written as
    itself, while an unknown depth is dropped -- a child whose depth nobody
    recorded may not read back as a root free to split again.
    """
    fields = {
        _ROOT_ISSUE: ancestry.root_issue or None,
        _DEPTH: ancestry.lineage_depth,
        _PARENT_ISSUE: ancestry.parent_issue or None,
        _CYCLE_ID: ancestry.cycle_id or None,
        _GENERATION: ancestry.generation or None,
        _SNAPSHOT_REF: ancestry.snapshot_ref or None,
        _SNAPSHOT_SHA: ancestry.snapshot_sha or None,
        _BASE_BRANCH: ancestry.base_branch or None,
        _SCOPE: ancestry.scope or None,
    }
    return {
        key: written
        for key, written in fields.items()
        if written is not None
    }
