# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The children a late split creates, and what each is born knowing.

The initial decomposer's children start from nothing: an issue, a scope, and a
base branch. These start from work that already exists, and everything here is
about handing that work over without handing over the branch it was committed
on -- which is about to be superseded and closed.

**What a child is told.** Its own declared scope, in the words the adjudication
used; the base branch it targets; the snapshot ref and the exact commit under
it; and where it sits in the lineage. Selective reuse is spelled out because
the alternative has to be ruled out in writing: a child may cherry-pick a
coherent commit or copy selected paths, and may not mechanically split hunks to
hit a size target. File and hunk boundaries do not express issue scope, so a
change partitioned along them is one nobody can build or review -- the judgment
about what belongs to a slice stays with the developer who implements it.

**What a child is born with.** The same parent link and creation stamp every
split child gets, plus the ancestry: the lineage this child continues, the
adjudication that created it, and the snapshot it may reuse. That record is
what the child's own size gate reads when it mints a generation, so automatic
splitting stops at the same bound three generations down as it does at the
root, and what its own late prompt states as the declared scope is the slice
written here rather than an issue body somebody has since edited.

**The order children are created in.** The initial mode's crash-safe sequence,
extended by one write: the count and the umbrella flag go down before the first
child exists, and each child's number is recorded in the parent -- in the
children list, in the direct-consumer ledger, and as an obligation of its own
-- in one write, before anything else is done with it. A crash in that window
costs an orphan child an operator can see, never a duplicate the retry would
create, and never a consumer the snapshot's reclamation would fail to wait for.

**Re-entry is a reuse, not a repeat.** A retry walks the same manifest and
adopts every child the parent already records rather than creating a second
one, then re-seeds it: the seed is the one step that can have been lost after
the number was durable, and writing it again costs a read and changes nothing
on a child that already carries it. The child's own state is read and added to
rather than replaced, because by the time a retry runs, a child may already be
implementing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional

from github.Issue import Issue

from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import identity as _identity
from orchestrator.workflow.late_split import lineage as _lineage
from orchestrator.workflow.late_split.models import (
    MAX_LINEAGE_DEPTH,
    LateFailure,
    LatePhase,
    LateResource,
    LateResourceKind,
    LateResourceState,
)
from orchestrator.workflow.stages.decomposition import (
    late_outcome as _late_outcome,
)
from orchestrator.workflow.stages.decomposition import split as _split
from orchestrator.workflow.stages.decomposition import state as _state
from orchestrator.workflow.stages.decomposition.late_models import _LateContext
from orchestrator.workflow.stages.decomposition.models import _SplitPlan

log = logging.getLogger("orchestrator.workflow")

_EXPECTED_CHILDREN = "expected_children_count"

_DEP_GRAPH = "dep_graph"

_WHOLE_ISSUE = "(the whole issue)"

_CHILD_CREATE_PARK = (
    "the committed candidate for this issue was adjudicated as a split and "
    "its snapshot is safe, but {child} could not be created, recorded, or "
    "seeded. The snapshot ref and every child already created are recorded on "
    "this issue; the next tick adopts them and continues from the same "
    "manifest without re-running any agent."
)

_REUSE_BLOCK = """---

## Reusing the work already committed for #{parent}

A developer already implemented issue #{parent} and committed the result. That
change measured past this repository's size ceiling, so it was split and you
own the slice above. The commit is preserved on an immutable snapshot ref --
its branch is superseded and its pull request is closed, so the snapshot is the
only place to read it from.

- ancestor snapshot ref: `{ref}`
- exact snapshot commit: `{sha}`
- the base it was cut against: `{base_sha}`
- target base branch: `{base_branch}`
- lineage: root #{root}, parent #{parent}, depth {depth} of at most {bound}
- adjudication: cycle {cycle}, generation {generation}

Read it, from this repository:

```sh
git fetch {remote} '+{ref}:{ref}'          # only if the ref is not here yet
git log --oneline {base_sha}..{sha}
git diff {base_sha}...{sha}                # three dots: what it ADDS
```

Reuse only what your scope covers, and do it one of two ways:

- **cherry-pick a coherent commit** -- `git cherry-pick <commit>` -- when a
  whole commit belongs to your slice; or
- **copy selected paths** -- `git checkout {ref} -- <path>` -- when it does
  not, and then finish the slice by hand.

Do **not** split hunks mechanically to make the change smaller. File and hunk
boundaries do not express issue scope, and a change partitioned along them is
one nobody can build or review. Where your slice needs part of a file, write
that part; where it needs none of it, leave the file out. Anything the snapshot
does not cover, implement normally.
"""


@dataclass(frozen=True)
class _ChildWalk:
    """One pass over a split manifest, and what the parent already records.

    `known` is read once, before the walk, and never again: it is both the
    register a resumed pass adopts from and the floor its writes may not go
    below. Re-reading it per child would read the walk's own partial write --
    the second index would find only the first child recorded, decide the
    slice it is on has none, and create a duplicate beside the one that
    exists.
    """

    plan: _SplitPlan
    known: tuple[int, ...]
    snapshot_ref: str

    def recorded_numbers(self) -> list:
        """The children the parent records once this step is durable.

        Monotonic on purpose. What the walk has placed so far, extended by
        whatever the previous pass recorded beyond it, so a crash in the
        middle of a resumed pass can never leave the parent knowing about
        fewer children than exist on GitHub.
        """
        placed = [number for number, _ in self.plan.created]
        return placed + list(self.known[len(placed):])


def _create_late_children(
    context: _LateContext, manifest: tuple, snapshot_ref: str,
) -> Optional[_SplitPlan]:
    """Create or adopt every child of this split, in the crash-safe order.

    Returns the populated plan, or None when a child could not be created,
    recorded, or seeded and the issue was parked -- in which case the caller
    creates nothing further and the next tick resumes from what is recorded.
    """
    _prepared(context, manifest)
    walk = _ChildWalk(
        plan=_SplitPlan.start(list(manifest), True),
        known=tuple(_recorded_numbers(context)),
        snapshot_ref=snapshot_ref,
    )
    for index, child in enumerate(manifest):
        created = _child_issue(context, walk, index, child)
        if created is None:
            return None
        if not _recorded(context, walk, index, created, child):
            return None
        if not _seeded(context, walk, created, child):
            return None
    return walk.plan


def _prepared(context: _LateContext, manifest: tuple) -> None:
    """Force this issue to be an umbrella, before a single child exists.

    Both fields are what a tick that died mid-loop is read back through: the
    count tells a partial split from a finished one, and the umbrella flag
    says the parent has no implementation of its own to return to. A split
    that recorded neither would leave a parent nobody could finish.

    The flag rather than the label. The label is the last thing this
    transaction writes, because a live generation pins `workflow:decomposing`
    and an issue relabelled ahead of its own retirement is one the guard puts
    straight back.
    """
    context.state.set(_EXPECTED_CHILDREN, len(manifest))
    context.state.set(_state._UMBRELLA, True)
    context.generation = replace(
        context.generation, phase=LatePhase.SPLITTING,
    )
    _late_outcome._persist(context)


def _child_issue(
    context: _LateContext, walk: _ChildWalk, index: int, child: dict,
) -> Optional[Issue]:
    """Adopt the child this index already has, or create it exactly once.

    Adoption is what keeps a retry from opening a second issue for a slice
    that already has one: the parent's own recorded list is the register, and
    it is written in the same durable step the creation is.
    """
    try:
        return _adopted_or_created(context, walk, index, child)
    except Exception:
        log.exception(
            "issue=#%d could not establish late split child %d (%r)",
            context.issue.number, index, child.get("title"),
        )
        _parked(context, f"child {index} ({child.get('title')!r})")
        return None


def _adopted_or_created(
    context: _LateContext, walk: _ChildWalk, index: int, child: dict,
) -> Issue:
    """Return the child at this index, opening one only where none exists."""
    if index < len(walk.known):
        return context.gh.get_issue(walk.known[index])
    return context.gh.create_child_issue(
        title=child["title"],
        body=_child_body(context, child, walk.snapshot_ref),
        parent_number=context.issue.number,
        labels=_split._child_initial_labels(),
    )


def _recorded(
    context: _LateContext,
    walk: _ChildWalk,
    index: int,
    child_issue: Issue,
    child: dict,
) -> bool:
    """Record this child as a child, a consumer, and an obligation, at once.

    One write, because the three say the same thing to different readers: the
    parent's walk drives the tree, the consumer ledger is what decides whether
    the snapshot may ever be reclaimed, and the obligation entry is what a
    cleanup asks GitHub about. A child recorded as one and not the others is a
    child the snapshot would stop waiting for.

    It is also the durable step that has to precede activation, which is why
    it is here rather than folded into the final write: a runnable child whose
    slot the ledger never took is one a reclamation could delete the snapshot
    out from under.
    """
    walk.plan.record(index, child_issue.number, child)
    try:
        owed = context.generation.with_consumers(
            (child_issue.number,),
        ).with_resource(LateResource(
            kind=LateResourceKind.CHILD,
            target=str(child_issue.number),
            resource_state=LateResourceState.PENDING,
        ))
    except _formats.InvalidLateValue:
        log.exception(
            "issue=#%d cannot record child #%d on its ledgers",
            context.issue.number, child_issue.number,
        )
        _parked(context, f"child #{child_issue.number} ({child.get('title')!r})")
        return False
    context.generation = replace(owed, phase=LatePhase.SPLITTING)
    context.state.set(_state._CHILDREN, walk.recorded_numbers())
    if walk.plan.dep_graph:
        context.state.set(_DEP_GRAPH, walk.plan.dep_graph)
    _late_outcome._persist(context)
    return True


def _seeded(
    context: _LateContext,
    walk: _ChildWalk,
    child_issue: Issue,
    child: dict,
) -> bool:
    """Give this child its parent link, its stamp, and its ancestry.

    The child's own state is read and added to rather than written fresh, for
    the case this step exists to repair: a retry reaches a child that was
    already created, and by then it may be implementing. Writing a fresh
    record over it would take its work with it.
    """
    try:
        _seed_child_state(context, walk, child_issue, child)
    except Exception:
        log.exception(
            "issue=#%d could not seed child #%d with its ancestry",
            context.issue.number, child_issue.number,
        )
        _parked(context, f"child #{child_issue.number} ({child.get('title')!r})")
        return False
    return True


def _seed_child_state(
    context: _LateContext,
    walk: _ChildWalk,
    child_issue: Issue,
    child: dict,
) -> None:
    """Add the parent link, the stamp, and the ancestry to a child's state."""
    child_state = context.gh.read_pinned_state(child_issue)
    child_state.set(_state._PARENT_NUMBER, context.issue.number)
    if child_state.get(_state._CREATED_AT) is None:
        child_state.set(_state._CREATED_AT, _usage._now_iso())
    _lineage.write_late_ancestry(
        child_state, _child_ancestry(context, child, walk.snapshot_ref),
    )
    context.gh.write_pinned_state(child_issue, child_state)


def _child_ancestry(
    context: _LateContext, child: dict, snapshot_ref: str,
) -> _lineage.LateAncestry:
    """What this child inherits from the generation that created it.

    The depth is asked of the lineage owner rather than incremented here, so
    the bound is enforced at the one place a child's depth is computed. The
    caller has already refused a split the lineage forbids; asking again costs
    nothing and means no path here can produce a child past the cap.
    """
    generation = context.generation
    return _lineage.LateAncestry(
        root_issue=generation.root_issue,
        lineage_depth=_identity.child_lineage_depth(generation.lineage_depth),
        parent_issue=generation.current_issue,
        cycle_id=generation.cycle_id,
        generation=generation.generation,
        snapshot_ref=snapshot_ref,
        snapshot_sha=generation.candidate_sha,
        base_branch=context.spec.base_branch,
        scope=_declared_scope(child),
    )


def _child_body(
    context: _LateContext, child: dict, snapshot_ref: str,
) -> str:
    """The issue body one child is created with.

    The manifest's own body first, because that is the slice a human reads,
    and the reuse block after it -- so an issue whose snapshot has since been
    reclaimed still opens as a description of work rather than as instructions
    for a ref that is gone.
    """
    generation = context.generation
    return "\n\n".join((
        _declared_scope(child),
        _REUSE_BLOCK.format(
            parent=generation.current_issue,
            ref=snapshot_ref,
            sha=generation.candidate_sha,
            base_sha=generation.base_sha,
            base_branch=context.spec.base_branch,
            remote=context.spec.remote_name,
            root=generation.root_issue,
            depth=_identity.child_lineage_depth(generation.lineage_depth),
            bound=MAX_LINEAGE_DEPTH,
            cycle=generation.cycle_id,
            generation=generation.generation,
        ),
    ))


def _declared_scope(child: dict) -> str:
    """The slice this child owns, as the adjudication wrote it."""
    written = child.get("body")
    if isinstance(written, str) and written.strip():
        return written.strip()
    return _WHOLE_ISSUE


def _recorded_numbers(context: _LateContext) -> list:
    """The child numbers this parent already records, in manifest order."""
    recorded = context.state.get(_state._CHILDREN) or []
    return [number for number in recorded if _formats.whole_number(number)]


def _parked(context: _LateContext, described: str) -> None:
    """Hand the issue back, naming the child that could not be established."""
    _late_outcome._emit_failure(context, LateFailure.CHILD_CREATE_FAILED)
    _late_outcome._park(
        context,
        _CHILD_CREATE_PARK.format(child=described),
        reason=_late_outcome.PARK_CHILDREN_FAILED,
    )
