# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a finished late run becomes, and the order it becomes it in.

The half of the late mode that reads a reply and settles what it decided,
split from the coordinator that produces one the way `outcomes.py` is split
from `run.py` beside it. The parks every late exit hands the issue back
through live here too, because the ordering rule they all obey is this
owner's: the durable write goes out before the external effect, never after.

That rule is what a completed adjudication is worth. The agent has already
been paid for by the time a reply is read, so a crash between reading it and
recording it costs a second run of an agent that already answered. The result
is therefore written and persisted BEFORE anything is posted, and the
announcement a question owes the issue is reconciled from that record on a
later tick rather than being the only place the outcome exists. What the
narrow crash window between the post and the write can still cost is one
repeated comment -- the same window every park in this repository has -- and
never the run.

`_announce` is published for the same reason it is not called from the two
places that record an outcome: the owner guard runs between the record and
anything said out loud, so what posts a question is the step past that guard
rather than the step that wrote it down.

That order has one cost, and paying it is the other thing the parks here do.
A flag written before its comment is a flag that reads as delivered when the
comment fails, and every later tick would take the human as told -- so what
the park still owes the thread is recorded beside the flag on the same write
and dropped only by the post that discharges it. The field is the `late_notice`
leaf's; the three readings of it are this owner's, because they are readings
of the ordering rule rather than of the field: whether a park is a repeat,
whether a stranded sentence is one nothing else would ever say, and whether
the tick that found it is the one that should say it.

The lineage bound is enforced here rather than in the parser, because it is a
property of the generation and not of the reply. A structurally valid split
proposed at the bound is recorded as the categorized question it actually is:
the workflow is asking a human, the recorded outcome says so, and the next
tick does not pay for another agent to propose the same forbidden split.

The three emissions sit here for the same reason the parks do. A verdict, a
typed failure, and the cancellation an owner read earns are each written
straight after the state they describe, and keeping them beside the writes is
what stops one of them reporting a step whose durable half never landed.

What this owner deliberately does NOT do is publish. It records a verdict
and returns; announcing a question, restoring or superseding the held plan PR,
creating children, and pushing an accepted candidate all belong to the steps
that act on the verdict.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.late_split import events as _events
from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.late_split import telemetry as _telemetry
from orchestrator.workflow.late_split.models import (
    LateFailure,
    LatePhase,
    LateVerdict,
)
from orchestrator.workflow.stages.decomposition import (
    late_notice as _late_notice,
)
from orchestrator.workflow.stages.decomposition import (
    late_reply as _late_reply,
)
from orchestrator.workflow.stages.decomposition import (
    late_session as _late_session,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateAdjudication,
    _LateAdjudicationRun,
    _LateContext,
    _LateDisposition,
    _LateRun,
    _StagedPark,
)

log = logging.getLogger("orchestrator.workflow")

_DECOMPOSING_STAGE = "decomposing"

_AWAITING_HUMAN = "awaiting_human"

_PARK_REASON = "park_reason"

# The issue-wide record of what the workflow has already acted on. Shared with
# every other stage, which is why this mode has to keep it moving: a reply this
# mode read and acted on is one the later validating -> in_review handoff must
# not find again as fresh PR feedback.
_LAST_ACTION_COMMENT_ID = "last_action_comment_id"

# Every way this mode hands an issue back, spelled once because each is a
# durable pinned value and because the set below is read against them.
PARK_HOLD_FAILED = "late_plan_pr_hold_failed"
PARK_INCOMPLETE = "late_generation_incomplete"
PARK_WORKTREE_MISSING = "late_worktree_missing"
PARK_WORKTREE_MUTATED = "late_worktree_mutated"
PARK_TIMEOUT = "late_adjudicator_timeout"
PARK_UNPARSED = "late_manifest_invalid"
PARK_UNRECORDABLE = "late_result_unrecordable"
PARK_OWNER_UNREADABLE = "late_owner_unreadable"
PARK_SNAPSHOT_FAILED = "late_snapshot_failed"
PARK_CHILDREN_FAILED = "late_children_failed"
PARK_SUPERSESSION_FAILED = "late_supersession_failed"
PARK_PR_UNRECONCILED = "late_pr_unreconciled"
PARK_QUESTION = "late_question"
PARK_CONTENT_DRIFT = "late_content_drift"
PARK_REVISION_DIRTY = "late_revision_dirty"
PARK_REVISION_UNMEASURED = "late_revision_unmeasured"
PARK_REVISION_UNANSWERED = "late_revision_unanswered"

# The parks a fresh attempt answers, and therefore retires before it runs. A
# hold that failed has now been reconciled, a worktree that was gone is back, a
# run that timed out or answered unusably is about to be re-run, and a pull
# request lookup nobody could take is about to be taken again, and each of the
# three transaction steps -- the snapshot, the children, the supersession -- is
# about to be reconciled again from the same recorded verdict, at no agent's
# cost. The six left out are the ones no retry answers. `PARK_QUESTION` is the announcement
# itself, and the four content
# parks are the workflow waiting to be told what an edited scope, a worktree
# the developer left changed, a candidate nobody could measure, or a developer
# that changed nothing and vouched for nothing now means. Retiring one of those
# would drop the very state the next tick reads to tell a human's answer from
# the silence before it.
#
# `PARK_OWNER_UNREADABLE` is left out for a different reason: it IS answered by
# a retry, but by one that runs before any of this -- the pending owner check
# the generation records, which is what brings a tick back to the read at all.
# That reconciliation reads the standing reason to decide whether it owes the
# thread a follow-up, so retiring the park here would erase the only durable
# evidence that this mode had said anything to retire.
_SUPERSEDED_PARKS = frozenset((
    PARK_HOLD_FAILED,
    PARK_INCOMPLETE,
    PARK_WORKTREE_MISSING,
    PARK_WORKTREE_MUTATED,
    PARK_TIMEOUT,
    PARK_UNPARSED,
    PARK_UNRECORDABLE,
    PARK_PR_UNRECONCILED,
    PARK_SNAPSHOT_FAILED,
    PARK_CHILDREN_FAILED,
    PARK_SUPERSESSION_FAILED,
))

_UNPARSED_PARK = (
    "the late decomposer did not return a usable "
    "`orchestrator-late-manifest` block ({reason}), so nothing was decided "
    "about this issue's oversized committed candidate."
)

_QUESTION_PARK = ":mag: the late decomposer is asking ({category}): {asked}"

_UNRECORDABLE_PARK = (
    "the late decomposer decided something this issue's pinned state cannot "
    "hold -- a question or a child manifest past the size one orchestrator "
    "comment may carry. Nothing was recorded and nothing was published, "
    "because half an outcome is not one. This oversized candidate needs a "
    "human to split it by hand."
)

# What a split proposed at the lineage bound is recorded as. The bound is a
# safety invariant, so the outcome is not the split the agent asked for; it is
# the categorized question the workflow now owes a human, recorded as one so a
# later tick asks the human rather than the agent.
_AT_BOUND_QUESTION = _LateAdjudication(
    verdict=LateVerdict.QUESTION,
    category=_events.LateVerdictCategory.LINEAGE_BOUND,
    question=(
        "the late decomposer proposed splitting this issue, but its lineage "
        "is already as deep as automatic splitting goes. It has to land as "
        "one change or be split by hand."
    ),
)


def _decide(
    context: _LateContext, last_message: str,
) -> _LateAdjudicationRun:
    """Read the reply, refuse a split the lineage forbids, and record it."""
    adjudication, parse_error = _late_reply._parse_late_reply(last_message)
    if adjudication is None:
        _stage_park(
            context,
            _UNPARSED_PARK.format(reason=parse_error),
            reason=PARK_UNPARSED,
        )
        _completed(context)
        return _finished(context, _LateDisposition.PARKED)
    if (
        adjudication.verdict == LateVerdict.SPLIT
        and not context.generation.may_split
    ):
        adjudication = _AT_BOUND_QUESTION
    return _recorded(context, adjudication)


def _recorded(
    context: _LateContext, adjudication: _LateAdjudication,
) -> _LateAdjudicationRun:
    """Persist one completed adjudication, then say what it decided.

    The persist is first and unconditional. Everything after it -- the two
    sinks, and the comment a question owes the issue -- is an external effect
    that a crash may repeat, and repeating one of those costs a duplicate
    record or a duplicate comment. Repeating what comes before it would cost
    another agent run against a candidate that has already been adjudicated,
    and a second run is free to decide differently.

    An outcome the record could not hold is the one case that never becomes
    an answer at all: nothing durable stands behind it, so acting on it would
    leave the issue decided in a way no later tick could see. It parks
    instead, and the park is staged BEFORE the write rather than after it, so
    the one write carries whichever of the two this run produced.

    What it deliberately does NOT do is announce. The announcement is an
    external effect on the issue, and whether the issue is still there is the
    owner guard's question -- which is asked between this write and anything
    said out loud, so a question is not posted to a thread somebody closed
    while the agent was answering it.
    """
    kept = _late_session._record_late_result(context.state, adjudication)
    if not kept:
        log.error(
            "issue=#%d the late outcome does not fit the pinned comment; "
            "refusing to record part of it",
            context.issue.number,
        )
        _stage_park(
            context, _UNRECORDABLE_PARK, reason=PARK_UNRECORDABLE,
        )
    _completed(context)
    _emit_verdict(context, adjudication)
    if not kept:
        return _finished(context, _LateDisposition.PARKED)
    return _LateAdjudicationRun(
        disposition=_LateDisposition.DECIDED,
        generation=context.generation,
        run=_late_session._read_late_run(context.state),
        adjudication=adjudication,
    )


def _announce(
    context: _LateContext, adjudication: _LateAdjudication,
) -> None:
    """Post the question this outcome owes the issue, if it owes one.

    Called past the owner guard rather than beside the record, so a question
    is never posted to a thread this tick could not prove is still open. The
    park it goes through commits everything staged with it, so a caller has
    nothing left to write afterwards.

    Read off the adjudication rather than off the record, so what the issue
    is told is what the agent actually wrote. The two agree -- an outcome is
    refused rather than shortened -- but the announcement is not the record's
    to paraphrase.

    A verdict that asks nothing announces nothing, and a question the issue is
    already waiting on a human for is not repeated -- which is what a recorded
    question reaching this a second time relies on.
    """
    if not adjudication.question or context.state.get(_AWAITING_HUMAN):
        return
    _park(
        context,
        _QUESTION_PARK.format(
            category=adjudication.category, asked=adjudication.question,
        ),
        reason=PARK_QUESTION,
    )


def _reused(
    context: _LateContext, run: _LateRun, *, retired: bool,
) -> _LateAdjudicationRun:
    """Report an answer this tick did not have to earn.

    The announcement a recorded question still owes the issue is not made
    here: it is made past the owner guard, from the question the record kept,
    which is what lets an outcome recorded and never said be said by a later
    tick rather than by another agent run.

    What IS owed here is the write. This is the one branch that returns
    without doing anything else, so a park retired into memory and not
    persisted is a park still standing on the issue -- durably claiming a
    human is owed something, on an issue whose answer is already recorded.
    """
    if retired:
        _persist(context)
    return _LateAdjudicationRun(
        disposition=_LateDisposition.DECIDED,
        generation=context.generation,
        run=_late_session._read_late_run(context.state),
        adjudication=_late_session._recovered_adjudication(run),
    )


def _parked_run(
    context: _LateContext,
    agent_result: AgentResult,
    message: str,
    *,
    reason: str,
) -> _LateAdjudicationRun:
    """Pin the session this run opened, then hand the issue back.

    Both parks that follow a finished run come through here, so the session a
    later resume has to land on is recorded at every exit that writes and at
    no exit that does not -- what the returned record claims is what the
    pinned comment holds.

    Staged rather than said, because the run this parks has already been paid
    for: the session and the park are made durable by the write below, and the
    notice waits for a read that proves the issue is still there.

    That write is this owner's own and not the guard's, which is the whole
    difference between a completion nobody has to pay for twice and one that
    can be lost. A timeout and a contaminated worktree are as finished as a
    verdict is -- the agent ran, the issue paid for it, and what it left is
    exactly as unrepeatable -- so what they decided goes down here, before
    anything that could fail to come back.
    """
    _late_session._record_late_session(context.state, agent_result)
    _stage_park(context, message, reason=reason)
    _completed(context)
    return _finished(context, _LateDisposition.PARKED)


def _emit_verdict(
    context: _LateContext, adjudication: _LateAdjudication,
) -> None:
    """Report one adjudication on both sinks, or lose the record instead.

    The event contract is checked where the event is built, which is here
    rather than inside the emission, so the refusal it raises is caught here
    too: a record nobody should have written and a tick broken by the attempt
    to write it are both failures, and only the first one is recoverable.
    """
    try:
        decided = _events.LateEvent(
            family=_events.LateEventFamily.VERDICT,
            verdict=adjudication.verdict,
            category=adjudication.category,
            child_count=adjudication.child_count,
        )
    except _formats.InvalidLateValue as refused:
        log.error(
            "issue=#%d late verdict refused as an event (%s); nothing "
            "emitted", context.issue.number, refused,
        )
        return
    _telemetry.emit_late_event(
        context.gh, decided, context.generation, stage=_DECOMPOSING_STAGE,
    )


def _emit_failure(context: _LateContext, failure: LateFailure) -> None:
    """Report one typed late failure on both sinks."""
    _telemetry.emit_late_event(
        context.gh,
        _events.LateEvent(
            family=_events.LateEventFamily.FAILURE, failure=failure,
        ),
        context.generation,
        stage=_DECOMPOSING_STAGE,
    )


def _emit_cancellation(context: _LateContext) -> None:
    """Report that this generation's owner was observed gone.

    Emitted after the cancellation is durable, like every other record here,
    so what a sink carries is a mark the cleanup can already read rather than
    a claim about a write that may not have landed. The family says everything
    on its own -- who was cancelled is the generation's own correlation -- so
    it carries no detail of its own.
    """
    _telemetry.emit_late_event(
        context.gh,
        _events.LateEvent(family=_events.LateEventFamily.CANCELLATION),
        context.generation,
        stage=_DECOMPOSING_STAGE,
    )


def _park(context: _LateContext, message: str, *, reason: str) -> None:
    """Hand the issue back to a human now, and commit everything staged.

    The two halves below run back to back, which is what every exit taken
    BEFORE a run reaches for: nothing has been paid for yet, so there is no
    result a refused comment could take down with it and no owner read between
    the write and the notice.
    """
    _stage_park(context, message, reason=reason)
    _persist(context)
    _release_staged_park(context)


def _stage_park(context: _LateContext, message: str, *, reason: str) -> None:
    """Record the park in memory and hold its notice for the caller.

    The reason is written durably beside the flag, which the shared park
    deliberately clears: without it, an issue parked here is one nothing can
    tell from an issue parked by any other stage, and the next late attempt
    could neither retire its own park nor leave somebody else's alone.

    A park already standing for this same reason is not announced again. Every
    late failure is reconciled on each eligible tick -- that is what makes the
    retries idempotent -- so an unchanged one would otherwise say the same
    sentence to the same thread once a tick until a human arrived. The state
    is still written: what is suppressed is the notice, not the park -- which
    is why the suppression is decided HERE, before the flags it reads are
    overwritten.

    Nothing is said, and the sentence is recorded as owed rather than assumed
    delivered. It is staged beside the flag in the SAME memory the park is,
    so the write the park rides out on carries both -- and until a post
    actually lands, every reader of that flag can tell a human who has been
    told from one who has not.

    Every exit a COMPLETED run takes stages its park and lets the owner read
    that follows decide whether the notice may be posted at all, so the
    durable half rides whatever write comes next and no comment can be posted
    ahead of it.
    """
    repeated = _stands_already(context, reason)
    context.state.set(_AWAITING_HUMAN, True)
    context.state.set(_PARK_REASON, reason)
    if repeated:
        log.info(
            "issue=#%d is already parked as %s; not repeating the notice",
            context.issue.number, reason,
        )
        return
    context.staged_park = _StagedPark(message=message, reason=reason)
    _late_notice._owe_notice(context, context.staged_park)


def _release_staged_park(context: _LateContext) -> None:
    """Say what a park already recorded is for, if it still owes a sentence.

    Called once the owner has been read and came back open, so nothing is said
    to a thread whose issue this tick could not prove is still there. A park
    whose notice this drops is not lost: the park itself is durable, and
    whatever re-takes it announces the reason it fails for THEN, which is the
    current one rather than one an outage ago.

    The mention goes through the shared park so the watermark it ratchets is
    the one every other park in this repository ratchets -- that id is the
    response boundary a reply is measured against, and a notice that did not
    move it would let a comment written before it read as an answer to it.

    The obligation is dropped between the post and the write, which is the
    only order that fails the right way: a crash in that window leaves the
    sentence owed by a thread that already has it, so the next tick repeats
    one comment -- the same window every park in this repository has -- rather
    than dropping one nobody ever said.
    """
    staged = context.staged_park
    if staged is None:
        return
    context.staged_park = None
    _guards._park_awaiting_human(
        context.gh,
        context.issue,
        context.state,
        f"{config.HITL_MENTIONS} {staged.message}",
        reason=staged.reason,
    )
    context.state.set(_PARK_REASON, staged.reason)
    _late_notice._notice_settled(context)
    _persist(context)


def _stands_already(context: _LateContext, reason: str) -> bool:
    """Whether this issue is already parked for exactly this reason.

    Asked of what the tick FOUND, not only of what it has staged. A park this
    tick retired into memory and is now re-taking for the same reason is the
    same park -- the step it named failed again, nothing about the issue moved
    between them, and the human it mentioned has already been told.

    "Nothing moved between them" is what the memory really claims, which is why
    the run that could move something clears it. Past a spawn the reason is no
    longer enough to call two parks the same: an agent answered, and a second
    categorized question or a second unusable reply says something the first
    notice did not. Suppressing those would leave an outcome recorded, durable,
    and never announced -- so only the reconciliation retries that spawn
    nothing keep the memory that quiets them.

    A park somebody cleared is not standing, whatever reason it carried, so an
    issue a human un-parked is announced to again rather than silently
    re-parked.

    And a park whose sentence was never said is not one the human has been
    told about, whatever its flag claims. The flag goes down before the
    comment goes out, so a refused post leaves one standing over a thread that
    was told nothing -- and answering from the flag alone would call that a
    repeat and suppress every later attempt to say it. What makes a park a
    repeat is the sentence, so that is what is asked.
    """
    if context.retired_park == reason:
        return True
    if not _stands_for(context, reason):
        return False
    return _late_notice._owed_notice(context) is None


def _stands_for(context: _LateContext, reason: str) -> bool:
    """Whether this issue is parked, right now, for exactly this reason.

    The flag alone, with nothing said about whether anybody was told. Asked by
    the steps that RETIRE a park -- which is owed to a park either way -- as
    against the ones that decide whether to repeat its notice.
    """
    if not context.state.get(_AWAITING_HUMAN):
        return False
    return context.state.get(_PARK_REASON) == reason


def _release_unsuperseded_park(context: _LateContext) -> None:
    """Say what a staged park explains, if nothing else ever will.

    The counterpart to holding a notice back for a read that could not be
    taken. Holding one back is only ever a DEFERRAL, and it is a deferral
    exactly where a later attempt supersedes the park: that attempt re-takes
    it, and announces the reason it fails for then, which is the current one.

    A park no attempt supersedes has no such tick coming. It IS what the issue
    is waiting on, and its sentence is the only thing that will ever say what
    the human has to do -- so dropping it leaves an `awaiting_human` standing
    with nothing behind it, for as long as the read keeps failing, which is
    unbounded. A comment on a thread this tick could not prove is open costs
    less than that.

    Asked of what an earlier tick left owed as well as of what this one
    staged, because the two are the same obligation: a notice a refused
    comment stranded is exactly a sentence nothing else will ever say.
    """
    staged = context.staged_park or _late_notice._owed_notice(context)
    if staged is None or staged.reason in _SUPERSEDED_PARKS:
        return
    context.staged_park = staged
    _release_staged_park(context)


def _reconcile_notice_delivery(context: _LateContext) -> None:
    """Discharge an obligation the thread shows was already discharged.

    The first thing a tick asks, ahead of even the owed owner read, because
    everything that reads the obligation afterwards would read it wrong. The
    post and the write that records it are two operations, so a write that
    failed after a post that landed leaves pinned state claiming a sentence is
    owed to a thread that already has it -- and two different steps then draw
    two different wrong conclusions from it. The redelivery repeats a comment,
    which is cheap; the guard's own recovery reads it as proof that nobody was
    ever told and clears the park WITHOUT the follow-up it promised, which is
    a sentence nothing else will ever say.

    So both halves the failed write was carrying are put back: the obligation
    is dropped, and the consumed watermark is ratcheted to the comment that
    actually carried it -- the id a park's own mention is supposed to move it
    to, and the one the follow-up's own at-most-once check is scoped by.

    Nothing is said here and nothing is decided. A notice the thread does not
    carry is left exactly as it was, for the retry below to say.
    """
    owed = _late_notice._owed_notice(context)
    if owed is None:
        return
    delivered = _late_notice._delivered_id(context, owed.message)
    if delivered is None:
        return
    log.info(
        "issue=#%d already carries the notice for park %s; recording it as "
        "said rather than saying it twice",
        context.issue.number, owed.reason,
    )
    _late_notice._notice_settled(context)
    _mark_replies_read(context, delivered)
    _persist(context)


def _redeliver_park_notice(context: _LateContext) -> None:
    """Say what a standing park is for, if a refused comment never did.

    The retry the durable half of a park earns. It runs at the top of a tick,
    ahead of every gate a park routes past, because a park is exactly the
    state that stops a tick reaching anything: the drift park consumes
    nothing and returns, the stalled revision waits for a reply, and the
    recorded question is answered from the record -- so a sentence hung off
    any of them would never be said.

    The tick's own snapshot is what it is said on, which is the same standing
    every park taken BEFORE a run has: the issue was fetched seconds ago by
    the poll that routed it here, and nothing has been paid for since. A
    cancelled cycle is the one exception -- its parks explain a candidate
    nobody is adjudicating any more, and its issue is one somebody closed.

    A park a fresh attempt supersedes is left to that attempt, which runs
    just below this and either retires the park or re-takes it and says the
    reason it fails for now. Saying the old sentence first would announce a
    wall this tick is about to walk through.

    Idempotent by what it clears: the obligation is dropped by the post that
    discharges it, so a notice reaches the thread once per park rather than
    once per tick.
    """
    generation = context.generation
    if not generation.is_present or generation.cancelled:
        return
    owed = _late_notice._owed_notice(context)
    if owed is None or owed.reason in _SUPERSEDED_PARKS:
        return
    log.info(
        "issue=#%d is parked as %s with its notice unsaid; posting it now",
        context.issue.number, owed.reason,
    )
    context.staged_park = owed
    _release_staged_park(context)


def _stands_parked(context: _LateContext) -> bool:
    """Whether this issue is already stopped waiting on a human.

    Asked by the owner guard before it takes a park of its own. An issue that
    a timeout, an unusable reply, or a stalled revision has already handed
    back is one nobody is going to publish anyway, and replacing that reason
    with "the owner could not be read" would swap the thing the human is
    being asked about for one they cannot answer. What brings the next tick
    back to the read in that case is the generation's own pending marker, not
    the park.
    """
    return bool(context.state.get(_AWAITING_HUMAN))


def _retire_park(context: _LateContext) -> bool:
    """Clear a late park this attempt has already answered.

    A park is a claim that the issue is waiting on a human. Once the step that
    failed has been reconciled the claim is stale, and leaving it standing is
    not harmless: the announcement a question earns is suppressed by exactly
    this flag, so a hold that failed once would silence a categorized question
    -- decided, durable, and never said out loud.

    Which is why this runs the moment the hold reconciles rather than beside
    the spawn. The question being silenced need not be one this tick produced:
    a run whose result persisted and whose comment then failed leaves an
    announcement owing, and a hold that failed in between would bury it under
    a park that has nothing to do with it.

    Only this mode's own parks, and only the ones an attempt answers. A park
    another stage left is not this one's to retire, and the question park is
    not stale: nothing here has answered it.

    Returns whether anything was retired. The write belongs to the caller,
    as it does for every other state this mode stages, and the caller that
    stages nothing else has to know it now owes one. What was retired is kept
    on the tick, so a park re-taken for the same reason is recognized as the
    one already announced rather than announced again -- and a park whose
    notice was never said is deliberately NOT kept, because "already
    announced" is the one thing it is not.

    The obligation goes with the park. Retiring one whose sentence is still
    owed is what makes it moot: the step it named has been reconciled, so
    what the sentence describes is over.
    """
    standing = context.state.get(_PARK_REASON)
    if standing not in _SUPERSEDED_PARKS:
        return False
    if _late_notice._owed_notice(context) is None:
        context.retired_park = standing
    _late_notice._notice_settled(context)
    context.state.set(_AWAITING_HUMAN, False)
    context.state.set(_PARK_REASON, None)
    return True


def _mark_replies_read(context: _LateContext, through) -> None:
    """Record the trusted conversation this tick acted on as read, issue-wide.

    The late fingerprints are this mode's own bookkeeping; the watermark moved
    here is everybody's. A reply that resolved a park, certified a candidate,
    or reopened a question has been ACTED on, and leaving the shared watermark
    behind would let the validating -> in_review handoff read the same comment
    as fresh PR feedback -- routing the pull request to `fixing` over an answer
    this mode already spent, or resuming the developer on input it handled.

    `through` is the highest TRUSTED comment folded in, so an untrusted comment
    sitting above it stays unconsumed exactly as it does on every other resume:
    nothing an outsider posts is marked read on their behalf. A one-way ratchet,
    because a park notice or another stage may already have moved it further.
    """
    if not _formats.whole_number(through):
        return
    prior = context.state.get(_LAST_ACTION_COMMENT_ID)
    if not _formats.whole_number(prior) or through > prior:
        context.state.set(_LAST_ACTION_COMMENT_ID, through)


def _answer_park(context: _LateContext) -> None:
    """Clear the park a human has now answered.

    The counterpart to `_retire_park` for the parks no retry supersedes. Those
    stand until somebody says something, so what clears them is an answer
    rather than another attempt -- and the caller that took the answer is the
    only thing that knows one arrived.

    Deliberately NOT remembered on the tick the way a retirement is. That
    memory exists to recognize a park re-taken unchanged, and an answered park
    is never that: the human said something, something ran because they did,
    and whatever it parks on next is news even when it carries the same reason.
    A second question is a different question, and remembering the first would
    leave it recorded, durable, and never said out loud. The write belongs to
    the caller, as it does for every other state this mode stages.

    A sentence this park still owed is dropped with it. The human has spoken,
    which is more than being told what to speak about, and telling them now
    what the issue was waiting on would ask them for something they have
    already given.
    """
    _late_notice._notice_settled(context)
    context.state.set(_AWAITING_HUMAN, False)
    context.state.set(_PARK_REASON, None)


def _completed(context: _LateContext) -> None:
    """Write what a finished run left, and the read it now owes, as one thing.

    The last step of every completion and the first one that could survive it.
    A run that finished is not free to repeat -- the agent has been paid for,
    and a second one is free to decide differently -- so what it decided is
    durable before the tick does anything that might not come back. That is
    as true of a timeout, an unusable reply, an outcome too large to record,
    a contaminated worktree, and a reconciliation nobody could make as it is
    of a verdict: each is a completed run, and each leaves a park a later tick
    would otherwise neither find nor be able to rebuild.

    The owner read the completion now owes rides the very same write, and that
    is not a convenience. Deriving the obligation from the guard a step later
    means a tick that dies in between leaves a generation still reading as
    `adjudicating` -- no park, no claim, and a next tick that pays for another
    agent against a candidate this one already answered.

    Which is why this is the LAST step of a completion and never a step in the
    middle of one. Everything the completion staged -- the session, the park,
    the notice it owes, the recorded outcome -- is already in memory when this
    runs, so the one write carries all of it. A caller that staged something
    afterwards would be staging it into a write that has already happened.
    """
    context.generation = replace(
        context.generation,
        phase=LatePhase.OWNER_CHECK,
        owner_check_pending=True,
    )
    _persist(context)


def _persist(context: _LateContext) -> None:
    """Write the generation this tick reached, and the state around it."""
    _late_state.write_late_generation(context.state, context.generation)
    context.gh.write_pinned_state(context.issue, context.state)


def _finished(
    context: _LateContext, disposition: _LateDisposition,
) -> _LateAdjudicationRun:
    """Report what this call did, with the run pinned state now records."""
    return _LateAdjudicationRun(
        disposition=disposition,
        generation=context.generation,
        run=_late_session._read_late_run(context.state),
    )
