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

from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github import comments as _github_comments
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.late_split import events as _events
from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import lineage as _lineage
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
from orchestrator.workflow.stages.decomposition import (
    late_cleanup as _late_cleanup,
)
from orchestrator.workflow.stages.decomposition import late_hold as _late_hold
from orchestrator.workflow.stages.decomposition import (
    late_outcome as _late_outcome,
)
from orchestrator.workflow.stages.decomposition import (
    late_snapshot as _late_snapshot,
)
from orchestrator.workflow.stages.decomposition import (
    activation as _activation,
)
from orchestrator.workflow.stages.decomposition import parents as _parents
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

# Stamped on the two comments this transaction owes, so a retry recognizes one
# it posted even when the write that was supposed to record it never landed.
# Both are scoped to the exact adjudication: a plan pull request outlives a
# cycle and an issue thread outlives everything, so an unscoped marker would
# read an earlier episode's receipt as this one's. HTML comments, so neither is
# visible in the rendered thread.
_SUPERSESSION_MARKER = (
    "<!--orchestrator-late-supersession:issue={issue}"
    ":cycle={cycle}:generation={generation}-->"
)

_FORWARD_LINK_MARKER = (
    "<!--orchestrator-late-split:cycle={cycle}:generation={generation}-->"
)

_FORWARD_LINKS = (
    ":scissors: the late decomposer read the committed candidate `{sha}` as "
    "{count} separable changes, so this issue becomes an umbrella and the "
    "work is handed to its children:\n\n{children}\n\nThe committed work is "
    "preserved on the immutable ref `{ref}` at `{sha}`; each child reuses the "
    "part of it their own scope covers. This issue has no implementation of "
    "its own and closes once every child resolves.\n\n{marker}"
)

_SUPERSESSION_NOTICE = (
    ":scissors: **Superseded.** The committed implementation for issue "
    "#{parent} was adjudicated as {count} separable changes, so this pull "
    "request is closed without merging and issue #{parent} is now an "
    "umbrella.\n\nThe work it carried is preserved on the immutable ref "
    "`{ref}` at `{sha}` -- nothing is lost, and each child reuses the part of "
    "it their scope covers:\n\n{children}\n\n{marker}"
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

_CONTRADICTED_PARK = (
    "the committed candidate for this issue was adjudicated as a split, but "
    "this issue's recorded lineage does not agree with the generation that "
    "was adjudicated: {reason}. Nothing was created. The generation was "
    "minted without the ancestry this issue was created under, and acting on "
    "it would let the lineage buy itself a generation past the bound -- so "
    "the two have to be reconciled by hand."
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

    Three refusals, and each is about state no step below could repair. A
    lineage at the bound is checked again here even though the verdict was
    already converted to a question where it was read: this is the transaction
    that creates a generation, so the cap is enforced where the children would
    be born as well as where the reply is parsed.

    An ancestry that disagrees with the generation is the second, and it is
    the same cap read from the other side. A child born of an earlier split
    carries the lineage it was created under; its own generation is minted
    from that record, so a generation naming a different root or a shallower
    depth is one minted without it -- and a shallower depth is exactly how a
    lineage buys itself another generation past the bound.

    An opaque ledger is the third. A split records a snapshot and one consumer
    per child on ledgers whose unreadable entries are written back verbatim, so
    an update merged into the typed view would vanish at the next write --
    taking with it either the ref nobody would then reclaim or the consumer the
    reclamation would stop waiting for.
    """
    if not context.generation.may_split:
        return _AT_BOUND_PARK
    if context.generation.has_opaque_ledger:
        return _OPAQUE_LEDGER_PARK
    contradicted = _lineage.contradicted_lineage(
        context.state, context.generation,
    )
    if contradicted is not None:
        return _CONTRADICTED_PARK.format(reason=contradicted)
    return None


def _announced(
    context: _LateContext, plan: _SplitPlan, snapshot_ref: str,
) -> None:
    """Say on the parent what it became, exactly once.

    Two gates, because neither answers the whole question on its own. The
    generation's own `links_announced` flag is the cheap one and the one that
    holds on the ordinary retry -- it is scoped to this adjudication, unlike
    `decomposed_at`, which an EARLIER decomposition of the same issue already
    wrote and which would therefore suppress this announcement entirely. The
    thread is the expensive one and the one that covers the window the flag
    cannot: a comment that landed and a process that died before the write is
    indistinguishable from the outside, so the marker this generation stamps
    into its own sentence is looked for among the comments before another is
    posted. It is asked only when the flag is unset, so a resume past the
    announcement costs nothing.

    `decomposed_at` is written all the same, because it is what the stage's
    own readers date a decomposition by; it is simply not this step's receipt.
    """
    if context.generation.links_announced:
        return
    if not _links_on_thread(context):
        _comments._post_issue_comment(
            context.gh,
            context.issue,
            context.state,
            _FORWARD_LINKS.format(
                sha=context.generation.candidate_sha,
                count=len(plan.created),
                children=_child_lines(plan),
                ref=snapshot_ref,
                marker=_forward_marker(context.generation),
            ),
        )
    context.state.set(_DECOMPOSED_AT, _usage._now_iso())
    context.generation = replace(
        context.generation,
        phase=LatePhase.SUPERSEDING,
        links_announced=True,
    )
    _late_outcome._persist(context)


def _links_on_thread(context: _LateContext) -> bool:
    """Whether this generation's own forward links are already said.

    Walked whole rather than from a watermark: the post moves every watermark
    this mode keeps past itself, so a scan bounded by one would start above
    the very comment it is looking for.
    """
    return _github_comments.carries_own_marker(
        context.gh.comments_after(context.issue, None),
        _forward_marker(context.generation),
        bot_login=getattr(context.gh, "_bot_login", None),
    )


def _forward_marker(generation: LateGeneration) -> str:
    """The receipt this generation's forward-link comment carries."""
    return _FORWARD_LINK_MARKER.format(
        cycle=generation.cycle_id, generation=generation.generation,
    )


def _supersession_marker(context: _LateContext) -> str:
    """The receipt this generation's supersession notice carries."""
    return _SUPERSESSION_MARKER.format(
        issue=context.issue.number,
        cycle=context.generation.cycle_id,
        generation=context.generation.generation,
    )


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

    Run on every pass, including one where the ledger already reads
    `reconciled`. That entry records what an EARLIER pass did, and a pull
    request is not a thing that stays where it was put: a human who reopens it
    between that write and the resume would otherwise have the resume skip
    straight past, report settled, and let the children loose beside a change
    still carrying the superseded work. Re-asking costs one fetch and one
    comment listing, and neither step repeats anything -- the notice is gated
    on this generation's own marker already on the thread, and a pull request
    that is not open is left exactly as it is.
    """
    number = context.generation.plan_pr_number
    if number is None:
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
            marker=_supersession_marker(context),
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
        held, notice=notice, marker=_supersession_marker(context),
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

    Activation is last and is best-effort: a child this pass could not flip
    reads as deps-satisfied on the umbrella's own next walk, which is the
    retry. It runs through that same walk rather than the initial split's
    one-shot flip, because by the time it runs a child's state is no longer
    this transaction's to assume. The supersession above can park for as long
    as a human takes to settle a pull request, and a child that reached
    `rejected` or `done` in that window would be flipped back to `ready` by a
    write that reads nothing -- the transition guard only warns by default, so
    nothing else would stop it. The walk reads each child fresh and moves only
    the ones still `blocked` with their recorded dependencies satisfied.
    """
    context.generation = _settled_generation(context.generation, branch)
    # The pull request this issue recorded is closed and carries superseded
    # work. Left in place it would point every later reader -- and the merged-PR
    # terminal above all -- at a change the umbrella's children are replacing.
    context.state.set(_PR_NUMBER, None)
    context.gh.set_workflow_label(context.issue, WorkflowLabel.UMBRELLA)
    _late_outcome._persist(context)
    _activated(context, plan)


def _activated(context: _LateContext, plan: _SplitPlan) -> None:
    """Let the children this split may still start, run.

    A read that failed leaves every child where it is. The umbrella's own walk
    takes the same reading on its next tick, so nothing is lost by declining
    to guess -- while flipping a child whose state could not be established is
    the write this exists to avoid.
    """
    scan = _parents._read_child_labels(
        context.gh, context.issue, [number for number, _ in plan.created],
    )
    if scan is None:
        log.warning(
            "issue=#%d could not read its children to activate them; the "
            "umbrella's own walk retries on the next tick",
            context.issue.number,
        )
        return
    _activation._activate_ready_children(
        context.gh, context.issue, context.state, scan,
    )


def _reclaimed_branch(context: _LateContext, branch: str) -> None:
    """Take the first swing at the superseded branch, and record the answer.

    After activation on purpose: the branch is tidiness with a deadline rather
    than a precondition, and children held back until a remote delete succeeded
    would be work stalled on housekeeping. What it does gate is the umbrella's
    own terminal completion -- which is why a failure is written down rather
    than logged and forgotten, and why the retry lives on the umbrella
    (`late_cleanup`) rather than here: an issue this transaction has finished
    with is one nothing brings back to this owner.

    The local checkout goes with it -- the reclamation takes every surface the
    branch exists on -- and it is safe here for one reason: the snapshot was
    created and proved before any of this, so the commit the worktree holds is
    no longer the only copy. A worktree left on a superseded branch is not
    merely untidy: the per-tick base refresh treats it as a pre-PR checkout and
    accretes merges onto a branch nobody will publish.
    """
    context.generation = _late_cleanup._reclaim_branch(
        context.gh,
        context.spec,
        context.issue.number,
        context.generation,
        branch,
    )
    deleted = branch not in _late_cleanup._owed_branches(context.generation)
    if not deleted:
        _late_outcome._emit_failure(context, LateFailure.BRANCH_CLEANUP_FAILED)
    _late_outcome._persist(context)
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
    the branch this write is recording as owed for the first time. The ordered
    child register stays with them: it is what says which child owns which
    slice of the manifest, and a transaction re-entered against a retired
    generation has to adopt them rather than open a second set.
    """
    owed = _late_cleanup._record_branch_obligation(generation, branch)
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
        split_children=owed.split_children,
        links_announced=owed.links_announced,
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
