# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a post-publication record claims, and the claims nothing may act on.

Two readings of the same pinned fields, taken together because they are the
same question asked twice: does this issue still owe the gate something, and
can the record it owes it under be read at all.

Every field in this domain is read fail-closed, and for the readings the gate
takes that is the whole answer: a value it cannot use is a value it does not
have. Ahead of the HANDLER it is only half of one. The reconciliations behind
this recognise unfinished work by what the record says -- a whole publication
group with no count, an approval with the head it was pinned against -- so a
record missing any one of those pieces reads as no record at all, and the
stage runs.

That is the fail-open this owner closes. A pinned comment that CLAIMS a
post-publication reading and cannot produce it is evidence something is wrong
with the evidence, and the one thing that must not follow is the stage: a
reviewer spawned over a pull request nobody can say received the work, a
bounce relabelling on a candidate nobody measured, a docs pass committing on
top of either. So the raw fields are read here, before anything is parsed into
a shape that can lose them, and a claim that cannot be made whole parks.

Nothing here repairs anything. The pieces are not recoverable from elsewhere
-- the label the record names has been replaced, the hold beside it names a
pull request only because this record named one first, the head is a commit
the branch has moved off -- so what
the refusal owes is a human, and it owes them one notice rather than one per
poll.
"""
from __future__ import annotations

import logging

from orchestrator import config
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow import state as _workflow_state
from orchestrator.workflow.late_split import (
    keys as _late_keys,
    rewrites as _rewrites,
    state as _late_state,
)
from orchestrator.workflow.late_split.models import LateGeneration
from orchestrator.workflow.stages.implementing import (
    late_parks as _parks,
    late_records as _records,
    state as _state,
)
from orchestrator.workflow.state import WorkflowLabel

log = logging.getLogger("orchestrator.workflow")


def _awaits_its_count(recorded: LateGeneration) -> bool:
    """Whether a live post-publication record still owes its measurement.

    The only window a source stage can be left holding one. A count that came
    back small retires the record in the same write, and one past the ceiling
    moves the issue to the adjudication -- so a generation still sitting on a
    stage that publishes, with a whole publication group and no number, is a
    tick that died between the freeze and the diff and nothing else.

    Unless the split that reading earned has settled. A candidate that became
    children owes no count, and the record says so by carrying none: what a
    settled split keeps is the publication group, for the releases and the
    branch delete its umbrella still has to make, and what it drops is the
    measurement, because one answering "oversized" pins `workflow:decomposing`
    and would put the umbrella label back on every tick. Read without that,
    the finished adjudication wears the whole shape of a reading nobody took,
    and the record it is read off strands the very umbrella it made: the group
    names the stage the gate was entered from, the issue is on
    `workflow:umbrella` now, and the refusal for a pair read off its own stage
    holds every tick in front of the handler that would release the children.
    """
    if not recorded.has_publication_context or recorded.cancelled:
        return False
    if recorded.split_has_settled:
        return False
    return bool(recorded.candidate_sha) and recorded.additions is None


# Every field one frozen reading is written with, paired with the reading that
# has to survive the parse for it. The pinned comment carries all of them from
# one durable write, so a value it CARRIES and the parse drops is damage
# rather than a tick part-way through: the readers behind this answer "absent"
# for it, which is what an issue that froze nothing answers too. A malformed
# candidate reads as no reading owed and the stage runs; a malformed base is
# re-frozen from a remote that has moved, so the same generation is measured
# over a different pair; a malformed count is taken again against it; and a
# malformed marker hides the whole group from the comparison that proves the
# publication has not moved.
#
# `_MINTED_EVIDENCE` is the part that write puts down in ONE go, so it is
# required rather than merely checked when present: a comment carrying late
# fields and missing one of these is the same damage read from its other end.
# The three below it are legitimately absent -- a base the remote would not
# name is recorded missing so the retry has one exact object to ask for, a
# count lands only once the diff is taken, and a generation entered before
# publication carries none of the group.
_MINTED_EVIDENCE = (
    (_late_keys.CYCLE_ID, lambda recorded: bool(recorded.cycle_id)),
    (_late_keys.GENERATION, lambda recorded: bool(recorded.generation)),
    (_late_keys.ROOT_ISSUE, lambda recorded: bool(recorded.root_issue)),
    (
        _late_keys.CURRENT_ISSUE,
        lambda recorded: bool(recorded.current_issue),
    ),
    (
        _late_keys.CANDIDATE_SHA,
        lambda recorded: bool(recorded.candidate_sha),
    ),
    (_late_keys.THRESHOLD, lambda recorded: recorded.threshold is not None),
    (_late_keys.PHASE, lambda recorded: recorded.phase is not None),
)


_FROZEN_EVIDENCE = _MINTED_EVIDENCE + (
    (_late_keys.BASE_SHA, lambda recorded: bool(recorded.base_sha)),
    (_late_keys.ADDITIONS, lambda recorded: recorded.additions is not None),
    (
        _late_keys.POST_PUBLICATION,
        lambda recorded: recorded.post_publication,
    ),
    (
        _late_keys.SOURCE_STAGE,
        lambda recorded: recorded.source_stage is not None,
    ),
    (
        _late_keys.PUBLISHED_PR_NUMBER,
        lambda recorded: bool(recorded.published_pr_number),
    ),
    (
        _late_keys.PUBLISHED_SHA,
        lambda recorded: bool(recorded.published_sha),
    ),
)


# How a record can claim a post-publication reading it cannot produce. Each is
# spelled as the park comment reads it, because what an operator has to repair
# differs by which half of the claim is missing.
_DAMAGED_EVIDENCE = (
    "the record carries frozen evidence the pinned comment cannot produce -- "
    "a field is there and is not a value this domain can read back"
)


_DAMAGED_SPENDS = (
    "the record says a hold owed route bookkeeping and cannot produce it as "
    "one group -- a member names no field this workflow closes, or is not a "
    "value the pinned comment can carry"
)


_DAMAGED_PUBLICATION = (
    "the record carries part of a publication group and not the whole of one "
    "-- the marker saying it was entered on a publication, the stage it came "
    "from, the pull request, and the head that pull request was standing on "
    "go down in one write and are not all there"
)


_DAMAGED_APPROVAL = (
    "the record says a commit is owed a push onto a pull request and cannot "
    "name the commit and the head to pin it against as a pair"
)


_DAMAGED_TRANSFER = (
    "the record says a transfer settled and still owes the sinks an account "
    "of it, and cannot produce one -- the note saying which reading proved "
    "that push landed is standing over a permission, a phase, or a reading "
    "nothing here can account for"
)


_DAMAGED_RECORD_PARK = (
    "{mentions} this issue's pinned comment claims a record taken over a "
    "pull request the remote already carries, and {refusal}. None of it can "
    "be worked out from anywhere else -- the label it names has been "
    "replaced, the pull request is not the plan one beside it, the head is a "
    "commit the branch has moved off, and which reading proved a push landed "
    "is a fact about the remote at the moment of that push -- and the stage "
    "may not run over the claim either, since it would hand a reviewer a "
    "pull request nobody can say received the work, or carry a verdict "
    "nothing here can account for. Nothing was pushed and nothing was "
    "discarded. Repair the pinned comment and the next tick reads it again."
)


def _unreadable_record(
    label: WorkflowLabel | None, state: PinnedState,
) -> str:
    """Why this issue's late record may not be acted on, or "" if it may.

    Asked of the RAW fields, because the parse is what loses them: a group
    missing one member comes back as no group, and an approval missing its
    lease comes back as no approval, and both of those read to the
    reconciliations behind this as an ordinary issue with nothing owed.

    All five claims on the five stages that publish onto a pull request the
    remote already carries, and they are named off the transition graph's own
    set rather than derived from it. `workflow:implementing` has an edge to
    the adjudication too and is NOT one of them: its approval carries no
    pull-request head because its push is the one that opens the pull
    request, so a crash between the two leaves exactly the shape this owner
    would otherwise call damaged.

    The five are also where every rewrite this workflow settles resumes, and
    that is what puts the transfer's own note among the claims. A settlement
    is one durable write and the record of it goes to the sinks behind that
    write, so a process lost in between leaves a note saying an account is
    owed -- and one standing over a permission, a phase, or a reading nothing
    here can account for is a claim like any other. Read as nothing owed, the
    reconciliation walks past it, the account is never made, and the corrupt
    note stands for the life of the issue while the stage runs behind it.

    `workflow:decomposing` is the adjudication's, and it is asked TWO of
    them. The publication group is the one piece of evidence that
    mode cannot re-derive and the one it decides everything by: a settlement
    reads it to know which pull request the verdict was taken over, which
    head to pin the push it licenses to, and which stage to hand the issue
    back to -- so a marker a hand edit took reads as a candidate nothing had
    published, and the accepted commit is routed to `workflow:implementing`
    with the frozen evidence retired behind it. The transfer note is the
    other, because a note that cannot produce the account it claims is damage
    in any mode: nothing writes an unreadable one, the statement that settles
    a transfer puts the note and the phase down together, and the grant that
    replaces a transfer drops the note with it -- so there is no settlement
    in flight for a refusal here to hold up, only a comment something took
    apart, and letting the adjudication run over it leaves the account
    unreported for the life of the issue.

    The other two are not asked there, and the approval is the reason: a
    verdict taken before anything was published approves its commit with no
    head to pin it against, which is exactly the half-written pair this owner
    calls damage everywhere else.
    """
    if label == WorkflowLabel.DECOMPOSING:
        asked = _ADJUDICATION_CLAIMS
    elif _workflow_state.publishes_onto_a_pull_request(label):
        asked = _CLAIMS
    else:
        return ""
    for claims, refusal in asked:
        if claims(state):
            return refusal
    return ""


def _claims_a_reading(state: PinnedState) -> bool:
    """Whether the comment carries frozen evidence the parse cannot produce.

    Three ways, and they are one damage read from three sides.

    A field the comment CARRIES that no reader will type is one every owner
    behind this treats as absent -- so the reconciliation finds nothing owed,
    the freeze re-derives the half it cannot see from a remote that has moved,
    and the stage runs over a reading nobody can defend.

    A field the comment does NOT carry at all is the same gap with nothing
    left to notice it by. The write that mints a generation puts
    `_MINTED_EVIDENCE` down in one go, so a record missing one of those is a
    record something edited -- and each reader answers for the hole exactly as
    it answers for an issue that froze nothing: no ceiling reads as a
    candidate that is never oversized, no phase as a generation standing at no
    boundary, no identity as a reading no audit line or lineage can be joined
    to, and no candidate as a record about no commit at all.

    The base is the one field asked conditionally, because a reading that
    could not freeze one is a state this domain PERSISTS -- the failure goes
    down beside the identity so the retry has an exact object to ask for. A
    COUNT beside it is what makes the absence damage: a number is taken over a
    pair, so a record carrying one and unable to show the base it was measured
    from cannot defend the answer it holds.

    A cancelled cycle is none of these. The cancellation owner reads the same
    record and ends it, and a group half-cleared on the way out is that
    owner's to finish rather than a human's to repair.
    """
    recorded = _late_state.read_late_generation(state)
    if recorded.cancelled:
        return False
    carried = [
        survives
        for key, survives in _FROZEN_EVIDENCE
        if state.get(key) is not None
    ]
    if not carried:
        return False
    if any(not survives(recorded) for survives in carried):
        return True
    if any(not survives(recorded) for _, survives in _MINTED_EVIDENCE):
        return True
    return recorded.additions is not None and not recorded.base_sha


def _claims_a_spend(state: PinnedState) -> bool:
    """Whether recorded route bookkeeping cannot be restored as one group.

    The reader hands back all of it or none of it, because the caller that
    restores a hold cannot tell which half it got: a round advanced without
    the bookmark it was spent for leaves the next re-entry rerunning a
    developer over feedback that was already answered. So an empty read
    against a key the comment still carries is the damage, and it parks here
    rather than the reconciliation quietly closing nothing.
    """
    if state.get(_late_keys.SPENDS) is None:
        return False
    return not _late_state.read_late_spends(state)


# Every raw field one publication group is written with, the marker
# included. The group is asked from EITHER end, because the marker is a member
# rather than the question: a comment carrying the stage, the pull request,
# and the head with no marker over them reads back as a pre-publication record
# -- nothing owed, nothing frozen -- and the stage below runs on a candidate
# nobody measured and nobody pushed. Asked this way the same edit is damage
# whichever member of the four it took.
_PUBLICATION_GROUP = (
    _late_keys.POST_PUBLICATION,
    _late_keys.SOURCE_STAGE,
    _late_keys.PUBLISHED_PR_NUMBER,
    _late_keys.PUBLISHED_SHA,
)


def _claims_a_publication(state: PinnedState) -> bool:
    """Whether a partial publication group cannot produce all four members.

    One durable write puts all four down together and a pre-publication record
    carries none of them, so a comment holding SOME of the group is a comment
    something edited. Which one it took makes no difference to what follows:
    the marker gone reads as an entry taken before publication, and any of the
    other three gone reads as a marker standing over nothing.

    A cancelled cycle is not one of these: the cancellation owner reads the
    same record and ends it, and a group half-cleared on the way out is that
    owner's to finish rather than a human's to repair.
    """
    if not any(state.get(key) is not None for key in _PUBLICATION_GROUP):
        return False
    recorded = _late_state.read_late_generation(state)
    if recorded.cancelled:
        return False
    return not recorded.has_publication_context


def _claims_an_approval(state: PinnedState) -> bool:
    """Whether a recorded debt cannot produce the pair it is spent as.

    The commit and the head it is pinned against are written together and
    mean nothing apart, so either one standing alone on a stage that publishes
    onto an existing pull request is half a claim. Read raw first, because a
    value the parse rejects is exactly the damage this is looking for.
    """
    claimed = any(
        state.get(key) is not None
        for key in (_state._APPROVED_SHA, _state._APPROVED_LEASE)
    )
    if not claimed:
        return False
    return not (
        _parks._approved_commit(state) and _parks._approved_lease(state)
    )


# Every claim a record can make and fail to produce, in the order an operator
# reads them: the evidence itself, then the publication it was entered on,
# then the debt it says is owed, then the bookkeeping a hold left behind, and
# last the account a settled transfer still owes the sinks. Each names what
# has to be repaired, because the pieces are not interchangeable.
#
# The transfer's own reader answers for the last of them rather than a
# question worded here, for the reason every other reader of that record is
# held to it: the note is written by the one statement that settles a
# transfer and dropped by the write behind the record it feeds, so what
# "cannot produce it" means is that owner's to say. Spelled again here, the
# two would drift and this seam would walk past exactly the state the
# recovery on the other side of the same window parks for.
_CLAIMS = (
    (_claims_a_reading, _DAMAGED_EVIDENCE),
    (_claims_a_publication, _DAMAGED_PUBLICATION),
    (_claims_an_approval, _DAMAGED_APPROVAL),
    (_claims_a_spend, _DAMAGED_SPENDS),
    (_rewrites.stranded_transfer_proof, _DAMAGED_TRANSFER),
)


# The two the adjudication is asked instead. It is mid-way through deciding
# the reading and the approval, so neither is a claim it has failed to
# produce; the publication group and the transfer note are records it did not
# write and cannot repair, and both are damage wherever they stand.
_ADJUDICATION_CLAIMS = (
    (_claims_a_publication, _DAMAGED_PUBLICATION),
    (_rewrites.stranded_transfer_proof, _DAMAGED_TRANSFER),
)


def _parks_the_damage(gate: _records._Gate, refusal: str) -> bool:
    """Stop a tick whose record claims something it cannot produce.

    Announced ONCE. Nothing this process can repair is behind it, so a fresh
    notice every poll would be a mention nobody can answer any faster; a park
    already standing for the same reading is left exactly as it is.
    """
    if gate.state.get(_state._PARK_REASON) == _parks.PARK_MEASUREMENT_FAILED:
        log.warning(
            "issue=#%d still carries a record nothing can read (%s); holding "
            "the tick without a second notice",
            gate.issue.number, refusal,
        )
        return True
    log.error(
        "issue=#%d records a post-publication claim it cannot produce (%s); "
        "refusing to run its stage over a claim nothing can check",
        gate.issue.number, refusal,
    )
    _parks._parked(
        gate, _records._reportable(gate, _late_state.read_late_generation(
            gate.state,
        )),
        refusal,
        _DAMAGED_RECORD_PARK.format(
            mentions=config.HITL_MENTIONS, refusal=refusal,
        ),
    )
    gate.gh.write_pinned_state(gate.issue, gate.state)
    return True
