# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Whether the size gate keeps a candidate off a pull request that is open.

The answer half of a gated publication, between the entry `late_overflow`
freezes and the push `late_push` makes with it. Everything the switch, the
record, and the measurement decide is asked here in one place, so the seam
that reached the gate makes no difference to what it is told: an install with
`DECOMPOSE=off` never reads a pull request, a record already in the gate goes
through the ordinary questions, and a commit an approval owes a push is one
this gate has already ruled on.

What comes back is never a bare permission. A hold is the whole of what the
tick did -- parked, or handed to the adjudication -- and the caller pushes
nothing and hands the issue on to nothing. Anything else carries the two
commits the push has to be named and pinned by, plus the head the pull request
is standing on now, which is what says whether the push has anything left to
do at all.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace as _replace

from orchestrator import config
from orchestrator.git.measurement import commits as _measurement_commits
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.stages.implementing import (
    late_freeze as _freeze,
    late_gate as _gate,
    late_overflow as _overflow,
    late_parks as _parks,
    late_records as _records,
    late_verdict as _verdict_owner,
)

log = logging.getLogger("orchestrator.workflow")


# The revision a checkout's own head is named by.
_HEAD = "HEAD"


# What the unpinnable-approval refusal is logged and reported as.
_UNPINNABLE_APPROVAL = "the approval records no head to pin its push against"


_UNPINNABLE_APPROVAL_PARK = (
    "{mentions} this issue owes a push for `{candidate}` -- a commit already "
    "adjudicated, so nothing measures it again -- and the pinned comment "
    "records no head to pin that push against. The lease is the whole of what "
    "stops it landing on a pull request somebody moved while the commit was "
    "waiting, so nothing was pushed. Repair the pinned comment, or commit "
    "again so the candidate is measured afresh."
)


@dataclass(frozen=True)
class _PublishedCandidate:
    """What the gate let through, and the two commits the push is named by.

    Neither SHA is decoration. `revision` is the commit that was proved and
    measured, and it is what the push publishes: between the reading and the
    push the checkout is writable -- another tick, an operator, a descendant a
    timeout cleanup raced -- and a push that named nothing would send whatever
    the branch had become while the record said the measured commit went out.
    `lease` is the head the pull request was standing on when the entry was
    frozen, and it is what the push is pinned against: a push that let git
    take its own reading of the remote would adopt whoever landed in between
    as the lease and overwrite them.

    Neither is dropped where the switch kept the candidate out of the gate.
    Nothing was measured there and no entry was frozen, so this owner read no
    pull request and has no head of its own -- but the CHECKOUT still names
    the commit, and the lease is the CALLER's wherever it established one: the
    conflict and base-sync publications each read the remote for themselves,
    and dropping what they read would make `DECOMPOSE=off` the setting that
    turns a lease into a blind force-push. `lease` is empty only for a caller
    that established none, and the push takes its own reading of the remote
    there. `revision` is empty only where the checkout could not name its own
    head at all.

    `standing` is the third: the head the pull request is on as this tick
    froze it, which is what says whether the push has anything left to do. It
    is the same value as `lease` wherever this tick took the reading, a
    different one for an approved commit -- whose lease is the head the
    reading was taken over rather than the head the remote is on now -- and
    empty where the switch kept the candidate out, since nothing read that
    pull request to say where it stands.

    `permitted_sha` is the fourth and is about the write PAST the push rather
    than the push itself: it is the commit a rewrite permit proved out for on
    this tick, and it is what licenses the receipt to carry a human's verdict
    over. Empty on every other road, the ordinary measurement's included --
    a permit that refused leaves the candidate to the cumulative gate, and a
    count the ceiling lets through publishes on that count and earns no
    exemption. Carried here rather than re-read from the comment, because a
    permission standing there says a permit was once granted and not that it
    still holds.

    `refused` says a `permit_only` caller's permit declined and nothing was
    measured in its place. It is a hold like any other here -- nothing is
    published -- and what makes it its own answer is that the gate took no
    park and made no route, so the caller is the one that has to fail closed.
    """

    held: bool
    revision: str = ""
    lease: str = ""
    standing: str = ""
    permitted_sha: str = ""
    refused: bool = False


# What every held answer is, since a hold publishes nothing and so names
# neither commit.
_HELD = _PublishedCandidate(held=True)

# What a permit-only caller's refusal is: the same silence, handed back as the
# caller's own to answer for rather than as a tick the gate finished.
_REFUSED = _PublishedCandidate(held=True, refused=True)


def _holds_published_work(
    plain: _records._Gate, entered: _records._Entered,
) -> _PublishedCandidate:
    """Whether the size gate keeps this candidate off an open pull request.

    `held` is the whole of what this tick did with it: the issue is parked on
    a reading nobody could take, or handed to the late coordinator under
    `workflow:decomposing`, and on both the caller pushes nothing and hands
    the issue on to nothing. Not held is the ordinary answer: the push this
    caller was about to make is the push it may still make, with the two
    commits beside it that push has to be named and pinned by.

    The switch is asked first and on its own terms, because everything below
    it costs a request or a park: an install with `DECOMPOSE=off` does not
    read a pull request, does not freeze an entry, and does not park over
    either, while a candidate already in the gate or a commit an approval owes
    a push is work the switch has nothing left to say about and still goes
    through the ordinary questions.

    What the caller established is applied to the subject BEFORE that
    question, because `answering` is one of the three states the switch has
    nothing left to say about. A call answering a reading this gate itself
    recorded is named and leased whatever the switch says -- and asked over a
    bare subject it would read as new work and hand back a push with no commit
    to name and no head to pin. That is the shape a retry lands in: an entry
    that refused persists no generation, so a tick taken after the switch was
    turned off has nothing on the record to tell it apart.

    `entered.reconciling` is the wider fact beside it -- that no developer ran
    on this tick -- which is what tells a checkout that MOVED from a resumed
    developer's fresh commit. There is no run here for a moved head to be the
    output of, so it is refused rather than measured. It answers the switch
    for none of the seams that set it without the narrower claim: a rebase, a
    resolution, and a recovery push are new work with no agent behind them.
    """
    recorded = _late_state.read_late_generation(plain.state)
    gate = _replace(
        plain,
        reconciling=entered.reconciling,
        answering=entered.answering,
        candidate=entered.candidate,
        spends=entered.spends,
        rewrite=entered.rewrite,
        permit_only=entered.permit_only,
    )
    if _freeze._outside_the_gate(gate, recorded):
        return _unentered(gate, _gate._holds_candidate(gate), entered)
    entry = _overflow._frozen_entry(gate, entered)
    if not entry.is_frozen:
        _overflow._refused_entry(gate, recorded, entry)
        return _HELD
    gate = _replace(gate, entry=entry)
    if _overflow._moved_publication(gate, recorded, entry):
        return _HELD
    return _measured(gate, _gate._holds_candidate(gate), entry)


def _unentered(
    gate: _records._Gate,
    verdict: _records._GateVerdict,
    entered: _records._Entered,
) -> _PublishedCandidate:
    """The answer for a candidate the switch kept out of the gate.

    No entry was frozen, so this owner read no pull request and has no head of
    its own to pin the push against. What the CALLER established is another
    matter and is carried through: the conflict and base-sync publications
    each read the remote for themselves and force-push under that lease, and
    it is the whole of what stops one of them overwriting a pull request
    somebody moved. Dropping it because the switch is off would make
    `DECOMPOSE=off` the setting that turns a lease into a blind force-push --
    a measurement the switch decides, and a safety claim it does not.

    The COMMIT is named all the same, and where the gate proved none the
    checkout names it: the switch keeps candidates out of the measurement, not
    out of a push that knows which commit it is publishing -- the same answer
    the initial publication gives an install running that way.

    Naming it is not a nicety here. Everything past the push is a claim about
    one object id: the receipt that records what reached the remote, and the
    proof that the checkout is still standing on it. Handed an empty name,
    both read a checkout that never moved as one that did -- so a push that
    landed would record an empty receipt and then park the issue for a head
    that is exactly where it was left.

    A checkout that cannot name its own head leaves the answer empty, and the
    two steps behind it skip rather than compare against nothing: an unnamed
    push publishes whatever the branch has become, so there is no commit for
    either to be about.

    The debt goes down here for the same reason it goes down beside a frozen
    entry: this push MOVES a publication -- the caller read the head it is
    replacing -- and past this call the only account of the work is the commit
    on the branch. Recorded against the caller's own lease, a tick that dies
    between the push and the receipt comes back to an issue that says which
    commit is owed a publication and what it is pinned to, rather than to one
    that looks as though it has published nothing.
    """
    revision = verdict.candidate_sha or _checkout_head(gate)
    if not verdict.held:
        _verdict_owner._owed_by_an_unmeasured_push(
            gate, revision, entered.head,
        )
    return _PublishedCandidate(
        held=verdict.held,
        revision=revision,
        lease=entered.head,
        permitted_sha=verdict.permitted_sha,
        refused=verdict.refused,
    )


def _checkout_head(gate: _records._Gate) -> str:
    """The commit this checkout is standing on, or "" if it cannot say.

    Proved rather than read, for the reason every other commit in this domain
    is: a revision this host cannot peel is not an id anything downstream may
    be held to.
    """
    proved = _measurement_commits._prove_candidate_commit(gate.worktree, _HEAD)
    if proved.is_frozen:
        return proved.sha
    log.warning(
        "issue=#%d could not name the commit its checkout stands on; the "
        "push it is about goes out unnamed and unverified behind",
        gate.issue.number,
    )
    return ""


def _measured(
    gate: _records._Gate,
    verdict: _records._GateVerdict,
    entry: _records._PublicationEntry,
) -> _PublishedCandidate:
    """The answer for a candidate this call proved, measured, and let through.

    Which head the push is pinned to turns on whether this tick did the
    reading. Where it did, the entry it froze IS the reading, and it is what
    the push is pinned to.

    Where it did not -- an APPROVED commit, which bypasses the measurement
    because a reading already settled it -- the entry is the wrong answer and
    the recorded lease is the only right one. Nothing measured this candidate
    against the head read now, so pinning to it would adopt a head somebody
    moved in between and force-overwrite them with work measured against the
    head it used to be on. That is why the fallback is a REFUSAL rather than
    the entry: an approval whose lease is gone or unreadable is one nothing
    can pin, and a publication nothing can pin is one this gate may not make.

    A pull request already standing ON the candidate is asked ahead of both,
    because there is no push left to name or pin: the remote carries this
    exact commit, so whatever the record still says is owed was paid by a
    push that landed. Asked here rather than at the push, so an approval
    whose lease died with the write that should have spent it is reconciled
    instead of parking for a lease no push needs.
    """
    if verdict.held:
        return _REFUSED if verdict.refused else _HELD
    standing = entry.published_sha
    if _parks._approved_commit(gate.state) != verdict.candidate_sha:
        return _PublishedCandidate(
            held=False,
            revision=verdict.candidate_sha,
            lease=standing,
            standing=standing,
            permitted_sha=verdict.permitted_sha,
        )
    lease = _parks._approved_lease(gate.state)
    if not lease and standing != verdict.candidate_sha:
        return _unpinnable(gate, verdict.candidate_sha)
    return _PublishedCandidate(
        held=False,
        revision=verdict.candidate_sha,
        lease=lease or standing,
        standing=standing,
        permitted_sha=verdict.permitted_sha,
    )


def _unpinnable(
    gate: _records._Gate, candidate_sha: str,
) -> _PublishedCandidate:
    """Refuse an approved commit whose lease the record cannot show.

    The approval says one commit is owed a push and no measurement stands
    between it and the remote, so the lease is the whole of what stops that
    push landing on a pull request somebody moved. Read fail-closed like every
    other late commit field, an absent or hand-edited one leaves nothing to
    pin against -- and falling back to the head read NOW would pin to exactly
    the move the lease exists to catch.
    """
    log.error(
        "issue=#%d owes a push for approved commit %s and records no head to "
        "pin it against; refusing to publish onto a pull request nothing "
        "could be leased to",
        gate.issue.number, candidate_sha,
    )
    recorded = _late_state.read_late_generation(gate.state)
    _parks._parked(
        gate, _records._reportable(gate, recorded), _UNPINNABLE_APPROVAL,
        _UNPINNABLE_APPROVAL_PARK.format(
            mentions=config.HITL_MENTIONS, candidate=candidate_sha,
        ),
    )
    return _HELD
