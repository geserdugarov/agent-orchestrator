# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a guarded split does, in the one order every crash in it is safe in.

Snapshot, children, links, supersession, activation, cleanup -- and the order
is the contract, because each step is an effect on GitHub or on a remote that
the process can die immediately after. Every one of them is preceded by the
durable fact that lets the next tick tell "already done" from "never started",
and every one of them is idempotent when that fact turns out to be ambiguous.

**Snapshot first, before any child.** A split ends with the parent's branch
superseded and its pull request closed, so the committed work survives only as
the ref `late_snapshot` creates and proves. A child created ahead of that ref
would be pointed at a branch that is about to stop existing.

**Then the children, each recorded before anything is done with it.** The
umbrella flag and the expected count go down before the first one, and every
child lands in the children list, the direct-consumer ledger, and the
obligation ledger in a single write. That write is what makes the snapshot's
reclamation wait for it, which is why it has to be durable before the child can
run.

**Then the links, and only then the supersession.** The parent says what it
became and where its children are; the plan pull request says it is superseded,
names the umbrella, the children, the snapshot ref, and the exact commit, and
is closed. Neither can be undone, and both are idempotent -- the parent's
announcement is gated on the durable stamp beside it, and the pull request's is
gated on its own hidden marker on the thread, so a crash between a post and the
write recording it costs at most a repeat that never happens twice.

**Then the label, the retirement, and the activation, in that order.** The
generation is retired -- identity, commits, and both ledgers kept, the
measurement dropped -- in the same write that hands the issue to `umbrella`,
because a live generation pins `workflow:decomposing` and the relabel guard
would put an early flip straight back. Activation runs after that write for the
reason the initial split's does: a crash between them must not leave a runnable
child under a parent still labelled `decomposing`.

**Cleanup last, and never in the way.** The superseded branch is an obligation
recorded on the ledger and reconciled after the children are running: a delete
that fails leaves a `failed` entry to retry and does not hold a single child
back, while the umbrella's own terminal completion is what it does block. That
asymmetry is the point -- children waiting on a branch deletion would be work
stalled on tidiness, whereas an umbrella closing over an unreclaimed remote
would be an obligation nobody ever settles.

Every failure is a park with the recorded verdict left standing, so the retry
costs a GitHub read rather than an agent: the next eligible tick reuses the
same recorded split, re-enters here, adopts everything already durable, and
carries on from the first step that did not land.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Optional

from orchestrator.git.worktrees import cleanup as _worktree_cleanup
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.late_split import events as _events
from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import telemetry as _telemetry
from orchestrator.workflow.late_split.models import (
    LateFailure,
    LateGeneration,
    LatePhase,
    LateResource,
    LateResourceKind,
    LateResourceState,
)
from orchestrator.workflow.stages.decomposition import (
    late_children as _late_children,
)
from orchestrator.workflow.stages.decomposition import late_hold as _late_hold
from orchestrator.workflow.stages.decomposition import (
    late_outcome as _late_outcome,
)
from orchestrator.workflow.stages.decomposition import (
    late_snapshot as _late_snapshot,
)
from orchestrator.workflow.stages.decomposition import split as _split
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateAdjudicationRun,
    _LateContext,
    _LateDisposition,
)
from orchestrator.workflow.stages.decomposition.models import _SplitPlan
from orchestrator.workflow.state import WorkflowLabel

log = logging.getLogger("orchestrator.workflow")

_DECOMPOSING_STAGE = "decomposing"

_DECOMPOSED_AT = "decomposed_at"

_PR_NUMBER = "pr_number"

# Stamped on every supersession notice so a retry recognizes one it posted even
# when the write that was supposed to record it never landed. This mode's own,
# invisible in the rendered thread.
SUPERSESSION_MARKER = "<!--orchestrator-late-supersession-->"

_FORWARD_LINKS = (
    ":scissors: the late decomposer read the committed candidate `{sha}` as "
    "{count} separable changes, so this issue becomes an umbrella and the "
    "work is handed to its children:\n\n{children}\n\nThe committed work is "
    "preserved on the immutable ref `{ref}` at `{sha}`; each child reuses the "
    "part of it their own scope covers. This issue has no implementation of "
    "its own and closes once every child resolves."
)

_SUPERSESSION_NOTICE = (
    ":scissors: **Superseded.** The committed implementation for issue "
    "#{parent} was adjudicated as {count} separable changes, so this pull "
    "request is closed without merging and issue #{parent} is now an "
    "umbrella.\n\nThe work it carried is preserved on the immutable ref "
    "`{ref}` at `{sha}` -- nothing is lost, and each child reuses the part of "
    "it their scope covers:\n\n{children}\n\n"
    f"{SUPERSESSION_MARKER}"
)

_OPAQUE_LEDGER_PARK = (
    "the committed candidate for this issue was adjudicated as a split, but "
    "this issue's external-obligation ledger holds an entry this orchestrator "
    "cannot read. Nothing was snapshotted, created, or superseded: a split "
    "records a snapshot and one consumer per child on exactly that ledger, "
    "and merging into one it cannot read would drop whatever it does not "
    "understand. Settle the ledger by hand, and the next tick continues from "
    "the same recorded verdict."
)

_AT_BOUND_PARK = (
    "the committed candidate for this issue was adjudicated as a split, but "
    "its lineage may not split any further. Nothing was created. This is a "
    "contradiction between a recorded verdict and the lineage bound, and it "
    "has to be resolved by hand: land the candidate as one change, or split "
    "it manually."
)

_SUPERSESSION_FAILED_PARK = (
    "the committed candidate for this issue was split and its snapshot and "
    "children are safe, but the held plan PR #{number} could not be "
    "superseded -- so no child was activated while a pull request carrying "
    "the superseded work is still open. The next tick retries the same "
    "supersession, which posts nothing twice."
)


def _run_late_split(
    context: _LateContext, finished: _LateAdjudicationRun,
) -> _LateAdjudicationRun:
    """Run the whole split transaction for one guarded verdict.

    Entered only with a split the post-agent owner read cleared, so nothing
    here re-asks whether the issue is still open: that guarantee is what the
    guarded handoff carries, and re-deriving it would read a snapshot as old
    as the run that produced the verdict.
    """
    guarded = finished.guarded_split
    context.generation = guarded.generation
    refusal = _refused_split(context)
    if refusal is not None:
        _parked(
            context,
            refusal,
            LateFailure.CHILD_CREATE_FAILED,
            _late_outcome.PARK_CHILDREN_FAILED,
        )
        return _late_outcome._finished(context, _LateDisposition.PARKED)
    snapshot_ref = _late_snapshot._snapshot_for_split(context)
    if snapshot_ref is None:
        return _late_outcome._finished(context, _LateDisposition.PARKED)
    plan = _late_children._create_late_children(
        context, guarded.children, snapshot_ref,
    )
    if plan is None:
        return _late_outcome._finished(context, _LateDisposition.PARKED)
    _announced(context, plan, snapshot_ref)
    if not _superseded(context, plan, snapshot_ref):
        return _late_outcome._finished(context, _LateDisposition.PARKED)
    # Resolved once, ahead of the write that clears `pr_number`: the resolver
    # falls back to the legacy ref while a pull request is recorded, so a
    # second reading after that write could name a different branch from the
    # one this transaction just recorded as owed.
    branch = _worktree_paths._resolve_branch_name(
        context.state, context.spec, context.issue.number,
    )
    _handed_to_children(context, plan, branch)
    _reclaimed_branch(context, branch)
    return replace(
        finished,
        disposition=_LateDisposition.SETTLED,
        generation=context.generation,
    )


def _refused_split(context: _LateContext) -> Optional[str]:
    """Why this split may not run at all, or None when it may.

    Two refusals, and both are about state no step below could repair. A
    lineage at the bound is checked again here even though the verdict was
    already converted to a question where it was read: this is the transaction
    that creates a generation, so the cap is enforced where the children would
    be born as well as where the reply is parsed.

    An opaque ledger is the other. A split records a snapshot and one consumer
    per child on ledgers whose unreadable entries are written back verbatim, so
    an update merged into the typed view would vanish at the next write --
    taking with it either the ref nobody would then reclaim or the consumer the
    reclamation would stop waiting for.
    """
    if not context.generation.may_split:
        return _AT_BOUND_PARK
    if context.generation.has_opaque_ledger:
        return _OPAQUE_LEDGER_PARK
    return None


def _announced(
    context: _LateContext, plan: _SplitPlan, snapshot_ref: str,
) -> None:
    """Say on the parent what it became, and stamp that it was said.

    Gated on the stamp rather than on the phase, because the phase is rewritten
    by the owner-read claim every retry passes through and would not survive to
    be read here. The comment goes out ahead of the stamp, so the window a
    crash can land in costs the write rather than the sentence -- and the next
    tick, finding no stamp, repeats one comment instead of leaving an umbrella
    that never said where its work went.
    """
    if context.state.get(_DECOMPOSED_AT) is not None:
        return
    _comments._post_issue_comment(
        context.gh,
        context.issue,
        context.state,
        _FORWARD_LINKS.format(
            sha=context.generation.candidate_sha,
            count=len(plan.created),
            children=_child_lines(plan),
            ref=snapshot_ref,
        ),
    )
    context.state.set(_DECOMPOSED_AT, _usage._now_iso())
    context.generation = replace(
        context.generation, phase=LatePhase.SUPERSEDING,
    )
    _late_outcome._persist(context)


def _superseded(
    context: _LateContext, plan: _SplitPlan, snapshot_ref: str,
) -> bool:
    """Close the held plan PR over a notice that links forward, or park.

    Only the pull request this generation actually HELD. `pr_number` is
    whichever one the issue currently records and may name an implementation
    somebody else opened; the hold's own record names the one this cycle
    marked, and superseding anything else would close a change nobody
    adjudicated.

    The hold comes off first, so a pull request that ends up closed does not
    also end up wearing a "do not merge" notice forever. A release that failed
    on a still-open pull request parks on its own, which is what stops this
    from closing a change whose description is not back where it belongs.
    """
    number = context.generation.plan_pr_number
    if number is None:
        return True
    if _reconciled_already(context, LateResourceKind.PLAN_PR, str(number)):
        return True
    release = _late_hold._release_plan_pr_hold(
        context.gh, context.issue, context.generation,
    )
    context.generation = release.generation
    settled = not release.failed and _closed_over_notice(
        context, number, _SUPERSESSION_NOTICE.format(
            parent=context.issue.number,
            count=len(plan.created),
            ref=snapshot_ref,
            sha=context.generation.candidate_sha,
            children=_child_lines(plan),
        ),
    )
    if not settled:
        return _unsuperseded(context, number)
    _recorded_resource(
        context,
        LateResourceKind.PLAN_PR,
        str(number),
        LateResourceState.RECONCILED,
    )
    return True


def _closed_over_notice(
    context: _LateContext, number: int, notice: str,
) -> bool:
    """Fetch the held pull request and hand it its supersession.

    The fetch is guarded here rather than left to the helper, because a
    PyGithub pull request is lazy and the request that can fail is as likely
    to be this one as the write behind it -- and by the time this runs the
    children are already live, so an exception would strand them behind a
    traceback instead of behind a retry.
    """
    try:
        held = context.gh.get_pr(number)
    except Exception:
        log.exception(
            "issue=#%d could not read plan PR #%d to supersede it",
            context.issue.number, number,
        )
        return False
    return context.gh.supersede_pr(
        held, notice=notice, marker=SUPERSESSION_MARKER,
    )


def _handed_to_children(
    context: _LateContext, plan: _SplitPlan, branch: str,
) -> None:
    """Retire the generation onto `umbrella`, then let the children run.

    One write for the label and the retirement, because the two are the same
    statement: this issue has no candidate of its own any more. The branch it
    still owes the remote is recorded in that write as well, so the obligation
    is durable before the cleanup that reconciles it is attempted -- and the
    activation that follows can therefore never be waiting on it.

    Activation is last and is best-effort, exactly as the initial split's is: a
    child with no recorded dependencies reads as deps-satisfied on the
    umbrella's own next walk, which is the retry.
    """
    context.generation = _settled_generation(context.generation, branch)
    # The pull request this issue recorded is closed and carries superseded
    # work. Left in place it would point every later reader -- and the merged-PR
    # terminal above all -- at a change the umbrella's children are replacing.
    context.state.set(_PR_NUMBER, None)
    context.gh.set_workflow_label(context.issue, WorkflowLabel.UMBRELLA)
    _late_outcome._persist(context)
    _split._activate_initial_split_children(context.gh, context.issue, plan)


def _reclaimed_branch(context: _LateContext, branch: str) -> None:
    """Delete the superseded branch, and record whether it is gone.

    After activation on purpose: the branch is tidiness with a deadline rather
    than a precondition, and children held back until a remote delete succeeded
    would be work stalled on housekeeping. What it does gate is the umbrella's
    own terminal completion, which is why a failure is written down as `failed`
    rather than logged and forgotten.

    The local checkout goes with it, and it is safe here for one reason: the
    snapshot was created and proved before any of this, so the commit the
    worktree holds is no longer the only copy. A worktree left on a superseded
    branch is not merely untidy -- the per-tick base refresh treats it as a
    pre-PR checkout and accretes merges onto a branch nobody will publish.
    """
    try:
        deleted = context.gh.delete_remote_branch(branch)
    except Exception:
        log.exception(
            "issue=#%d superseded branch %r delete raised",
            context.issue.number, branch,
        )
        deleted = False
    _worktree_cleanup._remove_issue_worktree(context.spec, context.issue.number)
    _worktree_cleanup._delete_local_issue_branch(
        context.spec, context.issue.number, branch,
    )
    if not deleted:
        _late_outcome._emit_failure(context, LateFailure.BRANCH_CLEANUP_FAILED)
    _recorded_resource(
        context,
        LateResourceKind.BRANCH,
        branch,
        LateResourceState.RECONCILED if deleted else LateResourceState.FAILED,
    )
    _emit_cleanup(context, branch, deleted)


def _settled_generation(
    generation: LateGeneration, branch: str,
) -> LateGeneration:
    """What is left of a generation whose candidate became children.

    The measurement is what goes. A parent that has become an umbrella has no
    candidate to measure -- the work is its children's now -- and keeping the
    reading would leave the record answering "oversized", which is the one
    thing that pins `workflow:decomposing` and would put the umbrella label
    back on every tick.

    Everything a later reader still needs stays. The identity is what a
    cleanup record is correlated by, the commits are what the snapshot
    preserves, and both ledgers are what the remote is still owed -- including
    the branch this write is recording as owed for the first time.
    """
    owed = generation.with_resource(LateResource(
        kind=LateResourceKind.BRANCH,
        target=branch,
        resource_state=LateResourceState.PENDING,
    ))
    return LateGeneration(
        cycle_id=owed.cycle_id,
        generation=owed.generation,
        root_issue=owed.root_issue,
        current_issue=owed.current_issue,
        lineage_depth=owed.lineage_depth,
        scope=owed.scope,
        candidate_sha=owed.candidate_sha,
        base_sha=owed.base_sha,
        phase=LatePhase.CLEANING_UP,
        resources=owed.resources,
        consumers=owed.consumers,
    )


def _recorded_resource(
    context: _LateContext,
    kind: LateResourceKind,
    target: str,
    resource_state: LateResourceState,
) -> None:
    """Move one obligation to the state this step left it in, durably.

    A ledger update this binary cannot apply is logged and stepped over rather
    than raised: by the time most of these run the children are already live,
    and taking the tick out over a bookkeeping entry would strand them behind
    an exception instead of behind a retry.
    """
    try:
        context.generation = context.generation.with_resource(LateResource(
            kind=kind, target=target, resource_state=resource_state,
        ))
    except _formats.InvalidLateValue:
        log.exception(
            "issue=#%d could not record the %s obligation %r",
            context.issue.number, kind, target,
        )
        return
    _late_outcome._persist(context)


def _reconciled_already(
    context: _LateContext, kind: LateResourceKind, target: str,
) -> bool:
    """Whether this obligation is one an earlier attempt already settled."""
    return any(
        entry.kind == kind
        and entry.target == target
        and entry.resource_state == LateResourceState.RECONCILED
        for entry in context.generation.resources
    )


def _unsuperseded(context: _LateContext, number: int) -> bool:
    """Park with the children durable and none of them activated."""
    _recorded_resource(
        context,
        LateResourceKind.PLAN_PR,
        str(number),
        LateResourceState.FAILED,
    )
    _parked(
        context,
        _SUPERSESSION_FAILED_PARK.format(number=number),
        LateFailure.SUPERSESSION_FAILED,
        _late_outcome.PARK_SUPERSESSION_FAILED,
    )
    return False


def _emit_cleanup(
    context: _LateContext, branch: str, deleted: bool,
) -> None:
    """Report what happened to the superseded branch, on both sinks."""
    _telemetry.emit_late_event(
        context.gh,
        _events.LateEvent(
            family=_events.LateEventFamily.CLEANUP,
            resource=LateResource(
                kind=LateResourceKind.BRANCH,
                target=branch,
                resource_state=(
                    LateResourceState.RECONCILED if deleted
                    else LateResourceState.FAILED
                ),
            ),
        ),
        context.generation,
        stage=_DECOMPOSING_STAGE,
    )


def _child_lines(plan: _SplitPlan) -> str:
    """The forward links one split owes every reader of it."""
    return "\n".join(
        f"- #{number}: {child['title']}" for number, child in plan.created
    )


def _parked(
    context: _LateContext, message: str, failure: LateFailure, reason: str,
) -> None:
    """Hand the issue back with the recorded verdict and ledgers standing."""
    _late_outcome._emit_failure(context, failure)
    _late_outcome._park(context, message, reason=reason)
