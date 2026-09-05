# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a landed publication does with the permission that licensed it.

The far end of the transfer. `late_transfer` grants a PERMISSION before the
push and moves nothing, because the object a rewrite produced is on no remote
yet and a verdict rotated onto it there is one a failed push or a dead process
strands. This owner is the write that finishes the move, and it runs at the
one moment the move is safe: the push has landed, so the commit a human's
verdict is about to name is a commit the pull request really carries.

Everything it stages rides the receipt's own write, and that is the whole
design. The exemption, the semantic identity beside it, the phase that says
the transfer is over, the account of what the remote now holds, the debt that
account pays, and the route bookkeeping the landing closes are one statement
about one publication -- split across two writes, a crash between them leaves
a comment claiming a verdict for a commit no receipt names, or a receipt for a
commit no verdict covers, and neither is a state any reader here can tell from
a hand edit.

What licenses the move at all is the PERMIT, re-asked by `late_transfer` on
this same tick and handed down the push tail beside the commit it proved out
for. The permission on the comment is not that: it says a permit was once
granted, and a refusal since -- a repointed pull request, a relabelled issue,
a checkout that stopped being clean, a contribution that no longer
fingerprints alike -- does not stop the candidate publishing, because the
ordinary cumulative gate measures it instead and a count under the ceiling
sends the same commit out. So a permission this tick's permit did not vouch
for is left exactly where it stands, and nothing is rotated or reported for
it.

What proved the publication is the other half of the record, and there are
exactly two answers. A leased force-push that moved the pull request off the
head the permit was granted against is the ordinary one. A retry that found
the pull request already standing on the rewritten commit is the recovery --
a tick that pushed and died before its receipt -- and what proves it is the
leased no-op the push tail makes anyway, taken at the remote rather than read
off a local note. There is no third: a pull request standing anywhere else is
a permit `late_transfer` refuses, so nothing reaches here to settle, and a
reading that could not be taken at all refuses the same way. Neither is ever
read as equivalence, and neither is ever pushed for unleased -- the lease is
the head the entry froze, or the rewritten commit itself where the remote is
already on it.

A permission this publication went PAST is dropped rather than rotated. The
push put some other commit on the pull request, so the head the permit was
granted against is gone and no later tick can be granted it again: what is
left is a claim about a push that will never happen, and the exemption it was
granted for has not moved. Only a record this build can read back whole and
still finds outstanding is dropped, which is the rollback's own rule -- a
group nobody can check is the only account there is of how the exemption came
to name what it names.

The record it emits is one `late_transfer` event and nothing else. In
particular it is not a second `late_verdict`: a transfer carries a decision a
human already made onto the object that replaced the one they made it about,
and a second `single` on the stream would read as a second adjudication of
work nobody was asked about twice.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from orchestrator.workflow.late_split import (
    events as _events,
    rewrites as _rewrites,
    state as _late_state,
    telemetry as _telemetry,
)
from orchestrator.workflow.late_split.models import LateGeneration
from orchestrator.workflow.stages.implementing import (
    late_publication as _publication_gate,
    late_records as _records,
)

log = logging.getLogger("orchestrator.workflow")


@dataclass(frozen=True)
class _Rotation:
    """What the settlement staged for one publication that landed.

    `staged` is what the caller's write turns on: a rotation and a dropped
    permission both change the comment, and a tick that staged neither may not
    spend a request saying what the comment already says.

    `rewrite` is what the caller REPORTS, and it is present only where a
    verdict actually moved. A dropped permission moved nothing -- the
    exemption is where the adjudication put it and always was -- so there is
    no transfer to describe and the stream stays quiet about it.

    `proof` travels with the rewrite because it is the half of the record no
    later reader could recover: which of the two publications proved the move
    is a fact about the remote at the moment of the push, and the receipt
    behind it looks identical either way.
    """

    staged: bool = False
    rewrite: _rewrites.LateRewrite | None = None
    proof: _rewrites.LateRewriteProof | None = None

    @property
    def is_reportable(self) -> bool:
        """Whether a verdict moved and therefore owes the sinks a record."""
        return self.rewrite is not None


# What a publication with no permission behind it stages and reports.
_NOTHING = _Rotation()


def _rotates_the_exemption(
    gate: _records._Gate,
    published: _publication_gate._PublishedCandidate,
) -> _Rotation:
    """Stage what a landed push does to the permission that licensed it.

    Asked of the PERMISSION rather than of the commit, for the reason every
    other reader of this record is: the grant writes the permission and the
    debt in one write for one commit, so a hand-edited target would otherwise
    make the permit invisible and leave a verdict standing over a rewrite
    nothing revalidated.

    Four answers, and only the first of them moves anything. A permission
    naming the commit that just landed, where THIS tick's permit proved out
    for that same commit, is spent: the exemption, its identity, and the phase
    go over together. A permission naming some OTHER commit is one this
    publication has gone past, and it is dropped. A permission no permit
    vouched for on this tick is left exactly where it stands. And a comment
    with no permission on it, one this build cannot read back whole, and one
    already spent each stage nothing at all -- the first has nothing to
    settle, and the other two are records this owner may not repair under the
    authority of a push it is in the middle of receipting.

    The permit's own answer is what the move turns on, and the record beside
    it is not a substitute for it. A permission on the comment says a permit
    was once granted; what says it still holds is `late_transfer` re-asking
    it, this tick, over the publication, the checkout, the issue, and both
    fingerprints. A refusal there does not stop the candidate publishing --
    it falls through to the ordinary cumulative gate, and a count under the
    ceiling sends the same commit out -- so a settlement reading the record
    alone would rotate a human's verdict onto a rewrite nothing revalidated
    and write the very digest the permit declined.

    Staged rather than persisted, because what makes it durable is the
    receipt's own write: the move and the account of what the remote holds
    land together or not at all.
    """
    authorization = _rewrites.read_rewrite_authorization(gate.state)
    if authorization is None:
        return _NOTHING
    if authorization.phase != _rewrites.LateRewritePhase.AUTHORIZED:
        return _NOTHING
    rewrite = authorization.rewrite
    if rewrite.to_sha != published.revision:
        return _abandoned(gate, rewrite, published.revision)
    if published.permitted_sha != rewrite.to_sha:
        return _unvouched(gate, rewrite)
    return _carried(gate, published)


def _unvouched(
    gate: _records._Gate, rewrite: _rewrites.LateRewrite,
) -> _Rotation:
    """Leave a permission this tick's permit did not vouch for standing.

    The publication is this permission's own commit and the push landed, so
    everything about the REMOTE lines up -- and that is exactly why the
    record alone may not be believed. A permit is granted on terms that can
    stop being true between the grant and here: a repointed pull request, a
    relabelled or paused issue, a checkout that stopped being clean, a lease
    this host can no longer peel, a recorded digest the contribution no longer
    takes. Any of them refuses the permit and leaves the ordinary cumulative
    gate to measure the rewrite, which publishes it whenever the count is
    under the ceiling. Rotating there would carry a human's verdict onto a
    change nothing revalidated, under a digest the permit just declined.

    Left standing rather than dropped, because a refusal is not evidence the
    transfer is dead: the remote is now on the rewritten commit, which is a
    head the permit accounts for while the permission is outstanding, so a
    later tick whose refusal has cleared can re-ask it in full and settle it
    then. What the standing permission costs in the meantime is a reading --
    the gate defers every approval on this issue back to the permit -- which
    is the conservative half of the same rule.
    """
    log.info(
        "issue=#%d published %s and no permit vouched for it on this tick; "
        "leaving the permission granted for it exactly as it stands rather "
        "than carrying the exemption for %s onto a rewrite nothing "
        "revalidated",
        gate.issue.number, rewrite.to_sha, rewrite.from_sha,
    )
    return _NOTHING


def _carried(
    gate: _records._Gate,
    published: _publication_gate._PublishedCandidate,
) -> _Rotation:
    """Carry the exemption onto the commit this push put on the remote.

    The record owner makes the move, not this one: the exemption, the identity
    it carries, and the phase that says the transfer is over are one record,
    and an owner setting the phase for itself would be free to announce a
    transfer whose verdict it never moved. What is decided here is only WHEN
    -- which is now, because the push has landed and the commit is one the
    pull request really carries.
    """
    proof = _proved_by(published)
    rewrite = _rewrites.record_rewrite_publication(gate.state, proof)
    log.info(
        "issue=#%d carried the exemption for %s onto %s, which pull request "
        "#%d %s; the %s rewrite is settled",
        gate.issue.number, rewrite.from_sha, rewrite.to_sha,
        rewrite.pr_number, proof, rewrite.kind,
    )
    return _Rotation(staged=True, rewrite=rewrite, proof=proof)


def _proved_by(
    published: _publication_gate._PublishedCandidate,
) -> _rewrites.LateRewriteProof:
    """Which reading proved this publication is the one the permit licensed.

    The head the entry FROZE is the whole of the answer, and it is a reading
    of the remote taken this tick before any effect. Standing on the commit
    already, the push was the leased no-op a lost receipt earns and the work
    reached the remote on a tick that died; standing anywhere else it was the
    head the permit was granted against, and the leased force-push moved it.

    Nothing here reads a record to decide it, because a local note is exactly
    what cannot distinguish the two: the receipt this write is about to make
    looks identical whichever of them happened.
    """
    if published.standing == published.revision:
        return _rewrites.LateRewriteProof.ALREADY_PUBLISHED
    return _rewrites.LateRewriteProof.PUSHED


def _abandoned(
    gate: _records._Gate, rewrite: _rewrites.LateRewrite, published: str,
) -> _Rotation:
    """Drop a permission the commit this push published has gone past.

    The permit was granted against one head of one pull request, and this push
    has moved that pull request onto some other commit -- so the remote will
    never again stand where the permit accounts for and no later tick can be
    granted it. What is left is a claim about a push that will never happen,
    which the next rewrite on this issue would have to decide whether to
    replace and which every reader would go on deferring to.

    The exemption needs no repair, and that is what makes the drop safe: the
    grant never moved it, so it is still the commit a human ruled on and the
    identity beside it still describes that commit's own contribution.

    Only an outstanding record this reader can vouch for entirely is dropped
    -- the two refusals above this call are what guarantee it -- which is the
    rollback's rule for the same reason: a group nobody can check is the only
    account there is of how the exemption came to name what it names.
    """
    log.info(
        "issue=#%d published %s and no longer has a head to spend the "
        "permission granted for %s against; dropping it rather than leaving a "
        "claim about a push that cannot happen",
        gate.issue.number, published, rewrite.to_sha,
    )
    _rewrites.clear_rewrite_authorization(gate.state)
    return _Rotation(staged=True)


def _reports_the_transfer(gate: _records._Gate, rotation: _Rotation) -> None:
    """Write the one record a settled transfer leaves on both sinks.

    Reported once the move is durable rather than beside it, because a record
    of a verdict that moved is worth nothing if the write carrying the move
    was refused -- and a refused write ends this tick, so there is no road on
    which the transfer stands unreported.

    The correlation is minted the way every other record with no live
    generation behind it is: a transfer runs past the retirement that dropped
    the pair it was adjudicated under, so there is no cycle left to file it
    against and one is derived from what the pinned comment already says.
    Stable across retries, so a settlement that lands twice reports the same
    attempt rather than a fresh one each tick.

    The pair the record carries is the pair the exemption moved ONTO, since
    that is what a later measurement of this same work would be joined on, and
    the publication group comes off the AUTHORIZATION rather than off this
    call: the record is the account of the pull request the transfer was
    granted against, and the caller in the push tail has no entry of its own
    to read one from.

    No phase, and deliberately: the phases are the boundaries a generation's
    reconciliation stands at, and a transfer is not one of them -- it happens
    past the retirement that ended the last.
    """
    if not rotation.is_reportable:
        return
    _reported_transfer(gate, rotation.rewrite, rotation.proof)


def _reports_a_settled_transfer(gate: _records._Gate) -> bool:
    """Make the record a settled transfer is still owed, if it is owed one.

    The window the proof on the comment exists for. A transfer is settled by
    the write that receipts its push and reported behind that write, so a
    process lost in between leaves a verdict that moved and nothing on either
    sink saying so -- and the one fact nothing later could re-derive, which
    reading proved the push landed, is exactly what that write kept.

    So the record is made from what the comment carries, and the write that
    drops the proof it was made from is taken with it: every tick that finds
    the proof still standing reports the record again, so the drop may not
    wait on a caller's write that is not guaranteed. Answering whether the
    record was made is what tells that caller a verdict on this comment has
    already been announced.

    Silent for every comment that owes nothing, which is a comment carrying
    no proof at all. A proof standing over a transfer this build cannot read
    whole, one still outstanding, or a reading this build does not know is
    not silence: it is a checkpoint saying two things at once, and the roads
    that ask park on it rather than reaching here.
    """
    proof = _rewrites.unreported_transfer(gate.state)
    if proof is None:
        return False
    authorization = _rewrites.read_rewrite_authorization(gate.state)
    log.info(
        "issue=#%d settled the transfer for %s onto %s and never reported it; "
        "making the record the write behind that settlement owed",
        gate.issue.number, authorization.rewrite.from_sha,
        authorization.rewrite.to_sha,
    )
    _reported_transfer(gate, authorization.rewrite, proof)
    return True


def _reported_transfer(
    gate: _records._Gate,
    rewrite: _rewrites.LateRewrite,
    proof: _rewrites.LateRewriteProof,
) -> None:
    """Write the one record on both sinks, and drop the proof it was made from.

    The drop is made durable HERE rather than left for the caller, because the
    caller's next write is not guaranteed and every tick that finds the proof
    still standing reports the record again. It is the one write in this
    domain that carries nothing but the fact that something has already been
    said, and it costs a request on the rare tick a verdict actually moves.

    A write GitHub refuses leaves the proof where it is, which is the safe way
    round: the record has been made and a later tick may make it again, rather
    than a settled transfer nobody ever announced. So it is logged and the
    tick carries on.
    """
    _telemetry.emit_late_event(
        gate.gh,
        _events.LateEvent(
            family=_events.LateEventFamily.TRANSFER,
            rewrite_kind=rewrite.kind,
            transfer_proof=proof,
            transferred_from_sha=rewrite.from_sha,
            transferred_from_base_sha=rewrite.from_base_sha,
        ),
        _reported(gate, rewrite),
        stage=rewrite.source_stage,
    )
    _rewrites.forget_transfer_proof(gate.state)
    try:
        gate.gh.write_pinned_state(gate.issue, gate.state)
    except Exception:
        log.warning(
            "issue=#%d reported the transfer onto %s and could not drop the "
            "proof it was made from; a later tick may report it again",
            gate.issue.number, rewrite.to_sha, exc_info=True,
        )


def _reported(
    gate: _records._Gate, rewrite: _rewrites.LateRewrite,
) -> LateGeneration:
    """The generation one transfer record is correlated by."""
    recorded = _late_state.read_late_generation(gate.state)
    carried = replace(
        _records._reportable(gate, recorded),
        candidate_sha=rewrite.to_sha,
        base_sha=rewrite.to_base_sha,
        phase=None,
    )
    return carried.with_publication(
        stage=rewrite.source_stage,
        pr_number=rewrite.pr_number,
        published_sha=rewrite.lease,
    )
