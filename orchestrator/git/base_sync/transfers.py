# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a rebase replaced, and how far the transfer of its exemption got.

A clean base rebase is a REWRITE of whatever the branch was standing on, and
on an issue whose exemption names that commit the replay is the one thing that
would punish it: the exemption names one commit and only it, so the object the
replay produced is measured past the same ceiling and the change a human
already adjudicated goes back into adjudication with a pull request open over
the work. What stops that is a permit, and a permit is granted on EVIDENCE --
the pair the adjudication recorded, the pair the replay produced, and the
publication the push is made against. This owner assembles that evidence, and
it sits beside the publisher rather than inside it because the tick that made
the rewrite is not the only one that needs it.

The refresh pins its recovery anchor before git runs, rebases, records the
permission, pushes, and receipts that push -- five durable moments with four
windows between them, and a process can die in any of them. What the next tick
comes back to is a checkout on the replay and a pinned comment that got as far
as it got, so before a recovery may finish anything it has to know which of
those it is looking at. That is the second half of this owner, and the answers
are closed:

* `NOTHING` -- no verdict is being carried here at all. The ordinary
  interrupted rebase, which the ordinary recovery finishes.
* `UNRECORDED` -- the replay is on the branch and no permission was ever
  written. The grant was still ahead of the crash, so the record cannot supply
  the evidence and the recovery assembles it exactly as the interrupted tick
  would have.
* `OUTSTANDING` -- a permission stands for this very head, the debt written
  with it agrees, and the push it licensed has not been receipted. The record
  IS the evidence, re-asked in full rather than believed, and the receipt is
  what this recovery still owes.
* `SETTLED` -- the receipt landed and the exemption is already on the head.
  Nothing is left to move, and a recovery that moved anything here would be
  making a second claim about a transfer one write already finished.
* `UNVOUCHED` -- a group is standing that this build cannot read whole, one
  claiming a commit that is not the head in hand, or one whose paired debt
  disagrees with it. Nothing is assembled and nothing is settled, and the
  recovery parks rather than letting the ordinary gate measure a change a
  human already ruled on -- which is the answer a permission nobody can check
  has to get.

Only `UNRECORDED` is handed fresh evidence, and that asymmetry is the safety
rule. A grant REPLACES the whole authorization group rather than adding beside
it, so a recovery that assembled a claim of its own over a group already
standing would repair a record nobody checked, under the authority of the very
transfer it is in the middle of deciding.

The last questions here are the ones the roads that publish nothing new have
to ask. Whether the rewrite the pull request already carries is one this
comment can ACCOUNT for -- finishing that road clears the recovery anchor, and
the anchor is the only thing that brings the tick back, so an exemption still
on the old commit, a debt nothing paid, or a receipt nobody wrote may not be
walked past however right the remote looks. Whether the record says a replay
reached a remote that no longer has it, which is somebody's rollback and not
this attempt's unfinished push -- and the head they rolled back to is the very
head a retry would lease itself against. And whether the permit still LICENSES
a settlement, asked before the gated publication rather than through it,
because the gate's answer to a declining permit is the ordinary cumulative
reading and there is nothing on that road to measure.
"""
from __future__ import annotations

from dataclasses import replace as _replace
from enum import StrEnum

from orchestrator.git.base_sync.models import (
    _AutoRebaseContext,
    _AutoRebaseRecoveryContext,
    _PendingRewrite,
)
from orchestrator.git.base_sync.state import (
    _PENDING_ANNOUNCED_SHA,
    _PENDING_REWRITE_PR,
    _PENDING_REWRITE_SHA,
    _PENDING_REWRITE_STAGE,
    log,
)
from orchestrator.git.measurement import commits as measurement_commits
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.state import (
    WorkflowLabel,
    publishes_onto_a_pull_request,
)


class _Handoff(StrEnum):
    """How far the exemption an interrupted rebase was carrying got."""

    NOTHING = "nothing"
    UNRECORDED = "unrecorded"
    OUTSTANDING = "outstanding"
    SETTLED = "settled"
    UNVOUCHED = "unvouched"


# Every key one attempt's record of its own replay goes down as, so a reader
# can tell a comment carrying none of them from one a hand edit or a
# half-finished write took a member out of.
_PENDING_REWRITE_KEYS = (
    _PENDING_REWRITE_SHA, _PENDING_REWRITE_PR, _PENDING_REWRITE_STAGE,
)


# The two handoffs a landed rewrite can be ACCOUNTED for under: the transfer
# finished, or none was ever granted and the ordinary gate published it. Both
# leave a receipt naming the commit, which is what the accounting is read off.
_ACCOUNTABLE = frozenset((_Handoff.SETTLED, _Handoff.UNRECORDED))


# Why a rewrite the pull request already carries is one the pinned comment
# cannot account for. Each is worded for the operator who has to reconcile it,
# because what they all end in is a park nobody but a human clears.
_UNREADABLE_CLAIM = (
    "a transfer record this build cannot read whole is standing over the "
    "commit this issue exempts"
)

_UNSETTLED_CLAIM = "the transfer standing here is `{handoff}` rather than over"

_UNRECEIPTED = (
    "no whole receipt on the pinned comment records `{published}` as pushed "
    "from `{anchor}`, so whether the verdict this rebase was carrying ever "
    "moved cannot be said"
)

_UNPAID = (
    "a push is still recorded as owed for `{owed}`, so the write that should "
    "have settled this publication did not land whole"
)


def _rewritten_by_the_rebase(
    context: _AutoRebaseContext | _AutoRebaseRecoveryContext,
    before_sha: str,
    after_sha: str,
    publication: _PendingRewrite | None = None,
):
    """What this rebase replaced, as the evidence a transfer is granted on.

    Assembled here because this is the one place both halves of the claim are
    in hand at once. The pair the contribution came FROM is the one the
    adjudication already recorded -- the commit a human ruled on and the base
    it was measured over -- and it is the only pair a verdict may be moved off
    and the only one a later reader re-derives the equality from. The pair it
    went TO is this rebase's own: the head the replay left, which moves with
    the next commit, and the base the branch now sits over, which is what the
    remote says its base branch is at.

    Nothing is decided here. Whether the two pairs are one contribution is the
    gate's question, asked over fingerprints taken from the objects
    themselves, and every other term this hands over is re-asked there against
    the publication the gate froze for itself.

    The base is frozen from what the REMOTE says the branch is at rather than
    read off the local ref the rebase named, and that is the whole of what
    keeps the pair honest. `refs/remotes/<remote>/<base>` lives in the object
    store the issue's agent writes to and any worktree sharing it can repoint
    -- after this tick's fetch, at that. A replay onto a ref carrying work the
    remote does not have fingerprints as the little that sits on top of it,
    while the pull request against the real base carries that work and this
    change together, and the permit would wave the pair through unmeasured. Read
    from the remote, the forged base is simply a different contribution and the
    ordinary cumulative gate measures it -- which costs one authenticated read
    on the rare tick an exempt issue is rebased.

    Empty where either half cannot be shown, and that is a claim withheld
    rather than a refusal reported. A comment with no semantic record -- an
    issue that never earned an exemption, one written before the record
    existed, one whose fingerprint could not be taken -- has no accepted pair
    to name, and a base the remote would not name, or one this host does not
    hold even after a fetch, is no commit to read a contribution over. In
    both the rebase goes to the ordinary cumulative gate and is measured like
    any other candidate.

    The PUBLICATION is the caller's where it hands one in, and that is the
    difference between the tick that makes the rewrite and the tick that comes
    back to it. The publisher is making the rewrite now, so the pull request
    and the stage it is entered on are the ones it is looking at. A recovery
    is not: taking them from the issue as it reads on the tick AFTER a crash
    would compare today with today, and a relabel or a repoint made while the
    process was down would pass as the terms the dead tick made its rewrite
    under -- which is exactly what the permit's publication checks exist to
    catch.

    The pre-rebase anchor goes down as the LEASE rather than as the commit
    that was replaced, and the two are deliberately kept apart. It is the head
    the pull request is standing on and the head the force-push behind this is
    pinned to; that it is also the commit the exemption names is what the
    equality of the two contributions proves, and not something this owner may
    assert by spelling one field from the other.
    """
    # Lazy import: the exemption record and the rewrite vocabulary sit in the
    # workflow layer above this package, so binding them at module load would
    # make every git-side import pay for the stage tree they pull in.
    from orchestrator.workflow.late_split import (
        exemption as _exemption,
        rewrites as _rewrites,
    )
    identity = _exemption.read_semantic_identity(context.state)
    if identity is None:
        return None
    replayed_onto = measurement_commits._freeze_base_commit(
        context.spec, context.worktree,
    )
    if not replayed_onto.is_frozen:
        return None
    made_against = publication or _PendingRewrite(
        sha=after_sha, pr_number=context.pr_number, stage=context.label,
    )
    return _rewrites.LateRewrite(
        kind=_rewrites.LateRewriteKind.AUTO_CLEAN_REBASE,
        from_sha=identity.candidate_sha,
        from_base_sha=identity.base_sha,
        to_sha=after_sha,
        to_base_sha=replayed_onto.sha,
        pr_number=made_against.pr_number,
        source_stage=made_against.stage,
        lease=before_sha,
    )


def _carried_by(
    context: _AutoRebaseRecoveryContext, local_head: str,
) -> _Handoff:
    """How far the transfer this interrupted rebase was making got.

    Read off the pinned comment alone, and asked before any of the recovery's
    own effects, because it is what decides which of them the tick owes: a
    permission still outstanding is a receipt this recovery has to land, one
    already spent is a route it only has to finish, and a rewrite the grant
    never reached is evidence it has to assemble for itself.

    Asked of the PERMISSION rather than of the commit the exemption names, for
    the reason every other reader of this record is: the grant writes the
    permission and the debt in one write for one commit, so a group whose
    target somebody edited would otherwise be invisible here and the recovery
    would carry on as though no transfer had ever been in flight.

    Asked of the EXEMPTION first, before any permission standing beside it is
    believed. A permission is a claim about moving one verdict, and what says
    which verdict is the exemption and the identity under it -- so a group
    something damaged after the grant went down leaves a permission that still
    reads back whole over a verdict nothing can name. Believed there, the
    settlement re-asks a permit whose accepted contribution cannot be
    fingerprinted, the ordinary gate measures the replay instead, and a change
    a human already ruled on is published and announced as though it had been.

    Asked by PRESENCE where the record has to be absent for the answer to be
    `NOTHING`, which is the difference between an issue that never earned a
    verdict and one whose record something damaged. The fail-closed readers
    answer both with a bare None -- rightly, since the gate's only move is to
    measure -- and a recovery that took that for "no verdict in flight" would
    finish a route over an exemption still naming the commit a human ruled on.
    So a comment claiming an exemption it cannot show whole is `UNVOUCHED`,
    and only a comment claiming none at all is `NOTHING`.

    The settlement PROOF is asked the same way and for the same reason. It is
    written by the statement that settles a transfer and dropped by the write
    behind the record it feeds, so one standing beside a phase, a reading, or
    an authorization this build cannot account for is a checkpoint saying two
    things at once. It is invisible to the readers above -- they answer for
    the permission, not for what was reported about it -- and the road it
    would let through clears the anchor and files no record of the move.

    Costs no git and no request. Every answer is a field this issue already
    carries, which is what lets the ordinary recovery -- the overwhelming
    majority, on issues that never earned an exemption -- pay nothing for a
    question that is not about it.
    """
    # Lazy for the reason every upward reach in this package is: the record
    # sits in the workflow layer above it.
    from orchestrator.workflow.late_split import (
        exemption as _exemption,
        rewrites as _rewrites,
    )
    if _exemption.unreadable_exemption(context.state):
        return _Handoff.UNVOUCHED
    if _rewrites.stranded_transfer_proof(context.state):
        return _Handoff.UNVOUCHED
    standing = _standing_permission(context, local_head)
    if standing is not None:
        return standing
    if _exemption.read_exemption(context.state) is None:
        return _Handoff.NOTHING
    return _Handoff.UNRECORDED


def _standing_permission(
    context: _AutoRebaseRecoveryContext, local_head: str,
) -> _Handoff | None:
    """What a permission already on the comment says about this head, or None.

    None where the comment carries no claim this recovery has to answer for,
    and a transfer that is OVER is the larger half of that. A settled record
    is never cleared, so it outlives the attempt that earned it: the exemption
    it moved is on the rewritten commit, the pull request is standing on that
    commit, and the next base advance anchors its own rebase to exactly that
    head. A settled record whose `to_sha` IS this attempt's anchor is
    therefore the previous rotation's history rather than a claim about this
    one -- and read as a claim it would be bound by a lease belonging to that
    earlier attempt, come back as a group nobody can vouch for, and park an
    attempt that has not started. So it is passed over, and the evidence for
    THIS replay is the recovery's to assemble like any other. A settled record
    naming any other commit is passed over for the plainer reason that the
    head in hand is not the one it is about.

    Everything else is a claim, and a claim is believed only where every field
    of it agrees with the attempt this recovery is finishing. A group this
    build cannot read back whole, one still outstanding for some other commit,
    one whose DEBT does not agree with it, and one whose publication, lease,
    or contribution belongs to some other attempt are all refused rather than
    replaced: the group is the only account there is of how the exemption came
    to name what it names, and a recovery that acted on one it could not tie
    to the attempt in front of it would be finishing somebody else's work.
    """
    # Lazy for the reason every upward reach in this package is: the record
    # sits in the workflow layer above it.
    from orchestrator.workflow.late_split import rewrites as _rewrites
    if not _rewrites.carries_rewrite_authorization(context.state):
        return None
    authorization = _rewrites.read_rewrite_authorization(context.state)
    if authorization is None:
        return _Handoff.UNVOUCHED
    settled = _is_settled(authorization)
    rewrite = authorization.rewrite
    if settled and rewrite.to_sha == context.pending_pre_rebase_sha:
        return None
    if rewrite.to_sha != local_head:
        return None if settled else _Handoff.UNVOUCHED
    return _claimed_by_this_attempt(context, authorization)


def _is_settled(authorization) -> bool:
    """Whether this record says the receipt behind its push has landed."""
    from orchestrator.workflow.late_split import rewrites as _rewrites
    return authorization.phase == _rewrites.LateRewritePhase.PUBLISHED


def _claimed_by_this_attempt(context, authorization) -> _Handoff:
    """What a whole permission for the head in hand is, once it is bound.

    Whole and about this commit is where the fail-closed reader stops; which
    ATTEMPT it belongs to is what the two questions here add, and both have to
    be answered before a recovery acts on it.
    """
    if not _made_by_this_attempt(context, authorization):
        return _Handoff.UNVOUCHED
    if _is_settled(authorization):
        return _Handoff.SETTLED
    return _outstanding_or_unvouched(context.state, authorization.rewrite)


def _made_by_this_attempt(context, authorization) -> bool:
    """Whether every term of this permission belongs to the attempt in hand.

    The reader above proves the group is WHOLE and that its rewritten end is
    the commit on this checkout. Whole is not the same as this attempt's: each
    field is individually well-shaped, and a comment where the publication,
    the leased head, or the digest came from somewhere else reads back exactly
    as cleanly as one that did not. Acted on, the recovery finishes -- clears
    the anchor, posts a notice, files an event -- over a transfer it cannot
    tie to the rebase it is recovering.

    So the record is cross-bound to the two things that CAN say which attempt
    this is. The pending record names the pull request and the stage the
    rebase was made for, and the anchor names the head its force-push was
    leased against; those are the three the permit itself is scoped by. And
    the semantic identity names the pair the digest was taken between -- the
    accepted one while the transfer stands, the rewritten one once the receipt
    has moved it -- so a target base or a fingerprint from another reading is
    a contribution this issue never adjudicated.

    Silent about the attempt's own record where there is none: the window
    before that write leaves nothing to bind to, and the roads behind this one
    already refuse to publish anything they cannot show the terms of.
    """
    rewrite = authorization.rewrite
    recorded = context.pending_rewrite
    if rewrite.lease != context.pending_pre_rebase_sha:
        return False
    if recorded.is_recorded and (
        rewrite.pr_number, rewrite.source_stage,
    ) != (recorded.pr_number, recorded.stage):
        return False
    return _names_the_adjudicated_pair(context, authorization)


def _names_the_adjudicated_pair(context, authorization) -> bool:
    """Whether the digest and the pair this record carries are the issue's.

    The end the phase binds is the one the semantic identity has to name --
    the accepted commit and the base it was measured over while the transfer
    stands, the rewritten commit and the base it was replayed onto once the
    receipt has moved it -- and the digest between them is the one the grant
    was taken over. A record carrying a pair or a digest from another reading
    describes a contribution this issue never adjudicated, however well each
    field is shaped on its own.
    """
    from orchestrator.workflow.late_split import exemption as _exemption
    identity = _exemption.read_semantic_identity(context.state)
    if identity is None or identity.fingerprint != authorization.fingerprint:
        return False
    rewrite = authorization.rewrite
    named = (
        (rewrite.to_sha, rewrite.to_base_sha) if _is_settled(authorization)
        else (rewrite.from_sha, rewrite.from_base_sha)
    )
    return (identity.candidate_sha, identity.base_sha) == named


def _outstanding_or_unvouched(state: PinnedState, rewrite) -> _Handoff:
    """A permission for the head in hand, held to the debt written with it.

    The grant is ONE write of two records for one commit: the permission that
    says what a push may carry a human's verdict over, and the debt that says
    the push is owed and what it is pinned to. They are written together
    precisely so a reader can hold each to the other, and this is that reader.

    A permission standing beside a debt that names another commit, another
    lease, or nothing at all is a comment something took apart. Read as
    outstanding, the settlement re-asks the permit -- and a permit that grants
    RE-WRITES both records, so the missing half would be reconstructed from
    the very claim nobody could check and the push would go out under it. So
    the pair is asked before the handoff is called outstanding, and a
    disagreement is the refusal every other unvouchable record here gets.

    Read through the same fail-closed readers the debt's own owner uses: a
    hand-edited value is no approval, which is exactly the disagreement this
    is looking for.
    """
    # Lazy for the reason every upward reach in this package is: the debt
    # sits in the workflow layer above it.
    from orchestrator.workflow.stages.implementing import late_parks
    owed = late_parks._approved_commit(state) == rewrite.to_sha
    if owed and late_parks._approved_lease(state) == rewrite.lease:
        return _Handoff.OUTSTANDING
    return _Handoff.UNVOUCHED


def _reconstructed(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
    carried: _Handoff,
):
    """The evidence a recovery hands the gate where the grant never landed.

    The one window the record cannot answer for itself. An interrupted tick
    rebased and died before the write that would have said what the replay
    replaced, so the reissued push reaches the gate with nothing on the
    comment naming a rewrite -- and the ordinary cumulative gate measures a
    change a human already ruled on past the same ceiling, with a pull request
    open over the work.

    Assembled from exactly the readings the interrupted tick would have taken:
    the pair the adjudication recorded, the head the checkout is standing on,
    the base the REMOTE names, the pinned anchor as the lease, and the pull
    request and stage that tick recorded making its rewrite against. Those
    last two are the dead tick's own rather than this one's, and they have to
    be: the permit checks them against the publication it freezes for itself,
    so terms taken from the issue as it reads now would compare today with
    today and adopt a relabel or a repoint made while the process was down.

    Nothing at all is assembled where the record names some OTHER commit, or
    where the terms it was made under cannot be read back: evidence made up to
    fill either gap is the one thing this owner may not offer the permit, and
    a publication taken from the issue as it reads on the tick after a crash
    is exactly that.

    A record carrying the terms and no head is assembled for, and it is the
    one place the head in hand is not the record's own claim. That is the
    window between git returning and the write that names what it produced:
    the terms went down with the anchor, so which publication the attempt was
    for is known, and the head is offered to the permit to be proved by what
    it contributes against the pair a human ruled on. The permit refusing is
    what that road is held by -- nothing here asserts the checkout is the
    replay, it hands over the claim for checking.

    None for every other handoff, each for its own reason: a permission still
    outstanding IS the evidence and is re-asked over the terms the grant was
    taken on, a spent one describes a transfer that is over, one nobody can
    vouch for may not be replaced by a claim this owner made up, and an issue
    carrying no verdict has nothing to carry.
    """
    if carried != _Handoff.UNRECORDED:
        return None
    if not _vouches_for_the_checkout(context.pending_rewrite, local_head):
        return None
    rewrite = _rewritten_by_the_rebase(
        context, context.pending_pre_rebase_sha, local_head,
        publication=context.pending_rewrite,
    )
    if rewrite is not None:
        log.info(
            "issue=#%d auto-rebase recovery: the interrupted tick left %s on "
            "the branch and no permission for it; re-deriving what the replay "
            "of %s contributes so the gate rules on the transfer it would have",
            context.issue.number, local_head[:8], rewrite.from_sha[:8],
        )
    return rewrite


def _vouches_for_the_checkout(
    recorded: _PendingRewrite, local_head: str,
) -> bool:
    """Whether this record may be the terms the head in hand is decided on.

    Two records may, and they are the two ends of one write. A record naming
    this exact commit is the ordinary one: the attempt got past `git rebase`
    and said what it produced. A record carrying the terms and no head at all
    is the window before that write, where nothing on the comment names any
    commit -- and the terms are still the dead tick's own, which is the whole
    of what the permit needs them for.

    Nothing else does. A record naming another commit says out loud that this
    checkout is not its work, and one whose terms cannot be read cannot say
    which publication anything was for.
    """
    if recorded.sha:
        return recorded.names(local_head)
    return recorded.is_declared


def _unaccounted_publication(
    state: PinnedState, local_head: str, anchor: str, carried: _Handoff,
) -> str:
    """Why a rewrite the pull request already carries is unexplained, or "".

    Asked of the road that has nothing left to publish and only a route to
    finish, and it is what decides whether finishing is safe. That route
    clears the recovery anchor, resets the review round, and hands the issue
    to the reviewer -- which on an issue carrying no verdict is exactly right
    and costs nothing, since there is no transfer for a missing record to
    strand.

    On an issue that WAS carrying one it is the opposite. The anchor is the
    only thing that brings this recovery back, so clearing it over an
    exemption still on the old commit, a debt nothing paid, or a receipt
    nobody wrote leaves the next tick to measure the rewrite as a fresh
    candidate -- past the same ceiling, and back into adjudication with the
    pull request already carrying the work. So those states park with the
    anchor left pinned instead, and a human settles what is on the comment.

    Two handoffs can be accounted for, and both by the same record: a transfer
    that FINISHED wrote the receipt in the same statement as the rotation, and
    a rewrite no permit ever licensed was published by the ordinary cumulative
    gate, which wrote one too. That receipt is read WHOLE -- the commit it
    names and the head it was pinned to, held against this recovery's own
    anchor -- because a receipt is never cleared and on its own goes on
    naming a commit this stage pushed rounds ago, vouching for any pull
    request somebody rewound onto it. The head is what dates it to THIS
    attempt, and the attempt is exactly what has to be accounted for. The debt
    beside it is asked as well: the two go down together, so an approval still
    standing over a receipted commit is a write that did not land whole.

    A group nobody can read is refused outright: it is the only account there
    is of how the exemption came to name what it names, and a route finished
    over it would be acting on evidence nothing checked.
    """
    if carried == _Handoff.NOTHING:
        return ""
    if carried not in _ACCOUNTABLE:
        if carried == _Handoff.UNVOUCHED:
            return _UNREADABLE_CLAIM
        return _UNSETTLED_CLAIM.format(handoff=carried)
    # Lazy for the reason every upward reach in this package is: the receipt
    # and the debt sit in the workflow layer above it.
    from orchestrator.workflow.stages.implementing import late_parks
    if late_parks._publication_from(state, anchor) != local_head:
        return _UNRECEIPTED.format(published=local_head, anchor=anchor)
    owed = late_parks._approved_commit(state)
    return _UNPAID.format(owed=owed) if owed else ""


def _rolled_back_publication(
    context: _AutoRebaseRecoveryContext,
    completed,
    carried: _Handoff,
) -> bool:
    """Whether the record says this replay reached a remote no longer on it.

    Asked once the pull request has been proved to be standing somewhere other
    than the commit the checkout carries, and it is the difference between a
    push that never went out and one somebody undid. Two records say the
    replay was there: a transfer that SETTLED, whose write says the exemption
    moved onto a commit the pull request really had, and the whole receipt
    beside it, which says the same thing for a replay no permit ever licensed.

    Either way the pull request has since been rolled back, and the head it
    was rolled back to is the very anchor a retry would lease its force-push
    against. That lease would be satisfied, the push would land, and the
    rollback would be gone -- which is the one outcome a lease exists to
    prevent. So it parks as the externally moved remote it is.

    Silent for every attempt whose push simply never went out, which is the
    ordinary interrupted rebase: nothing records a landing, so nothing here
    claims one.
    """
    # Lazy for the reason every upward reach in this package is: the receipt
    # sits in the workflow layer above it.
    from orchestrator.workflow.stages.implementing import late_parks
    landed = completed.local_head or ""
    if carried == _Handoff.SETTLED:
        return True
    receipted = late_parks._publication_from(
        context.state, context.pending_pre_rebase_sha,
    )
    return bool(landed) and receipted == landed


def _rotated_onto(state: PinnedState, local_head: str) -> bool:
    """Whether the record now says the verdict is on this commit.

    The one answer a settlement cannot take on trust from the call that made
    it. A permit granted before the gate is re-asked inside it, so anything
    that moved in between leaves the push landed and the verdict where it was
    -- and a landed push with no rotation behind it is a route this recovery
    may not finish, since finishing drops the anchor that would bring it back.

    Read as the whole record rather than as the phase alone, like every other
    reader of this group: a permission announcing itself published over fields
    nothing here understands has not been shown to have moved anything.
    """
    # Lazy for the reason every upward reach in this package is: the record
    # sits in the workflow layer above it.
    from orchestrator.workflow.late_split import (
        exemption as _exemption,
        rewrites as _rewrites,
    )
    authorization = _rewrites.read_rewrite_authorization(state)
    if authorization is None:
        return False
    if authorization.phase != _rewrites.LateRewritePhase.PUBLISHED:
        return False
    if authorization.rewrite.to_sha != local_head:
        return False
    return _exemption.is_exempt(state, local_head)


def _permits_the_publication(
    context: _AutoRebaseRecoveryContext, local_head: str, rewrite=None,
) -> bool:
    """Whether the permit still licenses this recovery to publish.

    Asked BEFORE the gated publication rather than through it, and that is
    the whole of what makes these roads safe. The gate's answer to a permit
    that declines is the ordinary cumulative reading, which is right for a
    rebase deciding whether to publish and wrong on a recovery twice over: a
    count under the ceiling reports a publication landed with the verdict
    still on the commit a human ruled on, and a count over it routes an
    adjudicated change into a second adjudication with a pull request already
    open over the work. There is nothing on either road to decide -- the
    remote has the commit, or the push it never made is already leased -- so
    the only question is whether the permission may be spent, and a refusal is
    a refusal. The gate is told the same thing on the way in, so a permit that
    stops holding between this ask and its own is refused there rather than
    measured.

    Asked over the evidence this recovery holds: the record the grant left,
    where there is one, and otherwise the rewrite re-derived for a grant the
    crash came before -- which is what `late_transfer` reads when a caller
    hands in no rewrite of its own. Every term is re-derived
    there: the publication this call freezes, the one the issue records, the
    checkout, the lease as an object this host holds, the issue read afresh,
    and both contributions fingerprinted from the objects themselves. A grant
    re-writes nothing, since the payload it would stage is the one already on
    the comment.

    The entry is frozen here for the same reason the permit needs one at all:
    it is the pull request read this tick, before any effect, and the terms
    the record claims are checked against it rather than against themselves.
    """
    # Lazy for the reason every upward reach in this package is: the permit
    # and the entry it is asked over sit in the workflow layer above it.
    from orchestrator.workflow.stages.implementing import (
        late_overflow as _overflow,
        late_records as _records,
        late_transfer as _transfer,
    )
    gate = _records._gate(
        context.gh, context.spec, context.issue, context.state,
        context.worktree,
    )
    entered = _records._Entered(
        head=context.pending_pre_rebase_sha or "",
        reconciling=True,
        candidate=local_head,
    )
    entry = _overflow._frozen_entry(gate, entered)
    if not entry.is_frozen:
        log.warning(
            "issue=#%d auto-rebase recovery cannot enter the publication its "
            "landed rewrite was made against (%s); the transfer it owes is "
            "left standing",
            context.issue.number, entry.refusal,
        )
        return False
    gate = _replace(
        gate, entry=entry, candidate=local_head, reconciling=True,
        rewrite=rewrite,
    )
    return bool(_transfer._carried_over(gate, local_head))


def _pending_rewrite(state: PinnedState) -> _PendingRewrite:
    """The record one interrupted attempt left of the replay it made.

    Read whole or not at all, like every other record this domain acts on: a
    pull request that is not an identity, a stage no publication is entered
    from, and a head that is not a whole git object id each answer as a record
    nobody may act on, which every caller reads as "cannot say" rather than as
    a fact about the world. The head is held to the same shape every other
    recorded commit here is, and for the same reason -- it is compared against
    one this tick read off a checkout, and a value that cannot be a commit
    would either never match or match something nothing ever wrote.

    Whole means something different for the TERMS and for the head, because
    they are written a `git rebase` apart. The terms go down with the anchor
    before the branch can move, so a comment carrying them and no head is not
    a group short of a member: it is an attempt this reader can date to the
    window between git returning and the write that records what it produced.
    A comment carrying a head with the terms missing is the other way round --
    nothing writes that order -- so it is damage.

    Which of the three absences a caller has is what travels back on the
    answer: one nobody ever wrote, one still in flight, or one something took
    apart.
    """
    # Lazy for the reason every upward reach in this package is: the shape a
    # recorded commit is held to is the late domain's own, and spelling it
    # twice is how a comment comes to accept what every other reader refuses.
    from orchestrator.workflow.late_split import formats as _formats
    recorded = state.get(_PENDING_REWRITE_SHA)
    number = state.get(_PENDING_REWRITE_PR)
    stage = _recorded_stage(state.get(_PENDING_REWRITE_STAGE))
    if not _formats.whole_number(number) or number <= 0 or stage is None:
        return _PendingRewrite(damaged=_claims_a_record(state))
    if recorded is None:
        return _PendingRewrite(pr_number=number, stage=stage)
    if not _formats.is_hex_of(recorded, _formats.COMMIT_LENGTHS):
        return _PendingRewrite(damaged=True)
    return _PendingRewrite(sha=recorded, pr_number=number, stage=stage)


def _claims_a_record(state: PinnedState) -> bool:
    """Whether a comment with unreadable terms claims an attempt anyway.

    Read by VALUE rather than by the key being there, because the write that
    ends an attempt blanks these fields rather than removing them: a group of
    nulls is the record nobody wrote, and a member carrying something beside
    one that does not is the record something took apart.
    """
    return any(
        state.get(key) is not None for key in _PENDING_REWRITE_KEYS
    )


def _recorded_stage(recorded: object) -> WorkflowLabel | None:
    """The stage a record names, or None where it names no publication.

    Held to the same predicate the permit holds its own evidence to -- the
    states that push onto a pull request the remote already carries -- so a
    record naming any other describes an attempt this workflow never made.
    """
    try:
        stage = WorkflowLabel(recorded)
    except ValueError:
        return None
    return stage if publishes_onto_a_pull_request(stage) else None


def _already_announced(state: PinnedState, local_head: str) -> bool:
    """Whether a finish already said what it published, for this commit.

    The mark a finish leaves between its announcement and its relabel, and the
    only thing that tells the last window of a finish from every earlier one.
    Everything else a finish does before its own write is invisible to a later
    tick: the notice is a comment, the audit event is on the sinks, and the
    label alone cannot say whose move it was.

    Held to the commit, like every other recorded id here -- which is why
    `_foreign_mark` beside this is what the roads that would announce
    something ask: a mark naming any other head, or naming no commit at all,
    is not an answer this reader may give as "nothing was announced".
    """
    recorded = state.get(_PENDING_ANNOUNCED_SHA)
    return bool(local_head) and recorded == local_head


def _foreign_mark(state: PinnedState, local_head: str) -> bool:
    """Whether a mark stands here that does not belong to the head in hand.

    Presence rather than truth, for the reason every other checkpoint on this
    comment is read that way. The mark is written by a finish naming what it
    had just published and dropped by the write that clears the attempt, so
    the key being there says a finish announced THIS attempt's replay -- and a
    value that is not that commit, or not a commit at all, is a checkpoint
    something took apart.

    Read as "not announced", such a mark costs the pull request a second
    notice and the stream a second `base_rebased` for one publication that
    happened once, with no way for a reader to tell which of the two describes
    the head the branch ended on. So it parks instead.
    """
    if state.get(_PENDING_ANNOUNCED_SHA) is None:
        return False
    return not _already_announced(state, local_head)


def _reports_a_lost_settlement(
    context: _AutoRebaseRecoveryContext,
) -> None:
    """Make the record a settled transfer never got to report.

    The last window a transfer has, and the only one whose evidence would be
    gone for good. The write that receipts a landed push is what settles the
    transfer, and the record of it goes to the sinks BEHIND that write -- so a
    process lost in between leaves a verdict that has moved and nothing on
    either sink saying so, over a proof no later reading could re-derive.

    So the settlement keeps that proof on the comment until the record is out,
    and this is where a recovery finding one still there makes it. Staged
    rather than written: the road that calls this has its own write coming,
    and the drop rides it.

    Silent for every comment that owes nothing, which is every recovery but
    the one that came back to this window.
    """
    # Lazy for the reason every upward reach in this package is: the record
    # and the sinks it goes to sit in the workflow layer above it.
    from orchestrator.workflow.stages.implementing import (
        late_records as _records,
        late_rotation as _rotation,
    )
    _rotation._reports_a_settled_transfer(
        _records._gate(
            context.gh, context.spec, context.issue, context.state,
            context.worktree,
        ),
    )


def _left_mid_transfer(state: PinnedState) -> bool:
    """Whether a permission on this comment still says a push is owed.

    Asked where a recovery is about to walk away from an attempt rather than
    finish it, and asked of the record alone: no fetch, no checkout, no
    comparison -- none of which the caller is on a road to make. What it needs
    to know is only whether walking away would leave a human's verdict
    licensed onto a commit no push carried, with the approval debt granted
    beside it still standing.

    Fail-closed in the same direction every reader of this record is. A group
    this build cannot read back whole answers yes, because "not shown to be
    over" is the only reading available to a caller deciding whether it is
    safe to forget one.
    """
    # Lazy for the reason every upward reach in this package is: the record
    # sits in the workflow layer above it.
    from orchestrator.workflow.late_split import rewrites as _rewrites
    return _rewrites.outstanding_permission(state)
