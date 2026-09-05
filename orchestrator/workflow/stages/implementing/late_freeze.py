# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The pair a count is taken over, and what a record has to carry to be one.

Which commit this tick is deciding about, which base it is measured against,
and whether a record already answering that question may be acted on. Every
answer here is a claim about one object id -- proved in the checkout, or
recorded and proved again -- because a measurement is only worth as much as
the two commits it names, and as much as the identity it can be correlated
by afterwards.
"""
from __future__ import annotations

import logging

from orchestrator import config
from orchestrator.git.measurement import commits as _measurement_commits
from orchestrator.git.measurement.models import (
    FrozenCommit,
    MeasurementFailure,
)
from orchestrator.workflow.late_split.models import LateGeneration
from orchestrator.workflow.stages.implementing import (
    late_parks as _parks,
    late_records as _records,
)

log = logging.getLogger("orchestrator.workflow")

_HEAD = "HEAD"

# The evidence a recorded pair has to carry to be acted on, named as the park
# reports a missing one. The base is first because it is the only one a road
# can legitimately be without.
_MEASUREMENT_FIELDS = (
    ("late_base_sha", lambda recorded: not recorded.base_sha),
    ("late_threshold", lambda recorded: recorded.threshold is None),
    ("late_phase", lambda recorded: recorded.phase is None),
)

# What a pair still waiting for its base is held to: the same evidence less
# the base itself. A base the remote would not name records none at all, and
# the freeze that follows is what supplies one -- but nothing else about that
# record is any less load-bearing for being written mid-freeze, and the mint
# the retry goes through re-stamps every one of these from the current
# process rather than from what the generation was frozen under.
_REFROZEN_FIELDS = _MEASUREMENT_FIELDS[1:]

_MISSING_FIELD = "`{field}` is missing from it"

_DAMAGED_RECORD_PARK = (
    "{mentions} this issue records a measurement of `{candidate}` that "
    "cannot be acted on: {damaged}. A count with no ceiling beside it is not "
    "a verdict and a count recorded against another issue is not this one's "
    "answer, so reading either as a small candidate would publish an "
    "implementation nobody measured. Nothing was published and nothing was "
    "re-run. Repair the pinned comment, or commit again so the candidate is "
    "measured afresh, and reply `/orchestrator continue`."
)

def _candidate_commit(
    gate: _records._Gate, recorded: LateGeneration,
) -> FrozenCommit | None:
    """The commit this tick decides about, or None when the gate is off for it.

    `DECOMPOSE=off` is a decision about NEW work: it stops a candidate ever
    entering the gate and deliberately decides nothing about one already in
    it. So the switch is read against the record rather than on its own, and
    an issue with nothing recorded is the only one it answers outright -- for
    which no commit is proved at all, since there is no question to prove one
    for.

    A call ANSWERING a recorded reading is never that issue, whatever the
    record says. It is a reading a previous tick recorded an intent to take --
    a park a human answered, a frozen pair a crash stranded -- and the
    commonest way to reach one with no candidate recorded is the refusal that
    happens before a generation can be minted: the candidate could not be
    proved, so there was nothing to freeze. Reading that as new work is the
    switch failing OPEN, publishing the very head whose reading is what
    somebody asked for. The switch keeps new candidates out of the gate; it
    does not answer a question the gate already asked.

    "No developer ran" is NOT that claim, and the two are separate fields
    because the seams that borrow the first outnumber the ones that mean the
    second: a clean rebase, a conflict resolution, a divergence publish, and a
    recovery push are each taken with no agent behind them and are each a
    candidate this gate has never seen. Answering the switch with the wider
    fact would measure exactly the fresh work `DECOMPOSE=off` exists to
    publish untouched.

    An issue that still OWES a push is never that issue either, and for the
    same reason one step later: the gate approved a commit, the record naming
    it was dropped by the write that approved it, and the push has not
    happened. Bypassing there hands the publication a candidate this gate has
    not looked at while the record beside it says a debt was paid -- and the
    debt is for one commit and only it, so what would ship is a head under a
    decision taken about a different one.

    The commit is proved rather than read, because everything downstream is a
    claim about one object id: a revision this host cannot peel to a commit is
    work made somewhere else, and nothing here may stand the current head in
    for it.

    A caller that NAMED its candidate is proved even where the switch keeps it
    out, because the switch keeps candidates out of the MEASUREMENT and not
    out of a push that knows which commit it is sending. That proof is local
    -- a revision peeled in this checkout -- so an install with the gate off
    still reads no pull request and parks over none; what it buys is the one
    comparison the naming exists for, and without it a commit landing between
    the caller's read and this one is published in its place.
    """
    if _outside_the_gate(gate, recorded) and not gate.candidate:
        return None
    head = _measurement_commits._prove_candidate_commit(gate.worktree, _HEAD)
    if not recorded.candidate_sha:
        return head
    if head.is_frozen and head.sha == recorded.candidate_sha:
        return head
    return _reconciled_candidate(gate, recorded, head)


def _outside_the_gate(
    gate: _records._Gate, recorded: LateGeneration,
) -> bool:
    """Whether the switch keeps this candidate out of the gate entirely.

    The one state `DECOMPOSE=off` answers outright, spelled once so every seam
    that measures asks it the same way -- and asks it before spending a read
    on anything the answer makes pointless. New work is all it decides: a
    record already in the gate, a call answering a reading the gate itself
    recorded, and a commit an approval still owes a push are each work the
    switch has nothing left to say about.

    The middle one is asked as `answering` rather than as `reconciling`, which
    is the wider fact that no developer ran: a rebase, a resolution, and a
    recovery push each set that and are each new work this gate has never
    seen.

    A call that may publish on a rewrite PERMIT and on nothing else is never
    outside the gate, whatever the switch says. What it is asking for is not a
    count -- the commit is already on the pull request, or already leased for
    a push it is owed -- and the permit that decides it is asked inside, over
    the publication this gate freezes. Kept out, it would publish with nothing
    vouching for the move, finish its route with the verdict still on the
    commit a human ruled on, and leave the permission standing outstanding.
    """
    if config.DECOMPOSE or recorded.candidate_sha:
        return False
    if gate.permit_only:
        return False
    return not gate.answering and not _parks._approved_commit(gate.state)


def _reconciled_candidate(
    gate: _records._Gate, recorded: LateGeneration, head: FrozenCommit,
) -> FrozenCommit:
    """What a record whose candidate is not the current head is reconciled as.

    The recorded commit is asked for FIRST, and that order is the whole
    contract: a recorded SHA is the evidence, and the current head is never a
    substitute for it. A host that cannot peel that object is one the work was
    not made on -- a rebuilt checkout, a machine the branch never reached --
    and it parks rather than measuring, adjudicating, or publishing whatever
    the branch happens to point at there.

    Past that proof the two commits are both HERE, and what that means splits
    on whether a developer ran. On an ordinary disposition it is the ordinary
    situation: the developer was resumed on a human's guidance and committed
    again, so the branch has genuinely moved past what was frozen. That is a
    fresh candidate, measured under a fresh generation of the same cycle --
    exactly as a revision under the adjudication label is. With the switch off
    it is a fresh candidate the gate does not measure, and the record it
    supersedes is retired rather than left standing: a `late_candidate_sha`
    naming work no longer on the branch freezes this branch out of the base
    refresh for good, and describes a commit nothing is going to publish.

    Either way the head is HANDED BACK by name. Which of the two it earns is
    the switch's answer and is decided one owner on, where the record is
    compared against the commit in hand; answering here with "no candidate at
    all" would drop the one fact every step past this needs. Downstream an
    unnamed verdict is not a smaller claim -- the push has no commit to send,
    the receipt has none to record, and the debt reader finds the empty SHA
    equal to an approval nobody wrote and parks the round for a lease that
    does not exist.

    On a RECONCILIATION none of that is available, and the checkout having
    been on the recorded commit a moment ago does not make it available. No
    developer ran on this tick, so there is no run whose output a moved head
    could be; the paths that reach here proved the head against the record
    before they started, and a head that differs NOW is one something moved
    while the tick was in flight -- another process, an operator, a descendant
    the timeout cleanup raced. Reading it as fresh work would measure and
    publish a commit this reconciliation was never about, and with the switch
    off it would retire the record and push that head unmeasured. So the
    reading is refused and the recorded pair is left standing for the retry,
    which is the same answer the pre-gate proof gives to a head that had
    already moved.

    That refusal is asked BEFORE the head is asked whether it is readable, and
    the order is the point: a head that moved to a commit this host cannot
    peel still NAMES one, and a named commit handed back from here is one the
    park downstream records -- minting a generation around it and dropping the
    pair this reconciliation exists to re-read. Unreadable or not, a head that
    is not the recorded candidate is not this tick's to substitute, so on a
    reconciliation it is refused without a name rather than passed on with
    one.
    """
    kept = _measurement_commits._prove_candidate_commit(
        gate.worktree, recorded.candidate_sha,
    )
    if not kept.is_frozen:
        log.error(
            "issue=#%d records candidate %s, which this host cannot read; "
            "refusing to reconcile it against HEAD instead",
            gate.issue.number, recorded.candidate_sha,
        )
        return kept
    if gate.reconciling:
        log.error(
            "issue=#%d is reconciling recorded candidate %s and its checkout "
            "moved to %s mid-tick; refusing to read a head no run of this "
            "tick produced as fresh work",
            gate.issue.number, recorded.candidate_sha,
            head.sha or head.failure,
        )
        return FrozenCommit(
            failure=MeasurementFailure.CANDIDATE_UNREADABLE,
        )
    return head


def _frozen_pair(
    gate: _records._Gate, recorded: LateGeneration, candidate_sha: str,
) -> LateGeneration | None:
    """Persist the exact pair a count is taken over, or park without one.

    The write is the point of the step. It goes out BEFORE the count, carrying
    both commits and the `measuring` boundary, so a tick that dies over the
    diff comes back to the pair this one froze rather than re-deriving one
    from a branch and a remote that have both moved -- which is the difference
    between a retry that measures the same candidate and a retry that measures
    a different one.

    A pair this issue already froze for the commit in hand is reused as it
    stands, and the remote is not asked again. It is the same evidence, the
    base it names is the one a verdict has to be defensible against, and
    re-freezing would let a base that advanced between two ticks change the
    size of a candidate nobody touched -- which is exactly what a retry after
    a base this host could not read would otherwise do, since the id it failed
    on is recorded and the branch it came from has moved on since.

    So the reuse proves that recorded object rather than assuming it, fetching
    once for it as the freeze itself does. It is the retry the recorded
    identity exists for: the SAME commit is asked for again, and a host that
    still does not have it parks rather than measuring against a base nobody
    froze.

    None is a base the remote would not name, or one this host does not hold,
    which is a measurement that did not happen. The identity is recorded all
    the same -- beside the failure, where the freeze puts it -- so the failure
    is reportable on both sinks and the retry has one exact object to ask for.
    Both of those are the transport rather than the record, so what the miss
    costs is a count on the pinned pair and, only once that count runs out, a
    human.

    A base that WAS frozen ends the count in the same write that records the
    pair, since the write happens either way: what a retry is owed is the
    readings it has lost in a row, and one it did not lose is the end of that
    row.
    """
    if recorded.candidate_sha == candidate_sha:
        if recorded.base_sha:
            return _refrozen_base(gate, recorded)
        if _damaged_unfrozen_record(gate, recorded):
            return None
    base = _measurement_commits._freeze_base_commit(gate.spec, gate.worktree)
    minted = _records._minted(gate, recorded, candidate_sha, base.sha)
    if not base.is_frozen:
        _parks._lost_reading(gate, minted, base.failure, base.detail)
        return None
    reached = _parks._reached(minted)
    _parks._persisted(gate, reached)
    return reached


def _refrozen_base(
    gate: _records._Gate, recorded: LateGeneration,
) -> LateGeneration | None:
    """Prove the recorded pair may be reused here, or say why it may not.

    The one question a reused pair still has to ask, because the pair is
    durable and the object store is not: a record written on one host and
    retried on another -- or on the same host after a prune -- names a commit
    this checkout may not hold. Asking the REMOTE again instead would answer
    with wherever the base branch is now, so the retry a human's continue
    drives would silently measure a different pair under the same generation.

    Whether the record may be ACTED on at all is asked first, and it is this
    owner's own precondition rather than each caller's, because the retry
    below is durable: it writes the miss it counted back onto the pinned
    comment. A record reaching that damaged -- or recorded against another
    issue -- would be rewritten under THIS issue's whole identity and become
    publishable the moment the base came back, which is a reading taken over
    there shipping work over here. A record nobody may act on is not a pair
    whose transport is worth retrying; it is one a human has to repair, and
    the roads in are too many for the proof to live on any of them.

    Past that proof the record is one the pinned comment may carry as it
    stands, so the retry persists it rather than an identity minted for the
    report: there is nothing left here to mint one for.

    An object a fetch did not bring back is the transport rather than the
    record, and the same fetch on the next tick is very often all it takes --
    so what a miss costs is one of the readings this pair is allowed to lose,
    and only the last of them is worth a human.
    """
    if _damaged_record(gate, recorded):
        return None
    base = _measurement_commits._base_object_present(
        gate.spec, gate.worktree, recorded.base_sha,
    )
    if base.present:
        return _reached_base(gate, recorded)
    log.warning(
        "issue=#%d records base %s, which this host does not hold even after "
        "a fetch (%s); refusing to re-read the remote for a different one",
        gate.issue.number, recorded.base_sha, base.detail,
    )
    _parks._lost_reading(
        gate, recorded, MeasurementFailure.BASE_ABSENT, base.detail,
    )
    return None


def _reached_base(
    gate: _records._Gate, recorded: LateGeneration,
) -> LateGeneration:
    """The record a base this host holds leaves, with any miss it owed dropped.

    Written where there is something to drop and only there. The reset has to
    be durable -- every tick is a fresh process, and the reading right after
    this one is the one a stale count would hand to a human early -- while a
    pair that never lost a reading would otherwise pay a pinned write on every
    tick for a field it does not carry.
    """
    reached = _parks._reached(recorded)
    if reached == recorded:
        return recorded
    _parks._persisted(gate, reached)
    return reached


def _unusable_record(
    gate: _records._Gate, recorded: LateGeneration, fields: tuple,
) -> str | None:
    """Why a recorded measurement may not be acted on, or None if it may.

    Named rather than counted, because the park has to tell a human which
    part to repair -- and because the parts are not interchangeable: a missing
    threshold and a missing base are two different reasons the number beside
    them means nothing.

    Which fields are asked for is the caller's, because it is the caller that
    knows what its road has established: a pair still waiting for its base is
    proved against everything but that one, and a pair that recorded one is
    proved against all of them.

    The identity carries the same weight as the count's own fields and is the
    half that is easy to forget, because nothing downstream reads it: a record
    with no cycle, no generation, or no root cannot be joined to the audit
    line the measurement was reported on, to the lineage a split would be
    bounded by, or to the verdict an adjudication files -- and a count that
    can be published but not correlated is a reading no operator can defend
    afterwards. One naming another issue is worse still: it is not this
    issue's answer at all, so publishing on it would ship work here on a
    reading taken over there.
    """
    for field, missing in fields:
        if missing(recorded):
            return _MISSING_FIELD.format(field=field)
    return _records._unusable_identity(gate, recorded)


def _damaged_record(gate: _records._Gate, recorded: LateGeneration) -> bool:
    """Park a recorded pair whose metadata cannot be acted on, or pass it.

    Asked on BOTH roads into a recorded pair, because the fields it checks are
    written by the freeze rather than by the count: a record reused for a
    reading that has still to be taken is as damaged without them as one whose
    number is already in. The counted road would otherwise be the only one
    guarded, and the uncounted one -- the ordinary crash retry -- would carry
    a threshold-less record into `_verdict_owner._settled`, where the record's own comparison
    answers "not oversized" on a missing ceiling and publishes it.

    The whole record is asked here because a base is one of the fields: this
    is the road where one was recorded, so a pair short of it is damaged
    rather than mid-freeze.
    """
    return _parks_the_damage(
        gate, recorded, _unusable_record(gate, recorded, _MEASUREMENT_FIELDS),
    )


def _damaged_unfrozen_record(
    gate: _records._Gate, recorded: LateGeneration,
) -> bool:
    """Park a record with no base this issue may not mint over, or pass it.

    The other half of the reuse proof, for the pair that has no base to
    re-prove. A base the remote would not name records no base at all, so the
    retry over that same candidate freezes afresh -- and the mint it freezes
    under INHERITS the record beside it: the cycle it is correlated by, the
    scope it was declared with, and the readings it has already spent, while
    re-stamping this issue's number, the configured ceiling, and the boundary
    from the process running NOW. Unproved, a reading taken against another
    issue becomes this one's to measure and then to publish, and a generation
    that lost the ceiling it was frozen under is re-judged against whatever
    the setting has been retuned to since.

    Everything but the base is asked for, because the base is the only field
    this road is legitimately without: it is what the failure being retried
    leaves behind, and the freeze below is what supplies one. Asking for it
    here would park every transport retry on the very pair it exists to keep
    re-reading.
    """
    return _parks_the_damage(
        gate, recorded, _unusable_record(gate, recorded, _REFROZEN_FIELDS),
    )


def _parks_the_damage(
    gate: _records._Gate, recorded: LateGeneration, damaged: str | None,
) -> bool:
    """Hand back a record that may not be acted on, under the reason it fails.

    None is a record that may, which is what every caller passes on. The
    failure reaches both sinks like every other refusal, so a record that
    fails open in the log is not what an operator has to notice.
    """
    if damaged is None:
        return False
    log.error(
        "issue=#%d records a measurement of %s that cannot be acted on "
        "(%s); parking rather than reading it as an answer",
        gate.issue.number, recorded.candidate_sha, damaged,
    )
    return _parks._parked(
        gate, _records._reportable(gate, recorded), damaged,
        _DAMAGED_RECORD_PARK.format(
            mentions=config.HITL_MENTIONS,
            candidate=recorded.candidate_sha,
            damaged=damaged,
        ),
    )
