# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The terminal answers a verified crash-recovery comparison can produce.

One interrupted auto-rebase resolves into exactly one of these: the rewrite
was already published, the comparison is unclassifiable, the remote moved
out of band, the worktree is dirty, the reissued push failed, the pinned
comment claims an exemption or a transfer nobody can read whole, the attempt's
own record is in pieces, the attempt was made for a publication this issue no
longer records, no permit licenses the replay to publish at all, the branch was
put back on the anchor with the attempt's own records still standing, the
remote was rolled back off a replay the record says it carried, a rewrite
the pull request already carries is one this tick cannot finish the route
behind, the issue was relabelled off the refresh-driven set with the
attempt's own records still standing, or an attempt still in flight left a
replay no record names and no verdict can prove.
Each one either finalizes through ``persistence`` or parks, so keeping them
in one owner is what makes the set enumerable -- an outcome that neither
routed nor parked would leave the issue holding an anchor no later tick can
act on.

Most parks reset HEAD onto the pre-rebase anchor first, because that anchor
is the head the remote PR still carries and the reviewer is still voting on.
Three must not. The unfinished-route park sits over a remote standing on the
REWRITE, so putting the branch back on the anchor would take the checkout off
work the pull request has. The foreign-publication park cannot say which
pull request the branch belongs to at all, which is a question about the
issue's record rather than about the commit -- throwing the replay away would
answer neither. And the stranded park is taken under a label nothing here
classifies, so it cannot say whether the hand that moved the issue moved the
checkout too. All three park with the anchor left pinned instead, and the
next tick classifies afresh.

The stranded one is also the only park here that has to recognize its own
work. It is reached from the label check ahead of every gate, so every poll
under the wrong label comes back to a comment nothing has changed -- and
saying it again would repeat one sentence on the thread and ratchet the
watermark past the operator's own reply, which is the thing that would
release the attempt.
"""
from __future__ import annotations

from orchestrator import config
from orchestrator.git.base_sync import persistence, snapshot
from orchestrator.git.base_sync.models import (
    _AutoRebaseRecoveryContext,
    _AutoRebaseRecoverySnapshot,
)
from orchestrator.git.base_sync.state import (
    _AWAITING_HUMAN,
    _PARK_REASON,
    _REASON_AUTO_BASE_REBASE_FAILED,
    _REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    log,
)
from orchestrator.workflow.state import WorkflowLabel


def _already_published_recovery_notice(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
) -> str:
    """Format the notice for a recovery push that landed before restart."""
    short_head = local_head[:8]
    notice = (
        f":mag: Recovered an interrupted auto-rebase for PR "
        f"#{context.pr_number}; the new head `{short_head}` was "
        "already published before the orchestrator restart."
    )
    if context.behind == 0:
        return (
            notice
            + f" Routing `{context.label}` -> `{WorkflowLabel.VALIDATING}`"
            " so the reviewer re-runs against the rewritten branch."
        )
    return (
        notice
        + f" Base advanced again by {context.behind} commit(s)"
        " since the interrupted rebase; rebasing once more before "
        f"routing to `{WorkflowLabel.VALIDATING}`."
    )


def _pushed_recovery_notice(
    context: _AutoRebaseRecoveryContext,
    local_head: str,
) -> str:
    """Format the notice for a recovery push reissued this tick."""
    short_head = local_head[:8]
    notice = (
        f":mag: Recovered an interrupted auto-rebase for PR "
        f"#{context.pr_number}; pushed the recovered head "
        f"`{short_head}`."
    )
    if context.behind == 0:
        return (
            f"{notice} Routing `{context.label}` -> "
            f"`{WorkflowLabel.VALIDATING}`."
        )
    return (
        notice
        + f" Base advanced again by {context.behind} commit(s) "
        "since the interrupted rebase; rebasing once more before "
        f"routing to `{WorkflowLabel.VALIDATING}`."
    )


def _finalize_already_published_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Finalize state after confirming that the interrupted push landed."""
    return persistence._finalize_recovered_rebase(
        context,
        local_head=recovery_snapshot.local_head,
        method="crash_recovery_relabel_only",
        notice=_already_published_recovery_notice(
            context, recovery_snapshot.local_head,
        ),
    )


def _reject_unknown_recovery_comparison(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Park when unequal heads cannot be classified as ahead or behind."""
    log.warning(
        "issue=#%d auto-rebase recovery: local HEAD (`%s`) differs "
        "from remote PR head (`%s`) but the divergence probe "
        "returned `(0, 0)`; aborting recovery and parking awaiting "
        "human",
        context.issue.number,
        recovery_snapshot.local_head[:8],
        recovery_snapshot.remote_head[:8],
    )
    local_short = recovery_snapshot.local_head[:8]
    remote_short = recovery_snapshot.remote_head[:8]
    return snapshot._abort_recovery_unverified(
        context,
        f"local HEAD `{local_short}` differs from remote "
        f"PR head `{remote_short}` but "
        "the divergence probe returned `(0, 0)`, which means the "
        "remote-tracking ref we just fetched could not be read or "
        "compared against -- the path the recovery would take next "
        "cannot be determined safely.",
    )


def _park_diverged_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor instead of overwriting an out-of-band PR update."""
    spec = context.spec
    local_short = recovery_snapshot.local_head[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    persistence._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: local worktree "
            f"(`{local_short}`) is {recovery_snapshot.ahead} ahead "
            f"and {recovery_snapshot.behind} behind remote "
            f"`{spec.remote_name}/{recovery_snapshot.branch}` -- the "
            "remote PR branch was updated out-of-band during the "
            "interrupted auto rebase. HEAD has been reset to the pre-"
            f"rebase SHA `{pre_rebase_short}`. "
            "Investigate the remote PR head and reply on this issue "
            "with anything once the divergence is reconciled."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    return True


def _park_dirty_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
    dirty_files: list[str],
) -> bool:
    """Reset and clean a recovered rebase that carries worktree changes."""
    local_short = recovery_snapshot.local_head[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    persistence._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: the rebased worktree (recovered "
            f"from a prior tick, HEAD `{local_short}`) "
            f"carries {len(dirty_files)} uncommitted change(s). HEAD "
            "has been reset to the pre-rebase SHA "
            f"`{pre_rebase_short}` and untracked "
            "files cleaned (use `git reflog` if you need the "
            "discarded edits). Investigate, then reply on this issue "
            "with anything to retry."
        ),
        reason="auto_base_rebase_dirty",
        clean=True,
    )
    return True


def _park_failed_recovery_push(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor after a recovered force-push fails."""
    local_short = recovery_snapshot.local_head[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    persistence._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: `--force-with-lease` push of the "
            f"recovered rebase (`{local_short}`, lease "
            f"against `{pre_rebase_short}`) failed. "
            "HEAD has been reset to the pre-rebase SHA. Most likely "
            "the remote PR branch was updated out-of-band; investigate "
            "and reply on this issue with anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    return True


def _park_unfinished_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
    detail: str,
) -> bool:
    """Park, without a reset, a landed rewrite this tick may not finish.

    The pull request is standing on the rewritten commit and so is the
    checkout, so there was never anything to send here: what the tick owed was
    a receipt, a settlement, or nothing at all -- and it could not establish
    which. `detail` is the reason it could not, and it is the operator's whole
    starting point.

    HEAD is deliberately left alone. Every other park here resets onto the
    pre-rebase anchor because that is the head the remote still carries; here
    the remote carries the rewrite instead, so a reset would take the checkout
    off work the pull request has and hand the next reader a branch behind its
    own publication.

    The anchor stays pinned with it, and that is what makes the park
    recoverable rather than terminal. It is also the whole reason this park
    exists rather than the ordinary relabel: the anchor is the only thing that
    brings this recovery back, so clearing it over a verdict that may not have
    moved leaves the next tick to measure a rewrite a human already ruled on
    and route it into adjudication a second time. A human's reply re-enters
    this route, the remote is read again, and whatever it turns out to be is
    classified from scratch.
    """
    local_short = (recovery_snapshot.local_head or "")[:8]
    log.warning(
        "issue=#%d auto-rebase recovery: PR #%d already carries %s and this "
        "tick cannot finish the route behind it (%s); leaving HEAD and the "
        "recovery anchor exactly as they are and parking awaiting human",
        context.issue.number, context.pr_number, local_short, detail,
    )
    persistence._park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: the branch was rebased and pushed before "
            f"an earlier tick died, so PR #{context.pr_number} already "
            f"carries `{local_short}` -- but the route behind that push "
            f"cannot be finished safely because {detail}. HEAD has NOT been "
            "reset: the worktree is standing on the commit the PR carries. "
            "Investigate the pinned comment and the remote branch, then reply "
            "on this issue with anything once they are reconciled."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    return True


def _already_stranded(state) -> bool:
    """Whether this route's own stranded park is already standing.

    Two fields, and the anchor beside them is what makes the pair this park's
    own rather than any other auto-rebase failure's: every other road that
    ends on this reason resets the branch and clears the attempt first, so a
    comment still carrying one is one only this park could have left. The
    caller is on that road by definition, since the record is what brought it
    here.
    """
    if not state.get(_AWAITING_HUMAN):
        return False
    return state.get(_PARK_REASON) == _REASON_AUTO_BASE_REBASE_FAILED


def _park_unvouched_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor rather than measure a record nobody can read.

    The comment claims something about the commit this issue exempts -- a
    transfer group short of a member, an exemption it cannot show whole, an
    identity taken under a scheme this build does not compute -- and the
    branch is standing on a replay of that commit with nothing on the remote
    yet.

    Every other road from here ends in the ordinary cumulative gate, and for
    an adjudicated change that is the wrong answer twice over: the replay is
    measured past the same ceiling and routed into a second adjudication, with
    a pull request already open over the work, on the strength of a record
    nothing checked. The permit refuses the same claim for the same reason, so
    there is nothing this tick could do with it but ask.

    So the branch goes back onto the anchor -- the head the remote still
    carries, so nothing is lost that the reflog does not have -- and the issue
    parks. The record itself is left exactly as it stands: a group this reader
    cannot vouch for is the only account there is of how the exemption came to
    name what it names, and the rollback drops only what it can read whole.
    """
    local_short = (recovery_snapshot.local_head or "")[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    log.warning(
        "issue=#%d auto-rebase recovery: the pinned comment claims a transfer "
        "for the commit this issue exempts and this build cannot read it back "
        "whole; resetting %s onto the anchor and parking rather than measuring "
        "an adjudicated change again",
        context.issue.number, local_short,
    )
    persistence._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: this issue's pinned comment claims an "
            "adjudication exemption -- or a transfer of one -- that the "
            "orchestrator cannot read back whole, and the interrupted rebase "
            f"left `{local_short}` on the branch. Publishing it would send a "
            "change a human already ruled on back into adjudication on the "
            "strength of a record nothing could check, so HEAD has been reset "
            f"to the pre-rebase SHA `{pre_rebase_short}` and nothing was "
            "pushed. Repair the `late_exempt_*` / `late_rewrite_*` fields on "
            "the pinned comment, then reply on this issue with anything to "
            "retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )
    return True


def _park_rolled_back_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor when the remote was rolled back off this replay.

    The record says the pull request carried the commit on this checkout --
    a receipt naming it, or a transfer that settled on the write behind one --
    and the remote is not standing on it now. Somebody moved the branch back,
    and where they moved it to is very often the pre-rebase anchor itself,
    which is the head a reissued force-push would be leased against. That
    lease would be satisfied, the push would land, and the rollback would be
    gone -- the one outcome a lease exists to prevent, reached by a recovery
    mistaking somebody's undo for its own unfinished work.

    So it is the externally moved remote it is: HEAD goes back onto the anchor
    so the checkout matches what the pull request has, the anchor is dropped
    with it, and the issue parks for a human to say which of the two heads the
    branch is supposed to be on.
    """
    local_short = (recovery_snapshot.local_head or "")[:8]
    remote_short = (recovery_snapshot.remote_head or "")[:8]
    log.warning(
        "issue=#%d auto-rebase recovery: the pinned comment records %s as "
        "published and PR #%d stands on %s; treating the branch as rolled "
        "back out of band rather than force-pushing over it",
        context.issue.number, local_short, context.pr_number, remote_short,
    )
    persistence._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: this issue's pinned comment records "
            f"`{local_short}` as already pushed, and the pull request is "
            f"standing on `{remote_short}` instead -- the branch was rolled "
            "back or moved out of band while the orchestrator was down. "
            "Reissuing the interrupted push would be leased against the very "
            "head it was rolled back to and would overwrite it, so nothing "
            "was pushed and HEAD has been reset to the pre-rebase SHA. "
            "Investigate the remote branch and reply on this issue with "
            "anything once it is reconciled."
        ),
        reason=_REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    )
    return True


def _park_foreign_publication_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Park, without a reset, an attempt made for another publication.

    The interrupted tick recorded which pull request it rebased for and which
    stage it was entered from, and the issue no longer says either. Every road
    out of a recovery ends in the same tail -- a notice to the pull request
    this tick holds, an audit event filed under the stage this tick reads, and
    the anchor dropped -- so finishing here would attribute the dead tick's
    work to a publication it was never made for, and drop the one record that
    could ever say otherwise.

    Nothing is reset. Which publication the branch belongs to is exactly what
    this tick cannot say, and putting the checkout back onto the anchor would
    throw the replay away to settle a question about the pull request rather
    than about the commit. The whole record stays pinned with it, so a human
    who repoints the issue back, or clears the record, hands the next tick
    something it can finish.
    """
    recorded = context.pending_rewrite
    log.warning(
        "issue=#%d auto-rebase recovery: the interrupted attempt recorded PR "
        "#%d from %r and this issue now records PR #%d on %r; parking rather "
        "than finishing a route for a publication it was not made for",
        context.issue.number, recorded.pr_number, str(recorded.stage),
        context.pr_number, str(context.label),
    )
    persistence._park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for this issue's auto "
            f"rebase: the interrupted attempt was made against pull request "
            f"#{recorded.pr_number} from `{recorded.stage}`, and this issue "
            f"now records pull request #{context.pr_number} on "
            f"`{context.label}`. Finishing it would post the notice, file the "
            "audit event, and route the reviewer against a publication that "
            "attempt was never made for, so nothing was pushed and HEAD has "
            "not been reset. Put the issue back on the publication the rebase "
            "was made for -- or clear the "
            "`pending_auto_base_rebase_*` fields on the pinned comment -- then "
            "reply on this issue with anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )
    return True


def _park_unrecorded_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor when the attempt's own record is in pieces.

    The pinned comment claims a record of what this attempt produced and
    cannot show it whole -- a member missing, a pull request that is not an
    identity, a stage no publication is entered from. Read as the absence it
    resembles, the recovery would fall through to the ahead/behind counts and
    a strictly-ahead checkout would be measured and force-pushed on the
    strength of a claim nothing could check.

    So the branch goes back onto the anchor, which is the head the remote
    still carries wherever this refusal is reachable, and the issue parks. The
    record itself is left where the reset's own rule leaves every damaged
    group: for a human to repair, not for this tick to guess at.
    """
    local_short = (recovery_snapshot.local_head or "")[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    log.warning(
        "issue=#%d auto-rebase recovery: the record of what this attempt "
        "produced is not one this build can read whole; resetting %s onto the "
        "anchor rather than publishing a checkout nothing vouches for",
        context.issue.number, local_short,
    )
    persistence._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: the pinned comment claims a record of the "
            "rebase an earlier tick was interrupted in the middle of, and the "
            "orchestrator cannot read it back whole -- so it cannot say that "
            f"`{local_short}` on the branch is that attempt's own work. HEAD "
            f"has been reset to the pre-rebase SHA `{pre_rebase_short}` and "
            "nothing was pushed. Repair or clear the "
            "`pending_auto_base_rebase_*` fields on the pinned comment, then "
            "reply on this issue with anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )
    return True


def _park_refused_permit_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor when the permit for an unpushed replay refuses.

    The branch is standing on a rewrite of a commit an adjudication accepted
    and the push it was made for never went out. The permit is the whole of
    what may let that push out here: measured instead, an oversized replay
    goes back into adjudication with a pull request already open over the
    work, and a small one is force-pushed and the route finished with the
    verdict still on the commit a human ruled on.

    So the branch goes back onto the anchor -- the head the remote carries
    wherever a retry was ever possible -- and the issue parks. The permission
    the rollback finds goes with the object it was granted for, on the
    rollback's own terms: the replay is on no branch after the reset, so a
    claim about a push that will never happen is dropped, and the exemption
    stays exactly where the adjudication put it.
    """
    local_short = (recovery_snapshot.local_head or "")[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    log.warning(
        "issue=#%d auto-rebase recovery: no permit licenses %s to publish and "
        "there is nothing else this road may publish it on; resetting onto "
        "the anchor rather than measuring an adjudicated change again",
        context.issue.number, local_short,
    )
    persistence._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: an earlier tick rebased this branch onto "
            "the advanced base and died before pushing, and the permission "
            f"that would let `{local_short}` publish as the change a human "
            "already adjudicated no longer holds -- the pull request, the "
            "stage, the checkout, the leased head, or the two contributions "
            "no longer agree, and the orchestrator log names which. "
            "Publishing it on anything else would send that change back into "
            f"adjudication, so HEAD has been reset to `{pre_rebase_short}` "
            "and nothing was pushed. Reconcile the pinned comment and reply "
            "on this issue with anything to retry."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )
    return True


def _park_unproven_replay_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Restore the anchor where nothing can vouch for a replay in flight.

    The attempt died between `git rebase` and the write that names what it
    produced, so the terms are on the comment, the anchor is on the remote,
    and no id anywhere names the commit the checkout is standing on. The one
    thing that can still say whose work it is, is the verdict this issue
    carries: the permit re-fingerprints the contribution in front of it
    against the pair a human ruled on, and a replay of that change proves out
    where a checkout somebody rebuilt does not.

    Reached where that evidence will not assemble at all -- a semantic record
    this issue never earned or nothing can read, a base the remote would not
    name, an object this host does not hold. There is nothing left to prove
    the head by, and the road behind this one is the ordinary cumulative
    reading, which answers a different question: a count says how big a
    change is, never whose it is. Measured and pushed on that, a worktree
    rebuilt from elsewhere lands on the pull request under a lease the anchor
    satisfies.

    So the branch goes back onto the anchor the remote is still carrying and
    the issue parks. Nothing is lost that the reflog does not have, and the
    rebase this branch is still owed is one the next tick makes for itself
    once a human has said the checkout is where they want it.
    """
    local_short = (recovery_snapshot.local_head or "")[:8]
    pre_rebase_short = context.pending_pre_rebase_sha[:8]
    log.warning(
        "issue=#%d auto-rebase recovery: the attempt died before recording "
        "the replay it made and nothing on this issue can vouch for %s; "
        "resetting onto the anchor rather than publishing a head no record "
        "and no verdict names",
        context.issue.number, local_short,
    )
    persistence._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: an earlier tick rebased this branch and "
            "died before it could record which commit that produced, so "
            f"nothing on this issue vouches for `{local_short}` -- and this "
            "issue carries no adjudication verdict the contribution could be "
            "proved against instead. Publishing it would force-push a head "
            "no record names over the pull request, so HEAD has been reset "
            f"to `{pre_rebase_short}` (the head the pull request carries) "
            "and nothing was pushed; the replay is still in `git reflog`. "
            "Check the worktree and reply on this issue with anything to "
            "have the rebase made again."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )
    return True


def _park_undone_recovery(
    context: _AutoRebaseRecoveryContext,
    recovery_snapshot: _AutoRebaseRecoverySnapshot,
) -> bool:
    """Finish the rollback that put the branch back, and park what undid it.

    HEAD is exactly the head the attempt anchored, and the comment says the
    attempt got further than that: a replay it recorded producing, a
    permission it never spent, or a tree the reset never finished cleaning.
    Something put the branch back -- a reset whose own park write was lost, or
    a hand at the checkout -- and what it left behind is the bookkeeping that
    reset owed.

    So the reset is re-run onto the commit the branch is already on. It moves
    nothing, and that is the point: it is the step the abandoned rollback's
    own bookkeeping rides, so the debt for a commit no branch has and the
    permission that will never be spent on it go with it, on the rollback's
    own terms. The exemption is untouched, since the grant never moved it.

    The park is what the missing half of that rollback owes a human: nothing
    here can say why the branch went back, and guessing would either rebase
    over an operator mid-repair or leave a record nobody reconciles.
    """
    unmoved = (recovery_snapshot.local_head or "")[:8]
    log.warning(
        "issue=#%d auto-rebase recovery: HEAD is back on the anchor %s and "
        "the comment still carries what the attempt did past it; finishing "
        "the rollback's bookkeeping and parking rather than starting over",
        context.issue.number, unmoved,
    )
    persistence._reset_clear_and_park(
        context,
        context.pending_pre_rebase_sha,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number}: the branch is back on the pre-rebase SHA "
            f"`{unmoved}` and this issue still records the rebase an earlier "
            "tick made past it -- the replay it produced, the permission "
            "granted for it, or uncommitted changes a reset never cleaned. "
            "Something undid that rebase without finishing its bookkeeping, "
            "so the records it abandoned have been dropped and nothing was "
            "rebased or pushed. Check the worktree and reply on this issue "
            "with anything once it is where you want it."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )
    return True


def _park_stranded_recovery(context: _AutoRebaseRecoveryContext) -> bool:
    """Hold an attempt whose issue was relabelled out from under it.

    The label is no longer one refresh drives, so this recovery has no road
    left: nothing here fetches, compares, or publishes for a stage the sync
    does not own. What the attempt left decides what that costs. An issue
    whose checkout is still standing on the anchor and holds nothing else
    loses nothing by dropping it -- git never moved the branch, and whichever
    handler owns the new label works from where it is. An issue holding a
    rebase an earlier tick RECORDED, a permission granted for a push nobody
    made, or a branch git has already replayed is a different thing entirely:
    the checkout may be standing on a rewrite the pull request has never seen,
    a human's verdict is licensed onto a commit no push carried, and the
    approval debt beside it says a publication is still owed.

    Dropped there, the three come apart from one another. The anchor is the
    only thing naming what the branch would go back to, so the replay stops
    being attributable to anything; the permission outlives the attempt it was
    granted for and the next grant trips over it; and a decomposition tick
    reading an issue with no attempt in flight is free to put another agent on
    a change a human already ruled on.

    So nothing is reset and nothing is cleared. The reset cannot run here for
    the same reason the classification cannot: this tick does not know whether
    the hand that moved the label also moved the checkout, and a hard reset
    onto the anchor would answer that by discarding it. The issue parks with
    every record standing exactly as the interrupted tick left it, which is
    what lets an operator put the label back and let the ordinary recovery
    finish the attempt on its own terms.

    Which is also why the park has to be taken ONCE. Keeping the record is
    what brings this route back, and this route is reached from the label
    check ahead of every gate -- so every poll under the wrong label arrives
    here again, over a comment nothing has changed. Said again each time, the
    thread fills with one sentence repeated and, worse, each park ratchets
    `last_action_comment_id` past whatever the operator wrote: the reply that
    would release the attempt ends up behind the orchestrator's own newest
    comment and the retry scan never sees it.

    So a park this route already left standing is left alone entirely --
    nothing posted, nothing written, nothing ratcheted -- and the state it
    guarded is exactly what a later tick reads. It is recognized by the pair
    only this park writes with an anchor still pinned: every other road that
    ends on this reason clears the attempt first, so an issue that still holds
    one and is already flagged for a human is one this route stopped before.
    """
    if _already_stranded(context.state):
        log.debug(
            "issue=#%d auto-rebase recovery: the attempt stranded under %r is "
            "already parked for a human; leaving the comment and the record "
            "exactly as the park left them",
            context.issue.number,
            context.label,
        )
        return True
    log.warning(
        "issue=#%d auto-rebase recovery: label %r is no longer in the "
        "refresh-driven set and the comment still carries the attempt an "
        "earlier tick left; parking with every record intact rather than "
        "clearing it",
        context.issue.number,
        context.label,
    )
    persistence._park_auto_rebase_failure(
        context.gh,
        context.issue,
        context.state,
        message=(
            f"{config.HITL_MENTIONS} crash recovery for PR "
            f"#{context.pr_number} cannot run under label "
            f"`{context.label}`, which the base refresh does not drive -- and "
            "this issue still carries an interrupted rebase: the replay an "
            "earlier tick made, a permission granted to carry an adjudication "
            "verdict over a push that never landed, or a checkout git has "
            "already moved off the pre-rebase SHA. Nothing was "
            "rebased, pushed, or reset, and no record was dropped, so the "
            "worktree and the pinned state are exactly as that tick left "
            "them. Move the issue back to the label the rebase was "
            "interrupted under and reply here with anything, and the next "
            "polling tick will finish the recovery on its own terms."
        ),
        reason=_REASON_AUTO_BASE_REBASE_FAILED,
    )
    return True
