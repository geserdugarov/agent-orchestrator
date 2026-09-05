# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The push a gated publication makes, and everything it spends.

The effect half. Every push onto a pull request the remote already carries
goes through the one call here -- the dev-fix publication and the bounce
behind it, both validating recoveries, the conflict resolution, the
recovered-commit and clean-rebase conflict publications, the base-sync auto
rebase and its own crash recovery, and the final documentation pass -- because
the three steps are one decision and splitting them across nine seams is how
one of them ends up measuring and not pinning, or pushing and not clearing the
debt.

What the answer hands back is spent rather than merely obeyed: the push is
NAMED against the commit that was measured, so a checkout something moved
between the reading and the push publishes the measured commit rather than
whatever it became, and it is PINNED to the head the entry froze, so a pull
request somebody pushed to in that same window rejects the push instead of
being overwritten. Past the push the receipt and the debt are settled in one
durable write, and the checkout is proved again -- what went out is right, and
what can be wrong by then is the tree every stage behind this gate reads.

That one write is also where a rewrite's exemption finally moves. The permit
`late_transfer` granted licensed this push and rotated nothing, because the
commit it is about was on no remote when it was granted; here it is, so the
verdict, the identity beside it, and the phase that spends the permission go
down with the receipt that says the remote has it -- `late_rotation` owns
which of those the comment owes and this owner owns the write they ride.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace as _replace

from orchestrator.git import branch_transport as _branch_transport
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.stages.implementing import (
    late_parks as _parks,
    late_publication as _publication_gate,
    late_records as _records,
    late_rotation as _rotation,
    publication as _publication,
    state as _state,
)

log = logging.getLogger("orchestrator.workflow")


@dataclass(frozen=True)
class _PushedCandidate:
    """What one gated publication did with the candidate it was handed.

    Three answers rather than two, because each owes its caller something
    different. `held` is a tick this gate finished -- parked, or handed to the
    adjudication -- and the caller stops without pushing, relabelling, or
    announcing anything. `landed` is the push that went out, with the debt it
    paid already spent. Neither is the push that was allowed and then failed,
    which is the caller's own to park for: only it knows what a failed push
    means where it stands.

    `refused` narrows the first for the one caller that may publish on a
    rewrite permit and nothing else: the permit declined, nothing was
    measured, and the gate neither parked nor routed -- so the caller owns
    what happens next rather than being told the tick is over.
    """

    held: bool = False
    landed: bool = False
    refused: bool = False


def _publishes(
    gate: _records._Gate,
    branch: str,
    entered: _records._Entered = _records._UNENTERED,
) -> _PushedCandidate:
    """Measure this candidate, push what it earned, and spend what it paid.

    The whole of one gated publication behind a single call, because the three
    steps are one decision and splitting them across nine seams is how one of
    them ends up measuring and not pinning, or pushing and not clearing the
    debt. Every push onto a pull request the remote already carries goes
    through here: the dev-fix publication and the bounce behind it, both
    validating recoveries, the conflict resolution, the recovered-commit and
    clean-rebase conflict publications, the base-sync auto rebase and its own
    crash recovery, and the final documentation pass.

    `entered` is what the caller already established and this owner may not
    re-read: a stage a same-tick relabel wrote, and the head the caller pinned
    its own decision to. Both are frozen onto the record, so the push this
    tick makes and the one a settled adjudication makes later are pinned to
    the same fact.

    `entered.reconciling` says no developer ran on this tick, which is what
    tells a checkout that moved from a resumed developer's fresh commit -- the
    rebase and recovery seams are all of that kind.

    The caller's terms are applied to the subject once, here, so every step
    below is about the same one. The answer half replaces them again on its
    own copy for the entry it freezes; what that copy cannot carry back is
    what the steps PAST it need -- the fields a landed push closes, which ride
    the receipt's own write.

    Past the push there is one durable write and the proof is taken ahead of
    it, so what it says goes down WITH the receipt rather than behind it.
    Settled the other way round -- receipt first, proof after -- a process
    dying in between comes back to a published branch, a paid debt, and
    nothing on the record owing the checkout a proof: the stage below reads a
    dirty worktree as no stranded work and hands a reviewer a checkout nobody
    read. The window that remains is the one before that write, and it is
    recoverable rather than silent: every push that MOVES its publication
    records the debt for it beforehand, so a crash there leaves an approval
    the reconciliation ahead of the next handler pays as a leased no-op and
    then re-proves here.
    """
    gate = _replace(
        gate,
        candidate=entered.candidate,
        spends=entered.spends,
        rewrite=entered.rewrite,
        permit_only=entered.permit_only,
    )
    published = _publication_gate._holds_published_work(gate, entered)
    if published.held:
        return _PushedCandidate(held=True, refused=published.refused)
    published = _repinned(published)
    if not _pushed(gate, branch, published):
        return _PushedCandidate()
    # The proof comes first and its answer rides the settlement's own write,
    # so no window exists in which the branch is published and the record
    # owes the checkout nothing.
    unproven = _unproven_checkout(gate, published.revision)
    _publication_paid(gate, published, unproven)
    if unproven:
        return _PushedCandidate(held=True)
    return _PushedCandidate(landed=True)


def _repinned(
    published: _publication_gate._PublishedCandidate,
) -> _publication_gate._PublishedCandidate:
    """Lease a publication the remote already carries against itself.

    Whether the pull request is standing on this exact commit is asked of the
    REMOTE rather than of the record, because the record is a local note about
    a push and the question is whether the push is still what the pull request
    has. Between a push that landed and the caller's own pinned write there is
    a whole tick's worth of work -- watermarks, a round, a relabel -- and a
    process that dies in it comes back to the same checkout and the same
    commit. Read off the local receipt alone, a publication somebody moved in
    the meantime would answer "landed" for a pull request that no longer
    carries the work, and the caller would go on to hand a reviewer a head
    that is not there. So the head this tick froze IS the answer, whatever any
    record does or does not say about it.

    What it changes is the LEASE and nothing else. A push whose commit the
    pull request already has nothing left to SEND -- git answers "everything
    up-to-date" -- so what it is for is the one thing no local record can
    supply: proof, taken atomically at the remote, that the publication this
    tick froze is still the publication the pull request has. Leased against
    the commit itself, a branch somebody pushed to between the freeze and here
    rejects the request instead of answering it, and the caller stops rather
    than settling a debt and handing a reviewer a head that is not there.

    Skipping the push entirely is what a bare record could do, and it fails
    open on both sides of the same window: the receipt is settled against a
    remote nothing re-read, and the steps behind the push -- the receipt's own
    write and the proof that the checkout is still standing on what went out
    -- never run at all. So the no-op goes through the ordinary tail instead
    of around it, pinned to the head the pull request is on NOW rather than to
    the head an approval was measured against, which the landed push has
    already moved off.

    Named exactly, so work committed on top of the published commit is the
    fresh candidate it is and gets the reading it is owed -- and every other
    candidate is handed back exactly as the gate answered it, including one
    the switch kept out of the gate, where nothing was frozen, nothing is
    known about the remote, and the push resolves it for itself.
    """
    if not published.revision or published.standing != published.revision:
        return published
    return _replace(published, lease=published.standing)


def _unproven_checkout(gate: _records._Gate, published: str) -> bool:
    """Refuse the handoff where the checkout stopped being what was pushed.

    The window the pre-push proof cannot cover, and the same one the initial
    publication asks about on the far side of its own push -- so it is asked
    with that owner's two questions rather than a second pair worded here.
    The push is a request and the worktree is writable while it runs: a
    descendant an agent or a cleanup left, or an unstaged edit beside it, is
    enough. What went out is exactly the commit that was named, so the branch
    and its pull request are right; what is wrong is the CHECKOUT, and the
    checkout is what every stage behind this gate works from -- the reviewer
    reads a head ahead of the pushed branch as unpushed work, the squash
    rewrites what is on it, the docs pass commits on top.

    So the publication stands and the HANDOFF stops: the caller is told the
    tick is finished, and it relabels nothing, announces nothing, and spends
    no round.

    It writes nothing, and that is what makes it crash-safe. The answer rides
    the settlement's own write behind it, so a refusal and the receipt it
    refuses over land together or not at all -- there is no window in which
    the branch is published, the debt is paid, and nothing on the record owes
    the checkout a proof. Written by the refusal alone, one step behind that
    settlement, that window exists and is silent: a process dying in it comes
    back to an issue whose stage reads a dirty worktree as no stranded work
    and hands a reviewer a checkout nobody read.

    Asked only where the push had a commit to be about. A checkout that could
    not name its own head sent whatever the branch was when git ran, so there
    is nothing this proof could compare it to -- and compared against nothing
    it reads every checkout as moved, parking an issue whose head is exactly
    where it was left and holding the handoff its push just earned.
    """
    if not published:
        return False
    moved = _publication._moved_after_the_push(
        gate.gh, gate.issue, gate.state, published, gate.worktree,
    )
    if moved:
        return True
    return _publication._dirtied_after_the_push(
        gate.gh, gate.issue, gate.state, published, gate.worktree,
    )


def _pushed(
    gate: _records._Gate,
    branch: str,
    published: _publication_gate._PublishedCandidate,
) -> bool:
    """Push exactly the commit the gate let through, onto the head it froze.

    The one place the two frozen commits are spent, so no seam past the gate
    has to remember to spend them. What it closes is the pair of races either
    half alone leaves open: a checkout that moved after the reading publishes
    the measured commit rather than whatever it became, and a pull request
    somebody pushed to in the same window rejects the push instead of being
    silently overwritten by work measured against the head it used to be on.

    A caller with a head of its OWN is pinned to that one -- the conflict and
    base-sync publications each read the remote for themselves -- because the
    entry froze what they established rather than what this owner would have
    re-read. Where the gate froze nothing at all the push reads the remote
    itself, which is the behavior an install with the switch off keeps.
    """
    return _branch_transport._push_branch(
        gate.spec, gate.worktree, branch,
        revision=published.revision or None,
        force_with_lease=published.lease or None,
    )


def _publication_paid(
    gate: _records._Gate,
    published: _publication_gate._PublishedCandidate,
    unproven: bool,
) -> None:
    """Settle what a push that landed paid, and what it still owes.

    The approval a small candidate earns says one commit is still owed a
    publication, and it is written before the push precisely so a tick that
    died in between leaves something on the issue naming the work. Past a push
    that landed there is no debt, and a record left standing would freeze this
    branch out of the ordinary base refresh for as long as the issue lives --
    while every later tick asks for a checkout back for work nothing is going
    to publish. Spent here rather than after the relabel, for the reason the
    initial publication spends it there: past the relabel the issue may belong
    to another stage entirely.

    Written HERE rather than left for the caller's own write, because the
    effect it answers for has already happened: the branch is on the remote,
    and every caller has work still to do between this and its pinned write.
A process that died in that window would leave a paid debt standing,
    which the reconciliation ahead of the next handler answers as the leased
    no-op it is -- the pull request already carries the commit, so there is
    nothing to send and what the republication buys is the receipt and the
    proof this write carries. That is the whole of what makes the one window
    left here recoverable rather than silent: every push that MOVES its
    publication records the debt for it beforehand.

    The receipt rides the same write, and it is the other half of the same
    guarantee: dropping the debt says nothing is owed, and only the receipt
    says WHY. Past this write the caller still has a tick's worth of work to
    do, and a process that dies in it comes back to a commit no record calls
    published -- so the gate would measure it again, against a base that has
    moved and a ceiling that may have been retuned, and could route a change
    the pull request already carries to adjudication. It names one commit and
    only it, so work committed on top gets the reading it is owed.

    What the caller owed rides the same write, and for EVERY caller rather
    than only the reconciliation. Past this write the approval is gone and so
    is the generation it was granted under, so nothing is left on the comment
    for a later tick to restore a round, a consumed fix batch, a docs receipt,
    or a resolved conflict outcome from -- while the caller still has a
    relabel and a write of its own to make. A process dying in that window
    would come back to a published commit, a label already moved on, and
    bookkeeping frozen at what the tick BEFORE the push had. Applied here it
    is one durable write: the publication, the debt it paid, and everything
    the route owed for it land together or not at all.

    The caller applies the same frozen pairs again once this call returns, and
    that is a no-op rather than a second increment: what it hands in is the
    value it computed BEFORE the push, so re-applying writes the same value
    twice. It is not redundant either -- a push whose commit nothing could
    name never reaches this write, and the caller is what closes the round
    there.

    Silent where the pinned comment already says all of it, which is what a
    retry over a publication the remote is already standing on reads: the
    receipt names this commit and no debt is left, so the tick that landed it
    closed what its route owed in this very write and a second application
    would count the same round twice. Silent too where nothing could name the
    commit at all: a receipt is a claim about one object id, and an empty one
    records that a push nobody can identify reached the remote.

    `unproven` is what the proof ahead of this call answered, and it is the
    one thing that breaks that silence: a checkout that stopped being what
    went out owes a CLAIM -- the whole approval, both halves the landed commit,
    since that is the head the pull request stands on now -- and it goes down
    here rather than one write later so the publication and the debt it owes
    the checkout land together. Recorded whole, the reconciliation ahead of
    the next handler reads it as the debt it is: restore the checkout and that
    tick republishes as a leased no-op onto the head this push left the branch
    on, re-proves it, and settles, with nothing re-run and nothing re-measured.
    A commit with no head to pin it against would be half a claim, which that
    owner refuses as damage and parks under a reason only a human clears.

    The exemption a REWRITE earned rides the same write, and it is the one
    thing here that is not about this issue's debt at all. `late_transfer`
    granted a permission for this push and moved nothing, because the commit
    it names was on no remote then; it is on one now, so the verdict, the
    identity beside it, and the phase that spends the permission go down with
    the receipt that says so. Settled one write later, a process dying in
    between would leave the pull request carrying a commit no exemption covers
    -- and the next push would measure a change a human already ruled on past
    the same ceiling and route an approved branch back into adjudication.

    That rotation is asked whether or not anything else here is owed, because
    it is a different question with a different answer: a comment whose
    receipt already names this commit still owes the move if the write that
    should have carried it was lost, and a settlement that skipped it there
    would never come back for it.
    """
    if not published.revision:
        return
    landed = published.revision
    rotation = _rotation._rotates_the_exemption(gate, published)
    settling = _owes_a_settlement(gate.state, landed)
    if not (settling or unproven or rotation.staged):
        return
    if settling:
        # Read before the drop below takes it: the accepted road keeps the
        # head this push replaced on the approval's own lease.
        superseded = (
            gate.entry.published_sha if gate.entry
            else _parks._approved_lease(gate.state)
        )
        _records._spend(gate.state, gate.spends)
        _parks._forget_approval(gate.state)
        _parks._record_publication(gate.state, landed, superseded)
    if unproven:
        _parks._approve(gate.state, landed, landed)
    gate.gh.write_pinned_state(gate.issue, gate.state)
    _rotation._reports_the_transfer(gate, rotation)


def _owes_a_settlement(state: PinnedState, published: str) -> bool:
    """Whether the pinned comment still disagrees with the push that landed.

    Two ways it can. The receipt may name a different commit. And a debt may
    still say this one is owed a push -- which is what every push that MOVES
    its publication leaves behind before it runs, measured or not, so the
    route bookkeeping riding this write is reached whenever there was a window
    to lose it in.

    The approval halves are read RAW rather than through the fail-closed
    readers beside them: a hand-edited value is not an approval, but it is
    still something on the comment that the write below clears.

    Neither is true of a push that found the pull request already standing on
    the commit. It had nothing to send, no debt was recorded for it, and the
    tick that really published it closed the same things in this very write --
    so a second application here would count the same round twice.
    """
    if _parks._published_commit(state) != published:
        return True
    return any(
        state.get(key) is not None
        for key in (_state._APPROVED_SHA, _state._APPROVED_LEASE)
    )
