# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The single route from an interrupted auto-rebase to one terminal answer.

The verified facts arrive from ``snapshot`` and the answers live in
``outcomes``; what this owner adds is the order they are asked in, and that
order is the safety property. An ineligible label is answered before anything
is fetched, an unmoved HEAD falls back to the normal rebase flow before any
comparison is trusted, and equality with the remote is checked before the
ahead/behind counts are -- so the reissued force-push is only ever reached by
a head proven to be ahead of a remote the tick actually read. Anything else
parks. The legacy keyword signature is bound here too, because the flat
callers still pass the pre-context argument list this route derives its
context from.

What the remote says is only half of what an interrupted rebase has to be
classified by, because the rebase may have been carrying a human's verdict
onto the commit it produced, and because every road out of here ends in a
notice, an audit event, and the anchor dropped -- so the publication the
attempt recorded making its rewrite for is reconciled against the one this
issue holds now before any of them is taken, with this route's own last
relabel -- the one label it writes, and no other -- recognized rather than
refused. The counts are a fallback for one state alone -- a comment carrying
no record of a replay at all -- and the window between git returning and the
write that names one has a road of its own, where the head is proved by what
it contributes rather than by an id nobody wrote down. An issue moved off the
refresh-driven set is answered by what the attempt left rather than by the
label alone: an anchor over a checkout still standing on it is dropped, and a
recorded replay, an unspent permission, or a branch git has already moved
parks with every record intact, since no road runs here and a clear would
leave them asymmetrically stranded. Everything an
attempt DID record either vouches for the checkout in front of this tick or
parks it. ``transfers`` answers the other half off the pinned comment -- how
far the transfer's own writes got -- and the roads that still publish
something are handed it. A rewrite the grant never reached
is given re-derived evidence, so the replay is decided on the transfer the
dead tick would have asked for rather than measured past the same ceiling; a
push that landed with its receipt lost is settled here, on the stage the
permit was granted under, through the leased no-op that proves the pull
request really carries it. Every state neither of those covers -- a record
this build cannot read, a tree carrying uncommitted changes, a remote
somebody moved -- is fail-closed: the branch goes back onto the anchor, or
the anchor stays pinned, and a human is asked.
"""
from __future__ import annotations

import inspect
from typing import Any

from orchestrator.git.base_sync import (
    outcomes,
    persistence,
    publication,
    snapshot,
    transfers,
)
from orchestrator.git.base_sync.models import (
    _AutoRebaseRecoveryContext,
    _AutoRebaseRecoverySnapshot,
    _PendingRewrite,
)
from orchestrator.git.base_sync.state import _PR_REFRESH_DETOUR_LABELS
from orchestrator.git.verification import probes as verification_probes
from orchestrator.workflow.state import WorkflowLabel

# Why a landed rewrite's route could not be finished, in the operator's own
# terms. Spelled at the two seams that answer for them rather than beside the
# park, which takes whatever reason its caller established.
_LOOSE_TREE = (
    "the worktree carries {count} uncommitted change(s), so the contribution "
    "the transfer would be settled over is not the one the pull request has"
)

_UNPROVEN_LANDING = (
    "the pull request and the checkout agree on `{published}` and nothing "
    "this attempt recorded names it, so the publication in front of this tick "
    "is not one it can show it made"
)

_REFUSED_PERMIT = (
    "the permit that would license the settlement refused this tick -- the "
    "pull request, the stage, the checkout, the lease, or the two "
    "contributions no longer agree with the permission on the comment, and "
    "the orchestrator log names which"
)

_UNROTATED = (
    "the push went out and the verdict did not move with it, so the "
    "permission granted for `{published}` is still outstanding"
)

_REFUSED_NO_OP = (
    "the `--force-with-lease` no-op that would have recorded it, leased "
    "against that same commit, was refused -- the remote branch moved after "
    "this tick read it"
)

_FOREIGN_MARK = (
    "a finish on this attempt recorded that it had already announced some "
    "other commit, so whichever of the two this route said it published "
    "cannot be told from the comment"
)

# The two handoffs that say a grant is still standing over a commit the
# branch does not have: one this build reads as owed a push, and one it cannot
# read at all. A settled transfer is neither -- it is never cleared, so an
# issue that earned one would never look unstarted again.
_UNSPENT_TRANSFERS = frozenset((
    transfers._Handoff.OUTSTANDING, transfers._Handoff.UNVOUCHED,
))

_RECOVERY_SIGNATURE = inspect.Signature((
    inspect.Parameter("gh", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("spec", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("issue", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("state", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("worktree", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter("pr_number", inspect.Parameter.KEYWORD_ONLY),
    inspect.Parameter("label", inspect.Parameter.KEYWORD_ONLY),
    inspect.Parameter(
        "pending_pre_rebase_sha",
        inspect.Parameter.KEYWORD_ONLY,
    ),
    inspect.Parameter(
        "pending_rewrite",
        inspect.Parameter.KEYWORD_ONLY,
        default=_PendingRewrite(),
    ),
    inspect.Parameter("behind", inspect.Parameter.KEYWORD_ONLY, default=0),
    inspect.Parameter(
        "unparking_consumed_max",
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
    ),
))


def _retry_recovery_push(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
    carried: transfers._Handoff = transfers._Handoff.NOTHING,
    *,
    permit_alone: bool = False,
) -> bool:
    """Publish a verified ahead-only recovery head and finalize its state.

    Measured before it is published, like every other push onto a pull request
    the remote already carries: the head this recovery found is one an earlier
    tick rebased and never pushed, so nothing on this branch has been read
    against the base it now sits on.

    Unless the branch is standing on a rewrite of a commit an adjudication
    accepted, which is the one candidate that may be published without a
    reading. `carried` says how far the interrupted tick got with that
    transfer, and what this call owes it is the evidence: a permission the
    grant already recorded is what `late_transfer` re-asks the permit over,
    and a rewrite that never reached one is re-derived here so the replay is
    not measured past the same ceiling and adjudicated a second time with a
    pull request open over the work.

    Where there IS such a transfer, the permit is the whole of what may let
    this push out. It is asked before the gate and the gate is told the same,
    so a refusal is a refusal on both sides of that seam rather than a
    fall-through to the cumulative reading -- which on this road would
    force-push a replay nothing vouched for and clear the recovery with the
    verdict still on the commit a human ruled on. The rotation is read back
    afterwards for the same reason, since a permit that stopped holding
    between the two asks leaves the push landed and the verdict where it was.

    `permit_alone` says the caller holds no id vouching for this checkout at
    all -- the attempt was still in flight when the process died -- so the
    evidence is the only thing that can. Evidence that will not assemble parks
    there rather than falling through, because the fall-through is the
    ordinary cumulative reading and measuring a commit is not a way of
    establishing whose it is.
    """
    dirty_files = verification_probes._worktree_dirty_files(context.worktree)
    if dirty_files:
        return outcomes._park_dirty_recovery(
            context, recovery_snapshot, dirty_files,
        )
    rewrite = transfers._reconstructed(
        context, recovery_snapshot.head, carried,
    )
    licensed = rewrite is not None or carried == transfers._Handoff.OUTSTANDING
    if permit_alone and not licensed:
        return outcomes._park_unproven_replay_recovery(
            context, recovery_snapshot,
        )
    if licensed and not transfers._permits_the_publication(
        context, recovery_snapshot.head, rewrite,
    ):
        return outcomes._park_refused_permit_recovery(
            context, recovery_snapshot,
        )
    return _pushes_the_recovered_head(
        context, recovery_snapshot, rewrite, licensed,
    )


def _pushes_the_recovered_head(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
    rewrite,
    licensed: bool,
) -> bool:
    """Reissue the interrupted push and finalize what it earns.

    `licensed` says a transfer this recovery already knows about is the whole
    of what may let the push out, and it is passed to the gate as well as
    asked ahead of it: a permit that stops holding between the two asks is
    refused there rather than measured, and one that stops holding after the
    push leaves the verdict where it was, which the rotation read below
    catches.
    """
    landed = recovery_snapshot.head
    records = publication._gate_records()
    published = publication._gated_publication()._publishes(
        records._gate(
            context.gh, context.spec, context.issue, context.state,
            context.worktree,
        ),
        recovery_snapshot.branch,
        records._Entered(
            head=context.pending_pre_rebase_sha or "", reconciling=True,
            # The head this recovery verified against the remote and the one
            # the finalize below records as published. The gate proves the
            # checkout again, and a commit that landed between the two
            # readings would be the one pushed while the notice and the event
            # named this one -- so the candidate is bound and a moved checkout
            # refuses instead.
            candidate=landed,
            rewrite=rewrite,
            permit_only=licensed,
        ),
    )
    unfinished = _unfinished_recovery_push(
        context, recovery_snapshot, published, licensed,
    )
    if unfinished is not None:
        return unfinished
    return persistence._finalize_recovered_rebase(
        context,
        local_head=landed,
        method="crash_recovery_pushed",
        notice=outcomes._pushed_recovery_notice(context, landed),
    )


def _unfinished_recovery_push(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
    published,
    licensed: bool,
) -> bool | None:
    """What a reissued push that did not finish owes, or None where it did.

    Four answers before the finalize, and each is a different tick. A permit
    the gate refused publishes nothing and parks here, since measuring is the
    one thing this road may not fall back on. A hold is a tick the gate
    finished for itself -- parked, or handed to the adjudication -- and only
    the flags it left in memory are owed a write. A push that went out and
    failed is the caller's own park. And a push that landed without the
    verdict moving with it is a permit that stopped holding inside the gate.
    """
    if published.refused:
        return outcomes._park_refused_permit_recovery(
            context, recovery_snapshot,
        )
    if published.held:
        # The gate took the candidate this recovery was finishing, so the
        # finalize behind this -- the notice, the event, the `validating`
        # route -- is not this tick's. The park it left is written here, since
        # nothing else would.
        context.gh.write_pinned_state(context.issue, context.state)
        return True
    if not published.landed:
        return outcomes._park_failed_recovery_push(context, recovery_snapshot)
    landed = recovery_snapshot.head
    if licensed and not transfers._rotated_onto(context.state, landed):
        return outcomes._park_unfinished_recovery(
            context, recovery_snapshot, _UNROTATED.format(published=landed),
        )
    return None


def _finish_published_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
    carried: transfers._Handoff = transfers._Handoff.NOTHING,
) -> bool:
    """Finish the route behind a rewrite the pull request already carries.

    The ordinary answer is a relabel and nothing else: the push landed before
    the crash, the remote has the commit, and what the dead tick still owed
    was the notice, the audit event, and the reviewer's route back. Nothing is
    pushed, nothing is measured, and no agent runs -- which is exactly what an
    already-landed rewrite has to get.

    One handoff owes more, and it is the window between a push that landed and
    the write that receipts it. There the permission a transfer was granted on
    is still OUTSTANDING: the exemption is on the commit a human ruled on, the
    debt still says a push is owed, and nothing on the comment says the pull
    request carries the rewrite. Relabelled and left, that permission is
    re-asked one stage later against a `validating` issue the rewrite was
    never entered from -- the permit refuses on the stage alone, the ordinary
    cumulative gate measures the replay, and an adjudicated change is routed
    back into adjudication.

    So the settlement is taken HERE, on the tick and the stage the transfer
    was granted under, and it is taken through the same gated publication
    every other push in this domain goes through. Standing on the commit
    already, that publication is the leased no-op the push tail makes anyway:
    nothing is sent, the lease is the rewritten commit itself, and what it
    buys is the receipt, the paid debt, and the rotation riding one durable
    write -- proved at the remote rather than read off a local note.

    Both roads are asked one thing first: whether the commit the pull request
    and the checkout agree on is the replay this attempt recorded making. They
    agreeing proves only that they agree -- somebody who moved the branch and
    the remote together leaves exactly this shape -- and finishing there drops
    the anchor that is the only thing bringing this recovery back.

    Every other handoff is then asked whether the pinned comment ACCOUNTS for
    the rewrite the pull request carries, and only an accounted one is
    finished.
    An issue carrying no verdict always is, which is the ordinary interrupted
    rebase and has nothing a missing record could strand. One whose transfer
    settled, or whose replay the ordinary cumulative gate published, is
    accounted for by the receipt that write left. Anything else -- a record
    this build cannot read, a receipt nobody wrote, a debt nothing paid --
    parks with the anchor still pinned, because finishing there would drop the
    one thing that brings this recovery back and leave the next tick to
    measure an adjudicated change as a fresh candidate.
    """
    landed = recovery_snapshot.head
    if not context.pending_rewrite.names(landed):
        return outcomes._park_unfinished_recovery(
            context, recovery_snapshot,
            _UNPROVEN_LANDING.format(published=landed or "an unreadable head"),
        )
    loose = _loose_tree_holds(context, recovery_snapshot, carried)
    if loose is not None:
        return loose
    unaccounted = transfers._unaccounted_publication(
        context.state, landed, context.pending_pre_rebase_sha, carried,
    )
    if unaccounted and carried != transfers._Handoff.OUTSTANDING:
        return outcomes._park_unfinished_recovery(
            context, recovery_snapshot, unaccounted,
        )
    return _finishes_the_landed_route(context, recovery_snapshot, carried)


def _finishes_the_landed_route(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
    carried: transfers._Handoff,
) -> bool:
    """Take the last step a landed rewrite is still owed.

    The record a settled transfer never got to report is made first, on
    whichever of them follows: the settlement's own write goes down before the
    record does, so a tick lost in between is one of the states this road
    comes back to, and the proof it kept is the one thing no later reading
    could re-derive. What it stages rides the write every road below makes.

    Then three answers, and the record says which. A finish that ANNOUNCED
    owes only the relabel it may not have made and the write that clears the
    attempt -- saying any of the rest again would put a second `base_rebased`
    on the stream and a second notice on the pull request for one publication
    that happened once. A permission still OUTSTANDING owes the receipt, taken
    through the leased no-op that proves the pull request really carries the
    commit. Everything else owes the ordinary route: the notice, the audit
    event, and the reviewer sent back to the rewritten head.
    """
    transfers._reports_a_lost_settlement(context)
    if transfers._already_announced(context.state, recovery_snapshot.head):
        return persistence._write_the_finished_route(context)
    if carried == transfers._Handoff.OUTSTANDING:
        return _settle_published_recovery(context, recovery_snapshot)
    return outcomes._finalize_already_published_recovery(
        context, recovery_snapshot,
    )


def _loose_tree_holds(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
    carried: transfers._Handoff,
) -> bool | None:
    """The park a checkout carrying loose work owes, or None where it is clean.

    Asked of every handoff carrying a verdict rather than only of the one that
    still owes a receipt, because what finishing does is hand the issue to the
    reviewer -- and a reviewer sent to a checkout with uncommitted files reads
    work the pull request does not have as though it were under review. A
    settled transfer is no different from an outstanding one there: the remote
    is right either way, and the tree is what the next reader works from.

    Nothing is reset for it. The pull request carries the rewrite and so does
    the branch, so there is nothing here to put back -- what a loose tree
    costs is the finish, and the anchor stays pinned so the tick that comes
    back once a human has cleaned up can make it.

    Silent for an issue carrying no verdict at all, which is the ordinary
    interrupted rebase: nothing is being moved onto the replay there, and the
    dirty tree is the stage handler's own to answer for.
    """
    if carried == transfers._Handoff.NOTHING:
        return None
    dirty_files = verification_probes._worktree_dirty_files(context.worktree)
    if not dirty_files:
        return None
    return outcomes._park_unfinished_recovery(
        context, recovery_snapshot,
        _LOOSE_TREE.format(count=len(dirty_files)),
    )


def _settle_published_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Receipt a landed rewrite through the leased no-op that proves it.

    Entered on the pre-rebase anchor and named against the rewritten commit,
    exactly as the interrupted tick entered it: the anchor is the head the
    permit was granted against, and the pull request standing on the rewrite
    instead is a head that permit accounts for while its permission is
    outstanding -- this issue's own push having landed, with only the receipt
    behind it lost.

    Asked of the PERMIT before the gate, and refused rather than measured.
    The gate's own fallback for a permit that declines is the ordinary
    cumulative reading -- which is the right answer for a rebase deciding
    whether to publish, and the wrong one here twice over. A count under the
    ceiling would report this call as a landed publication and let the route
    finish with the permission still outstanding and the verdict still on the
    commit a human ruled on; a count over it would route an adjudicated change
    into a second adjudication with the pull request already carrying the
    work. There is nothing to measure on this road at all: the remote has the
    commit, and the only question is whether the permission may be spent.

    Asked again on the far side, because a permit that granted before the
    gate is not proof the settlement happened: the terms are re-read inside,
    and anything that moved in between leaves the push landed and the verdict
    where it was. The rotation itself is the answer, so it is read off the
    record rather than assumed.

    A refused push is the one thing that can still go wrong, and it is a
    remote that moved between this tick's fetch and the request. Nothing is
    reset for it: the checkout is standing on the commit the pull request was
    carrying a moment ago, and putting it back on the anchor would take the
    branch off work the remote has. The anchor stays pinned, the issue parks,
    and the next recovery classifies the remote afresh.
    """
    landed = recovery_snapshot.head
    if not transfers._permits_the_publication(context, landed):
        return outcomes._park_unfinished_recovery(
            context, recovery_snapshot, _REFUSED_PERMIT,
        )
    records = publication._gate_records()
    published = publication._gated_publication()._publishes(
        records._gate(
            context.gh, context.spec, context.issue, context.state,
            context.worktree,
        ),
        recovery_snapshot.branch,
        records._Entered(
            head=context.pending_pre_rebase_sha or "", reconciling=True,
            candidate=landed,
            permit_only=True,
        ),
    )
    unsettled = _unsettled_no_op(context, recovery_snapshot, published)
    if unsettled is not None:
        return unsettled
    return outcomes._finalize_already_published_recovery(
        context, recovery_snapshot,
    )


def _unsettled_no_op(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
    published,
) -> bool | None:
    """What a leased no-op that settled nothing owes, or None where it did.

    Three ways the receipting push does not finish, and each is its own tick.
    A permit the gate refused publishes nothing and parks here rather than
    falling through to a reading nothing on this road needs. A hold is a tick
    the gate finished for itself, and only the flags it left are owed a write.
    A refused push is a remote that moved between this tick's fetch and the
    request. And past all three, a push that landed with the verdict still
    where it was is a permit that stopped holding inside the gate.
    """
    if published.refused:
        return outcomes._park_unfinished_recovery(
            context, recovery_snapshot, _REFUSED_PERMIT,
        )
    if published.held:
        context.gh.write_pinned_state(context.issue, context.state)
        return True
    if not published.landed:
        return outcomes._park_unfinished_recovery(
            context, recovery_snapshot, _REFUSED_NO_OP,
        )
    landed = recovery_snapshot.head
    if transfers._rotated_onto(context.state, landed):
        return None
    return outcomes._park_unfinished_recovery(
        context, recovery_snapshot, _UNROTATED.format(published=landed),
    )


def _recover_pending_auto_base_rebase_context(
    context: _AutoRebaseRecoveryContext,
) -> bool:
    """Route an interrupted auto-rebase from verified local/remote state."""
    if context.label not in _PR_REFRESH_DETOUR_LABELS:
        return _answers_an_ineligible_label(context)

    recovery_snapshot = snapshot._fetch_recovery_snapshot(context)
    if recovery_snapshot is None:
        return True
    if (
        recovery_snapshot.local_head
        and recovery_snapshot.local_head == context.pending_pre_rebase_sha
    ):
        return _finish_an_unmoved_head(context, recovery_snapshot)

    return _route_recovery_snapshot(context, recovery_snapshot)


def _answers_an_ineligible_label(
    context: _AutoRebaseRecoveryContext,
) -> bool:
    """Answer an anchor found under a label the base refresh does not drive.

    Nothing is fetched and nothing is compared, because nothing under this
    label is coming back to do either. So the road is a clear or a park, and
    what decides between them is whether the attempt left anything a clear
    would strand.

    An anchor over a checkout still standing ON it strands nothing. Git never
    moved the branch, no replay exists, and no permission was granted, so the
    anchor is a promise to come back that nobody is coming back for: dropping
    it costs the issue nothing, and leaving it pinned would strand a flag no
    later tick under this label ever reads.

    Everything else parks with every record intact. A rebase the tick
    RECORDED, or a permission granted for a push that never landed, is state
    the clear cannot honour: it would drop the one field naming what the
    branch would go back to while leaving the verdict, the debt, and the
    replay standing without it. A checkout that has MOVED off the anchor
    under the terms alone is the same refusal one reading over: that is the
    window between `git rebase` returning and the write that names what it
    produced, and the terms on their own cannot tell it from an attempt that
    never started. Cleared there, the replay stays on the branch with nothing
    on the comment naming it, and the issue this route hands on is one no
    reader can tell from an issue with nothing in flight -- so another handler
    or a decomposition tick is free to start over on a change a human already
    ruled on.

    The head is read locally, which costs no fetch and no request. A reading
    that could not be taken at all is no evidence the branch is where the
    attempt left it, so it parks with everything else this route cannot
    prove.
    """
    if context.pending_rewrite.left_a_replay:
        return outcomes._park_stranded_recovery(context)
    if transfers._left_mid_transfer(context.state):
        return outcomes._park_stranded_recovery(context)
    if verification_probes._head_sha(
        context.worktree,
    ) != context.pending_pre_rebase_sha:
        return outcomes._park_stranded_recovery(context)
    return snapshot._clear_ineligible_recovery(context)


def _finish_an_unmoved_head(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Answer a checkout standing exactly where the attempt anchored it.

    Two states look identical from HEAD alone, and only one of them is the
    shortcut. An attempt that pinned its anchor and got no further left
    nothing else behind: no record of a replay, no permission it never spent,
    and a tree git has not touched. Dropping the anchor there costs nothing --
    the normal rebase flow picks the branch up on this same tick and does the
    work again.

    The other is an attempt that got a long way and was UNDONE. A reset that
    landed and whose park write did not, or somebody's own `git reset`, puts
    the branch back on the anchor with the record of the replay, the
    permission granted for it, and the debt beside it all still standing --
    and the tree may still carry whatever the reset was taken over. Dropping
    the anchor there throws away the only thing that brings a recovery back,
    leaves the transfer state for the next grant to trip over, and hands the
    branch straight to a fresh rebase.

    So the shortcut is for the unstarted attempt only, and everything else is
    finished as the rollback it is: the reset is re-run onto the head the
    branch is already on, which is what lets the abandoned debt and the
    permission the replay will never spend go with it, and the issue parks for
    a human to say what undid it.
    """
    if _unstarted_attempt(context, recovery_snapshot):
        return snapshot._clear_unchanged_recovery(context)
    return outcomes._park_undone_recovery(context, recovery_snapshot)


def _unstarted_attempt(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Whether this attempt left nothing behind but the anchor it pinned.

    Three things say it did leave something. A record of the replay it
    produced, whether whole or in pieces, is an attempt that reached the write
    after `git rebase` -- and a reset that put the branch back before its own
    park write leaves exactly that. A mark saying a finish had already
    announced a head says the same from the far end of the route, since no
    finish ever announces the anchor. A permission this build reads as
    outstanding, or one it cannot vouch for at all, is a grant that was never
    spent on a commit the branch no longer has.

    A SETTLED permission is not one of them: a transfer that finished is never
    cleared, so every issue that ever earned one would fail this test for the
    rest of its life.

    The TREE is nobody's question here, and deliberately. A checkout carrying
    uncommitted work is one the clean-tree gate ahead of every rebase already
    refuses, so a shortcut taken over one hands the branch to a flow that
    stands down on the same tick -- and where the dirt came from a reset this
    attempt took, the record it left is the first test above.

    Costs no git and no request, which is what lets the ordinary unstarted
    attempt -- the whole reason this shortcut exists -- pay nothing for it.
    """
    if context.pending_rewrite.left_a_replay:
        return False
    if transfers._foreign_mark(context.state, recovery_snapshot.local_head):
        return False
    carried = transfers._carried_by(context, recovery_snapshot.local_head)
    return carried not in _UNSPENT_TRANSFERS


def _route_recovery_snapshot(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Route a changed-head recovery from its completed local/remote compare.

    Two classifications rather than one, taken together because the answer is
    the pair. Where the REMOTE stands says which effect the dead tick got as
    far as -- still on the anchor and the push never went out, on the rewrite
    and it did, anywhere else and somebody moved the branch out of band. What
    the pinned comment CARRIES says which of the transfer's own writes it got
    as far as, and that is what the two roads with something left to publish
    are handed: the evidence a permit is decided on, and whether the receipt
    behind a landed push is still owed.

    The transfer is read once, before either road, because it costs nothing
    and because reading it twice would let the two roads disagree about the
    same comment.

    Both are answered by exact SHAs rather than by the ahead/behind counts,
    and for the interrupted rebase that is the whole difference between
    finishing and parking. A rebase REPLAYS the branch: the commit the pull
    request still carries is on no local history afterwards, so git counts the
    branch as behind its own publication -- ahead by the replay and the base
    it moved onto, behind by the object it replaced. Read off those counts,
    the canonical pre-push recovery is indistinguishable from a remote
    somebody else pushed to, and the tick that only ever needed to reissue its
    push parks instead. What tells them apart is the pair of heads the attempt
    itself recorded: the anchor the remote must still be standing on, and the
    replay the checkout must still be.

    A remote the RECORD says has already carried this replay is refused
    before anything is pushed at it. The receipt behind a landed push, and the
    settled transfer that rides the same write, are both claims that the pull
    request had this commit -- so a pull request that no longer does was rolled
    back by somebody, and the anchor the retry would lease against is exactly
    the head they rolled it back to. The lease would be satisfied and the
    rollback overwritten, which is the one thing a lease exists to stop.

    A record nobody can vouch for is refused before either road that would
    publish anything. Left to the ordinary gate, a damaged transfer group over
    an adjudicated commit is measured afresh, sent past the same ceiling, and
    routed into a second adjudication with a pull request already open over
    the work -- so the branch goes back onto the anchor and a human is asked.

    The mark a finish leaves is asked here for the same reason and one step
    earlier than the road that reads it: a mark naming any head but the one in
    hand is a checkpoint something took apart, and every road below would read
    it as no announcement at all -- putting a second notice on the pull
    request and a second `base_rebased` on the stream for one publication that
    happened once.

    The counts still answer for every remote neither SHA accounts for, which
    is the one question they can settle: a publication that moved out of band
    is behind as well as ahead, and a pair of zeros over two heads that
    disagree is a reading that did not happen.
    """
    completed = snapshot._complete_recovery_snapshot(
        context, recovery_snapshot,
    )
    if completed is None:
        return True
    carried = transfers._carried_by(context, completed.local_head)
    if _made_for_another_publication(context, completed):
        return outcomes._park_foreign_publication_recovery(context, completed)
    if transfers._foreign_mark(context.state, completed.local_head):
        return outcomes._park_unfinished_recovery(
            context, completed, _FOREIGN_MARK,
        )
    if completed.local_head and completed.local_head == completed.remote_head:
        return _finish_published_recovery(context, completed, carried)
    return _route_an_unpublished_head(context, completed, carried)


def _made_for_another_publication(
    context: _AutoRebaseRecoveryContext,
    completed: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Whether the attempt was made for a publication this tick is not on.

    Asked ahead of every road, because what a recovery does at the end of one
    is never silent: the notice goes to the pull request this tick holds, the
    audit event is filed under the stage this tick reads, and the anchor that
    is the only thing bringing the tick back is dropped. A pull request
    repointed or an issue relabelled while the process was down would have all
    three attributed to a publication the interrupted attempt was never made
    for -- and would attribute them without anything having noticed.

    Asked of the RECORD rather than of the permit, because the permit is not
    on every road: a settled transfer has no permit left to re-ask, an issue
    carrying no verdict never had one, and both still reach the finalize that
    clears the anchor and announces itself.

    Silent where the attempt recorded nothing, which is the window between git
    returning and the write that records it. There is no claim to disagree
    with there, and the roads behind this one already refuse to publish
    anything they cannot show the terms of.

    One stage disagreement is this route's OWN and is forgiven: a finish
    relabels the issue to `validating` right after it records that it has
    announced itself, so a tick that finds that mark beside a record made from
    another stage is looking at its own last step. Only `validating` is
    forgiven, and only there, because that is the one label this route ever
    writes: an issue somebody moved to `fixing` or `documenting` while the
    process was down is a publication this attempt was not made for, whatever
    mark stands beside it. The pull request is never forgiven either -- no
    step of this route repoints one.
    """
    recorded = context.pending_rewrite
    if not recorded.is_declared:
        return False
    if recorded.pr_number != context.pr_number:
        return True
    if recorded.stage == context.label:
        return False
    if context.label != WorkflowLabel.VALIDATING:
        return True
    return not transfers._already_announced(
        context.state, completed.local_head,
    )


def _route_an_unpublished_head(
    context: _AutoRebaseRecoveryContext,
    completed: _AutoRebaseRecoverySnapshot,
    carried: transfers._Handoff,
) -> bool:
    """Route a checkout the pull request is not standing on.

    Four refusals before the one road that pushes, in the order the evidence
    for them costs nothing to read. A remote the record says already carried
    this replay has been rolled back by somebody, and the anchor a retry would
    lease against is the head they rolled it back to. A transfer record nobody
    can vouch for would reach the ordinary cumulative gate and send an
    adjudicated change into a second adjudication. An attempt record that does
    not vouch for the checkout -- damaged, or whole and naming some other
    commit -- is the same refusal one field over: read as the window it
    resembles, it would fall through to the counts, and a strictly-ahead
    checkout would be measured and force-pushed on the strength of a claim
    nothing could check. And a remote standing anywhere but the anchor is not
    one this attempt's own push was ever leased against.

    What is left is the retry the anchor exists for, and -- for a remote
    neither pinned head accounts for -- the counts, over an attempt that
    recorded nothing at all.
    """
    if transfers._rolled_back_publication(context, completed, carried):
        return outcomes._park_rolled_back_recovery(context, completed)
    if carried == transfers._Handoff.UNVOUCHED:
        return outcomes._park_unvouched_recovery(context, completed)
    if _unclaimed_checkout(context, completed):
        return outcomes._park_unrecorded_recovery(context, completed)
    in_flight = _is_an_attempt_in_flight(context, completed, carried)
    if in_flight or _is_this_attempts_rewrite(context, completed):
        return _retry_recovery_push(
            context, completed, carried, permit_alone=in_flight,
        )
    return _route_a_moved_remote(context, completed, carried)


def _is_an_attempt_in_flight(
    context: _AutoRebaseRecoveryContext,
    completed: _AutoRebaseRecoverySnapshot,
    carried: transfers._Handoff,
) -> bool:
    """Whether this is the window between `git rebase` and its own record.

    The narrowest window the attempt has and the only one no id can close: the
    rebase produced a commit, the write naming it never happened, and what the
    comment still carries is the terms the attempt was entered under and the
    anchor the remote is standing on. Every id-based road refuses this
    checkout, rightly -- nothing wrote the head down, so nothing can say it is
    this attempt's work.

    Something else can. An issue whose exemption names the commit the pull
    request carries has a pair a human ruled on recorded on it, and the permit
    re-fingerprints the checkout's contribution against that pair before it
    licenses anything: a replay of the accepted change proves out, and a
    commit somebody else left does not. So the road is opened only where there
    IS such a verdict to prove against, and the push behind it is permitted or
    it does not happen.

    Both other halves are still required, and for the reasons they always
    were. The remote has to be standing exactly on the anchor, which is what
    says no push of this attempt's ever landed and what the force-with-lease
    is pinned to. And the terms have to read back whole, since the permit's
    publication checks are asked against them.
    """
    if carried != transfers._Handoff.UNRECORDED:
        return False
    if completed.remote_head != context.pending_pre_rebase_sha:
        return False
    recorded = context.pending_rewrite
    return recorded.is_declared and not recorded.sha


def _unclaimed_checkout(
    context: _AutoRebaseRecoveryContext,
    completed: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Whether a record was written and does not vouch for this checkout.

    The counts behind this refusal are a fallback for one state and one only:
    an attempt that reached no record of a REPLAY, which is either a comment
    from before this record existed or the window between git returning and
    the write that names what it produced. There they are all a recovery has
    on its own, and a strictly-ahead branch is a fast-forward the anchor lease
    loses nothing to.

    Every other absence is a claim. A group something took a member out of,
    and a whole group naming some OTHER commit, both leave a checkout the
    attempt does not vouch for -- and read as the window they resemble, the
    counts would measure it and force-push it under a lease a rebuilt
    worktree, an operator's reset, and a branch pointed at other work all
    satisfy. So a record of the replay having been written at all is what
    decides which road is available, and a comment carrying none is the only
    one that reaches the counts.

    The TERMS on their own are not that claim. They go down with the anchor,
    before git can move the branch, so a comment carrying them and no head
    says an attempt was in flight rather than anything about the checkout --
    and the road that answers for that window vouches for the head by what it
    contributes instead.
    """
    recorded = context.pending_rewrite
    return recorded.left_a_replay and not recorded.names(
        completed.local_head,
    )


def _is_this_attempts_rewrite(
    context: _AutoRebaseRecoveryContext,
    completed: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Whether the checkout is the replay this attempt made, over its anchor.

    Both halves, and neither is enough alone. The REMOTE has to be standing
    exactly on the anchor the rebase pinned before git ran, which is what says
    no push of this attempt's landed and what the force-with-lease behind the
    retry is pinned to. And the CHECKOUT has to be the head that attempt
    recorded as its own replay, which is the only thing that says the
    divergence in front of this tick is the rebase's work rather than a
    worktree somebody rebuilt, an operator's reset, or a branch pointed
    somewhere else -- every one of which satisfies the same lease and would
    take the candidate off the pull request.

    Empty provenance answers no, and what happens then depends on which
    emptiness it is. A comment carrying the attempt's terms and no head is the
    window between git returning and the write that records the replay, and
    the road beside this one answers for it on the evidence rather than on an
    id. A comment carrying nothing at all is an attempt from before this
    record existed, and there the recovery falls back to the counts it always
    used: a strictly-ahead branch is a fast-forward the anchor lease loses
    nothing to, and a divergent one parks.
    """
    if completed.remote_head != context.pending_pre_rebase_sha:
        return False
    return context.pending_rewrite.names(completed.local_head)


def _route_a_moved_remote(
    context: _AutoRebaseRecoveryContext,
    completed: _AutoRebaseRecoverySnapshot,
    carried: transfers._Handoff,
) -> bool:
    """Route a remote neither SHA this recovery holds accounts for.

    Reached once the pull request is proved to be standing on neither the
    rewrite this branch carries nor the anchor the rebase pinned before git
    ran, so whatever is on it arrived from somewhere else. The counts are what
    is left to tell those apart, and they answer the question they were always
    about: a pair of zeros over two heads that disagree is a reading that did
    not happen, a remote with commits of its own is one a force-push would
    drop, and a strictly-ahead branch is a lease this recovery may still try
    -- the push is pinned to the anchor, so a remote that is not on it refuses
    the request rather than being overwritten.
    """
    if completed.ahead == 0 and completed.behind == 0:
        return outcomes._reject_unknown_recovery_comparison(context, completed)
    if completed.behind > 0:
        return outcomes._park_diverged_recovery(context, completed)
    return _retry_recovery_push(context, completed, carried)


def _recover_pending_auto_base_rebase(
    *args: Any,
    **kwargs: Any,
) -> bool:
    """Finalize a clean auto-base-rebase interrupted by a prior crash.

    The pinned pre-rebase SHA distinguishes an unchanged worktree, an
    already-published rewrite, an ahead-only rewrite that still needs a
    push, and a branch that diverged through an out-of-band update. Returns
    False only when HEAD still equals the anchor and the normal rebase flow
    should continue on the same tick.
    """
    bound_fields = _RECOVERY_SIGNATURE.bind(*args, **kwargs)
    bound_fields.apply_defaults()
    context = _AutoRebaseRecoveryContext(**bound_fields.arguments)
    return _recover_pending_auto_base_rebase_context(context)


_recover_pending_auto_base_rebase.__signature__ = _RECOVERY_SIGNATURE
