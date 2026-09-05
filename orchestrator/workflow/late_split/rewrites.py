# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What authorized an exemption to move from one commit to the one that replaced it.

An exemption names the exact commit a human adjudicated, and nothing else is
exempt. That is the whole of its safety, and it is also what a workflow
REWRITE breaks: a squash on approval collapses the accepted commit into a new
object, and a clean base rebase -- the refresh's own, or the one the conflict
stage runs -- replays it onto a base that moved, so either way the identical
contribution comes back on a commit nothing exempts. The gate, which
recognizes a decided candidate by one commit and only it, would measure that
object past the same ceiling and adjudicate the same change a second time.

This record is what says the move is earned. It is written before anything
reaches the remote and it moves NOTHING -- the exemption stays on the commit a
human ruled on until `record_rewrite_publication` carries it over on the write
that receipts a landed push, since a verdict rotated onto an object no remote
has is one a failed push strands. What it carries is the whole of the evidence
the move was granted on:
the
commit and base the contribution came FROM, the commit and base it went TO,
which rewrite produced it, the digest both pairs fingerprinted to and the
scheme that digest was taken under, and the publication it was made against --
the pull request, the stage the rewrite was entered from, and the head that
pull request was standing on when the lease was taken.

Every one of those is evidence rather than decoration. The two pairs are what
a reader re-derives the equality from rather than taking this record's word
for it. The kind is bounded because what one member licenses is a rewrite this
workflow makes on purpose; a spelling this build does not know is a record
from somewhere else, and acting on it would carry a human's verdict onto an
object nothing here produced. And the publication group is what scopes the
authorization to one push: an authorization that could not name the pull
request it was granted against would go on answering for the next one.

The PHASE is what says whether the move has happened, and it is the one
field every other reading turns on. The record goes down before the push and
says `authorized`, and it moves NOTHING: the exemption is still the commit a
human ruled on, because the object the rewrite produced is on no remote yet
and a verdict rotated onto it there would be stranded by a push that failed or
a process that died. What spends the permission is the write that receipts a
landed push: `record_rewrite_publication` carries the exemption and its
identity over and moves the phase to `published`, and the caller stages it
alongside the account of what the remote now holds, so the move and the
receipt land together or not at all.

Both writes are here rather than at the seam that decides, because the move
and the evidence for it are one record: the exemption, the identity beside it,
and the phase have to agree at every moment a reader could read them, and an
owner that set the phase for itself would be free to announce a transfer whose
exemption it never moved. So the reader is the writer's own gate -- a
publication is recorded only over a permission this build can read back whole
and still finds outstanding -- and `published` is a phase this owner writes
exactly once per grant and reads fail-closed everywhere else: a group
announcing itself finished over fields nothing else here understands came from
a hand edit or another build, and it is the one a reader may not act on
unchecked.

That is also what binds this record to the exemption, and which end binds
follows from the phase: the accepted end while it stands at `authorized`, the
rewritten one once it is `published`. And it is what a rollback reads. A
force-push refused between the two puts the branch back onto the head the
rewrite found it on -- the accepted end for a squash, which collapsed that
commit, and the lease for a rebase, which read the anchor for itself -- so what
the reset owes is dropping the permission it will never spend. An `authorized`
record is therefore droppable and a `published` one is not: the first is a
transfer whose effect never left this host, the second one the pull request
already carries.

Read fail-closed, whole or not at all, like every other late record. A field
that is missing, one that is not the shape it claims, a kind or a phase this
build cannot account for, a digest taken under a scheme it does not compute, a
stage that does not make the kind recorded beside it, and a record whose new
commit is not the one the exemption currently names each read back as no
authorization -- which costs a rollback the reversal and never lets one happen
on evidence nobody can check. The kind and the stage are held TOGETHER rather
than one at a time, because each is a value this build knows and only the pair
says whether the rewrite is one anything here produced.

A WRITER asks something else, and it is the question the fail-closed reader
cannot answer for it: whether a group standing here CLAIMS the commit this
issue exempts. A grant replaces the whole group rather than adding beside one,
so an unreadable claim about the exempt commit is evidence a transfer may not
overwrite in the act of deciding it is entitled to. The one group that is not
such a claim is one naming another commit, which a later exemption moved past
-- read as a claim it would refuse every transfer the issue could ever earn
again.

The keys live outside `LATE_STATE_KEYS` for the reason the exemption's do:
they describe the commit on the exemption field and share its whole lifetime,
so the write that clears a generation may take none of them.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from orchestrator.git.measurement.models import FINGERPRINT_FORMAT
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import (
    exemption as _exemption,
    formats as _formats,
    payloads as _payloads,
)
from orchestrator.workflow.state import (
    WorkflowLabel,
    rebased_by_the_base_refresh,
)


class LateRewriteKind(StrEnum):
    """Which rewrite of its own the workflow may carry an exemption over.

    Bounded, and small on purpose: a member is a rewrite this workflow makes
    itself, over a checkout it is holding still, out of commits it can name
    both ends of. Three are, and two of them are rebases told apart by which
    owner runs one. The SQUASH a reviewer's approval earns collapses the
    accepted commit into an object of the orchestrator's own making. The
    AUTO_CLEAN_REBASE the per-tick refresh publishes replays it onto a base
    that moved -- the refresh holds a branch standing on an exempt commit out
    of the rebase only while the stage that has to act on that commit still
    has the issue, and past that handoff keeping the pushed head in step with
    base is the PR-aware sync's own job. The CONFLICT_REBASE is the replay
    `workflow:resolving_conflict` runs when a branch has stopped merging
    cleanly, which is the one rebase that refresh never drives: it does not
    own that label.

    No member is a claim that the contribution survived. A rebase that
    resolved content conflicts and a squash of work nobody adjudicated are
    both a kind this build authorizes carrying evidence that fingerprints to
    some other change, and the permit refuses them on the fingerprints rather
    than on the kind.

    A wire string this build does not know is not a kind to widen the
    vocabulary for at read time. It is a record from another build or a hand
    edit, and what it would license -- a human's verdict carried onto an
    object nothing here produced -- is the one thing this domain may never
    grant on evidence it cannot account for.
    """

    SQUASH = "squash"
    AUTO_CLEAN_REBASE = "auto_clean_rebase"
    CONFLICT_REBASE = "conflict_rebase"


# Which stages each kind is entered from, because the two fields are one claim
# rather than two. A rewrite record says which rewrite this workflow made and
# where it was made, and each half types perfectly on its own -- so a
# `conflict_rebase` recorded against `validating`, or a `squash` against
# `resolving_conflict`, passes every check asked a field at a time while
# describing a rewrite that stage does not make. Believed, it carries a
# human's verdict onto an object under a provenance nothing here can account
# for, which is the one thing this domain may never do.
#
# Each set is what its producer really names. The SQUASH is the push a
# reviewer's approval earns, made from `validating` and made there before the
# approval handoff relabels. The CONFLICT_REBASE is the replay
# `workflow:resolving_conflict` runs, and that owner spells its own label. The
# AUTO_CLEAN_REBASE names whichever stage the refresh found the issue on, so
# its set is the four that refresh drives -- and telling those from
# `resolving_conflict` is exactly what keeps the two rebase kinds apart.
#
# Every member also publishes onto a pull request the remote already carries,
# which is the predicate the entry a rewrite is made under was frozen against
# -- so this is the narrower of the two questions and never admits a record
# that one would refuse.
_ENTERED_FROM: Mapping[LateRewriteKind, frozenset] = MappingProxyType({
    LateRewriteKind.SQUASH: frozenset((WorkflowLabel.VALIDATING,)),
    LateRewriteKind.CONFLICT_REBASE: frozenset(
        (WorkflowLabel.RESOLVING_CONFLICT,),
    ),
})


def entered_from(
    kind: LateRewriteKind | None, stage: WorkflowLabel | None,
) -> bool:
    """Whether this stage is one that makes this kind of rewrite.

    The cross-field question, asked in one place so the reader and the writer
    cannot answer it differently -- and asked of the PAIR, since each field
    alone is a value this build knows and only the two together say whether
    the record describes a rewrite anything here produced.

    The refresh's own rebase is asked of the stages that refresh DRIVES rather
    than of a set spelled here, since what its evidence names is whichever of
    them the issue was on when the base moved.

    False for a kind this build does not authorize, which is the same answer
    the kind's own check gives: there is no stage that makes a rewrite nothing
    here knows how to make.
    """
    if kind == LateRewriteKind.AUTO_CLEAN_REBASE:
        return rebased_by_the_base_refresh(stage)
    return stage in _ENTERED_FROM.get(kind, frozenset())


class LateRewriteProof(StrEnum):
    """Which reading proved the push a transfer was settled on had landed.

    Beside the phase rather than with the record that reports it, because it
    is the other half of what `PUBLISHED` means: an exemption moves only onto
    a commit the remote holds, so a transfer that reached that phase reached
    it by one of exactly two proofs. `PUSHED` is the ordinary one -- a leased
    force-push moved the pull request off the head the permit was granted
    against. `ALREADY_PUBLISHED` is the recovery -- a tick that pushed and
    died before its receipt came back to a pull request standing there
    already, and the leased no-op that found it so is what proved it this
    time.

    Closed at two, because a remote standing anywhere else is not a third
    outcome: it is a permit that was refused, and a refused permit spends no
    permission and reports nothing.

    Recorded on the comment under `late_rewrite_proof` and dropped by the
    write that follows the record it feeds, so a process lost between the
    settlement and the report leaves the next reader something to report
    from. It sits OUTSIDE the authorization's own keys all the same: those
    are the evidence the permission was granted on, which a reader holds
    whole, and this is a note about a record that may already be out.
    """

    PUSHED = "pushed"
    ALREADY_PUBLISHED = "already_published"


class LateRewritePhase(StrEnum):
    """How far the transfer this record authorizes got.

    `AUTHORIZED` is written before the push and moves NOTHING: the exemption
    is still the commit a human ruled on, and what the record says is that a
    push for the rewritten one is outstanding. `PUBLISHED` is what the write
    that receipts that landed push moves it to, and that write is where the
    exemption is carried over -- `record_rewrite_publication` makes both moves
    at once, so no reader ever sees one without the other.

    So an outstanding permission says three things at once, and every reader
    here turns on one of them. A push is OWED for the commit it names, which
    is why the approval standing beside it may not be spent on the object id
    alone. The exemption has NOT moved, which is why the accepted end binds
    the record while it stands here and the rewritten one binds it after. And
    the move is still undoable, which is why a rollback may drop an
    `AUTHORIZED` permission -- the reset puts the branch back onto the commit
    the exemption never left -- while a `PUBLISHED` one describes a transfer
    the pull request already carries and an exemption already moved.
    """

    AUTHORIZED = "authorized"
    PUBLISHED = "published"


# Which rewrite the transfer was granted for, and how far it got. Spelled here
# because this is the record's owner, and deliberately outside the keys
# `clear_late_generation` drops.
LATE_REWRITE_KIND = "late_rewrite_kind"

LATE_REWRITE_PHASE = "late_rewrite_phase"

# The pair the contribution came FROM -- the exempt commit a human ruled on,
# and the base it is read over -- and the pair it went TO. Both are recorded
# because the equality between them is re-derived rather than believed: a
# reader with the four ends can fingerprint each pair again and compare, and
# one holding a digest alone could only take this record's word for it.
LATE_REWRITE_FROM_SHA = "late_rewrite_from_sha"

LATE_REWRITE_FROM_BASE_SHA = "late_rewrite_from_base_sha"

LATE_REWRITE_TO_SHA = "late_rewrite_to_sha"

LATE_REWRITE_TO_BASE_SHA = "late_rewrite_to_base_sha"

# The digest both pairs fingerprinted to, and the scheme it was taken under.
# The version travels with it for the reason it travels beside the exemption's
# own: two digests taken under different rules are not comparable, and nothing
# about the ids themselves would say so.
LATE_REWRITE_FINGERPRINT = "late_rewrite_fingerprint"

LATE_REWRITE_FINGERPRINT_FORMAT = "late_rewrite_fingerprint_format"

# The publication the rewrite was made against, which is what scopes this
# authorization to one push: the pull request the work is on, the stage the
# rewrite was entered from, and the head that pull request was standing on
# when the force-push behind it was leased.
LATE_REWRITE_PR_NUMBER = "late_rewrite_pr_number"

LATE_REWRITE_SOURCE_STAGE = "late_rewrite_source_stage"

LATE_REWRITE_LEASE = "late_rewrite_lease"

# Which reading proved the push a settled transfer was spent on, kept beside
# the record until the sinks have been told. The proof is a fact about the
# remote at the moment of the push and nothing later can re-derive it, so a
# process lost between the write that settles the transfer and the record it
# owes would lose it for good. Written with the settlement and dropped by the
# report, so a comment still carrying one is a report somebody still owes.
#
# Deliberately outside the group a reader is held to whole: the transfer is
# settled whether or not it has been reported, and a record short of this
# member is not one to refuse. Being PRESENT is another matter -- the key
# stands only between a settlement and the record it owes -- so one standing
# over a phase or a reading nothing here can account for is damage.
LATE_REWRITE_PROOF = "late_rewrite_proof"

# What each recorded hex field has to be, at its exact length: every end of
# either contribution is a whole git object id, the lease is the whole head a
# push was pinned against, and the fingerprint is a whole digest. An
# abbreviation is not a commit this domain froze, and a truncated digest is
# not a hash of anything.
_HEX_SHAPES = MappingProxyType({
    LATE_REWRITE_FROM_SHA: _formats.COMMIT_LENGTHS,
    LATE_REWRITE_FROM_BASE_SHA: _formats.COMMIT_LENGTHS,
    LATE_REWRITE_TO_SHA: _formats.COMMIT_LENGTHS,
    LATE_REWRITE_TO_BASE_SHA: _formats.COMMIT_LENGTHS,
    LATE_REWRITE_LEASE: _formats.COMMIT_LENGTHS,
    LATE_REWRITE_FINGERPRINT: _formats.DIGEST_LENGTHS,
})

# Everything one authorized transfer leaves on the pinned comment, taken as
# one group: it describes the commit the exemption names, so a record short of
# any member describes a transfer this issue cannot show the evidence for.
_AUTHORIZATION_KEYS = (
    *_HEX_SHAPES,
    LATE_REWRITE_KIND,
    LATE_REWRITE_PHASE,
    LATE_REWRITE_FINGERPRINT_FORMAT,
    LATE_REWRITE_PR_NUMBER,
    LATE_REWRITE_SOURCE_STAGE,
)


@dataclass(frozen=True)
class LateRewrite:
    """One rewrite a caller made of work a pull request already carries.

    The evidence a transfer is granted on, handed in by the owner that made
    the rewrite because every field is something no reading taken afterwards
    could recover: the commit and base the contribution came FROM are off the
    branch the moment it is rewound, and the head the pull request was
    standing on before the force-push is one the push itself moves.

    All eight travel because a transfer is granted on the whole of them and on
    nothing else. The two pairs are what the contribution is fingerprinted
    between, at both ends, so the equality is re-derived rather than asserted.
    The `kind` says which rewrite this workflow made, and it is bounded because
    what a member licenses is a commit the orchestrator produced itself. And
    the publication group -- the pull request, the stage it was entered from,
    and the pre-rewrite head the force-push is leased against -- is what scopes
    the whole claim to one push onto one pull request.

    The same record is what goes down on the pinned comment once a permit is
    granted, so what a later reader is held to is exactly what the grant was
    taken over rather than a second spelling of it.
    """

    kind: LateRewriteKind | None = None
    from_sha: str = ""
    from_base_sha: str = ""
    to_sha: str = ""
    to_base_sha: str = ""
    pr_number: int = 0
    source_stage: WorkflowLabel | None = None
    lease: str = ""


@dataclass(frozen=True)
class LateRewriteAuthorization:
    """One granted transfer, once every field of it proved out.

    Handed out whole or not at all, so nothing downstream has to decide what
    half of one means. Which end the exemption names when a reader holds this
    follows from the `phase` -- the accepted one at `authorized`, the
    rewritten one at `published` -- and the reader PROVES that rather than
    assuming it: the end and the exemption are separate pinned fields, and a
    comment where the bound one disagrees describes a transfer some later
    write moved the exemption off.

    The `fingerprint` is what the permission was granted OVER, and it is
    handed out to be compared: a caller re-deriving the contribution holds
    this digest to its own reading rather than carrying it forward, since a
    digest nobody checks is one a grant would quietly rewrite. And
    `fingerprint_format` travels with it because two digests taken under
    different rules are not comparable.
    """

    rewrite: LateRewrite
    phase: LateRewritePhase
    fingerprint: str
    fingerprint_format: int


def carries_rewrite_authorization(state: PinnedState) -> bool:
    """Whether this comment carries any part of an authorization at all.

    Presence rather than truth, and presence of ANY member rather than of all
    of them, because what this answers is whether the comment is CLAIMING a
    transfer -- which a record a crash left half written, or one a hand edit
    damaged, claims just as loudly as a whole one. A reader that asked the
    fail-closed reader instead would read a damaged claim as no claim, which
    is the one reading that lets a repair overwrite evidence nobody checked.

    The key being THERE is the whole test, rather than the value under it
    being something. A pinned comment is JSON, so a field can be present and
    `null` -- a hand edit, or an older binary writing a value this one reads
    as nothing -- and a group carrying one member spelled that way is exactly
    the minimal damaged claim this exists to catch. Asked for a value instead,
    it would answer "no group at all" and let the write past.
    """
    return any(key in state.data for key in _AUTHORIZATION_KEYS)


def claims_the_exemption(state: PinnedState) -> bool:
    """Whether a group standing here claims the commit currently exempt.

    What a WRITER has to ask before it replaces one. An authorization is
    evidence for the exemption beside it, so overwriting a group this build
    could not read would throw away the only account of how the exemption came
    to name what it names -- and a transfer that did so would be granting
    itself the right to repair a record nobody checked.

    A group whose new commit is readable and is NOT the exempt one is the
    exception, and it is not a claim about anything a transfer is doing: the
    exemption has moved on since, which drops the identity and leaves this
    group describing a commit nothing exempts. Left as a claim it would refuse
    every transfer this issue could ever earn again.

    Anything else counts, the missing and the damaged `to_sha` included. A
    record that cannot name its own new commit has not been shown to be about
    some other one, and "not shown to be stale" is the only reading a writer
    may act on here.
    """
    if not carries_rewrite_authorization(state):
        return False
    bound = _bound_end(state)
    if bound is None:
        return True
    return bound == _exemption.read_exemption(state)


def rewritten_commit(state: PinnedState) -> str | None:
    """The commit a group standing here says a rewrite produced, or None.

    The raw end rather than the bound one, because what asks is a caller
    deciding whether a group is ABOUT a commit at all -- before any question
    of whether the move has happened. Read fail-closed like every other
    recorded id, so a field that is missing or is not a whole object id
    answers None, which a caller has to read as "cannot say" rather than as
    "some other commit".
    """
    return _payloads.as_hex(
        state.get(LATE_REWRITE_TO_SHA), _formats.COMMIT_LENGTHS,
    )


def outstanding_permission(state: PinnedState) -> bool:
    """Whether a group standing here says a push is still owed.

    The one question a caller may ask of a record it is not going to act on,
    and the answer is deliberately asymmetric. A permission is recognized as
    SPENT only from a record this build can vouch for entirely -- every field
    at its shape, the phase-bound end the commit the exemption names, and that
    phase `published` -- which is exactly what the write that spends one
    leaves, since it moves the exemption and the phase together. Read off the
    raw phase instead, a group carrying
    nothing else this build understands would announce itself as finished, and
    the approval standing beside it would be spent on an object id with
    neither the permit nor a reading behind it.

    Everything else that carries a group is a claim: a record missing a
    member, one bound to a commit this issue does not exempt, and one still at
    `authorized`. None of those has been shown to be over, and "not shown to
    be over" is the only reading a caller deciding whether to skip a
    measurement may act on.
    """
    if not carries_rewrite_authorization(state):
        return False
    authorization = read_rewrite_authorization(state)
    if authorization is None:
        return True
    return authorization.phase == LateRewritePhase.AUTHORIZED


def _bound_end(state: PinnedState) -> str | None:
    """Which end of the rewrite this record says the exemption is on, or None.

    The one thing the phase decides for a reader, and the reason it is on the
    record at all. A transfer is granted BEFORE the push and rotates nothing:
    while it stands at `authorized` the exemption is still the commit a human
    ruled on, so the accepted end is what binds the record to it. The write
    that receipts the landed push is what moves the exemption onto the object
    the rewrite produced, and past that boundary the rewritten end is what
    binds -- which is why that write moves the two in one statement rather
    than one after the other.

    None where the phase or the end it names is not one this build can read,
    which is not the same claim as "bound to some other commit": a record that
    cannot say which end it is on has not been shown to be about anything
    else, and every caller here treats that as a claim rather than as a gap.
    """
    phase = _payloads.as_member(
        LateRewritePhase, state.get(LATE_REWRITE_PHASE),
    )
    if phase is None:
        return None
    bound = (
        LATE_REWRITE_TO_SHA if phase == LateRewritePhase.PUBLISHED
        else LATE_REWRITE_FROM_SHA
    )
    return _payloads.as_hex(state.get(bound), _formats.COMMIT_LENGTHS)


def read_rewrite_authorization(
    state: PinnedState,
) -> LateRewriteAuthorization | None:
    """Return the transfer this issue's exemption was granted by, or None.

    None wherever the record cannot vouch for itself, which is every way it
    can fail to: a field that is missing, a group where only some of them are
    there, a value that is not the shape its field takes, a kind or a phase
    this build cannot account for, a digest taken under a scheme it does not
    compute, a stage no publication is entered from, and an end the phase
    binds to that is not the commit the exemption currently names -- the
    accepted one while the transfer stands at `authorized`, the rewritten one
    once the receipt has moved it.

    Each of those is a record nothing may act on, and what acting on it would
    do is MOVE a human's verdict: onto the rewritten commit where the push it
    licensed has landed, and back off it where a rollback abandoned one.
    Either way, a record whose ends nobody can name would move that verdict
    onto whatever the damaged field happened to say.
    """
    recorded = {
        key: _payloads.as_hex(state.get(key), lengths)
        for key, lengths in _HEX_SHAPES.items()
    }
    if not all(recorded.values()):
        return None
    if _bound_end(state) != _exemption.read_exemption(state):
        return None
    bounded = _bounded_terms(state)
    if bounded is None:
        return None
    return LateRewriteAuthorization(
        rewrite=LateRewrite(
            kind=bounded["kind"],
            from_sha=recorded[LATE_REWRITE_FROM_SHA],
            from_base_sha=recorded[LATE_REWRITE_FROM_BASE_SHA],
            to_sha=recorded[LATE_REWRITE_TO_SHA],
            to_base_sha=recorded[LATE_REWRITE_TO_BASE_SHA],
            pr_number=bounded["pr_number"],
            source_stage=bounded["source_stage"],
            lease=recorded[LATE_REWRITE_LEASE],
        ),
        phase=bounded["phase"],
        fingerprint=recorded[LATE_REWRITE_FINGERPRINT],
        fingerprint_format=bounded["fingerprint_format"],
    )


def _bounded_terms(state: PinnedState) -> dict | None:
    """The five bounded fields of one record, or None if any is not one.

    Together because they fail together: a kind, a phase, a digest scheme, a
    pull request, and a stage are each a value this build either accounts for
    or does not, and a record short of any of them is not one to act on.

    The stage is asked what it IS rather than merely whether it is a label,
    and asked AGAINST THE KIND beside it: the two are one claim about which
    rewrite this workflow made and where, so a record whose stage does not
    make its kind is one nothing here produced however well each field types
    on its own. That is narrower than the predicate the entry was frozen
    against and admits nothing it would not, so a record this reads whole
    still describes a publication the remote already carries.
    """
    kind = _payloads.as_member(LateRewriteKind, state.get(LATE_REWRITE_KIND))
    phase = _payloads.as_member(
        LateRewritePhase, state.get(LATE_REWRITE_PHASE),
    )
    written = _payloads.as_identity(state.get(LATE_REWRITE_FINGERPRINT_FORMAT))
    pr_number = _payloads.as_identity(state.get(LATE_REWRITE_PR_NUMBER))
    stage = _payloads.as_member(
        WorkflowLabel, state.get(LATE_REWRITE_SOURCE_STAGE),
    )
    if kind is None or phase is None or pr_number is None:
        return None
    if written != FINGERPRINT_FORMAT:
        return None
    if not entered_from(kind, stage):
        return None
    return {
        "kind": kind,
        "phase": phase,
        "fingerprint_format": written,
        "pr_number": pr_number,
        "source_stage": stage,
    }


def record_rewrite_authorization(
    state: PinnedState, rewrite: LateRewrite, fingerprint: str,
) -> None:
    """Record what authorizes this issue's exemption to move, before any push.

    Nothing moves here. The exemption stays exactly on the commit a human
    ruled on, and what goes down beside it is the permission for a later write
    to move it -- so a transfer whose push never lands leaves the verdict
    where the adjudication put it rather than on an object no branch carries.
    The rotation belongs to `record_rewrite_publication`, which the write that
    receipts the push stages it into, so the move lands with that receipt or
    not at all.

    Written beside that exemption and validated against it: the accepted end
    of the rewrite has to BE the commit this issue exempts, because the whole
    of what this record licenses is moving that one verdict, and one naming
    any other commit would describe a move this issue never earned.

    Every field is held to the shape it claims for the reason the exemption
    itself is -- a value that cannot name a commit, a digest, a bounded kind,
    a pull request, or a stage a publication is entered from is not one, and
    writing it would move the failure onto a reader whose only move is to
    reverse a transfer it could not check.

    The phase and the digest scheme are this build's own rather than the
    caller's. The first says the push this authorizes has not been receipted
    yet, which is what tells a reader the exemption has not moved and a
    rollback may still drop the permission; the second says which scheme the
    digest beside it was taken under, and only the owner that takes one can
    answer that.
    """
    refusal = _unusable_terms(rewrite, fingerprint)
    if refusal:
        raise _formats.InvalidLateValue(refusal)
    if _exemption.read_exemption(state) != rewrite.from_sha:
        raise _formats.InvalidLateValue(
            "a rewrite authorization is not the exempt commit's",
        )
    for key, recorded in _written_terms(rewrite, fingerprint).items():
        state.set(key, recorded)
    # The proof belongs to the transfer this grant REPLACES, and the phase
    # going back to `authorized` is what makes it unreadable beside the new
    # one. A report whose own drop-write GitHub refused is the only way one
    # survives this far, and its record has already been made -- so it is
    # dropped with the group it described rather than left to park the issue
    # a settlement later.
    forget_transfer_proof(state)


def _written_terms(rewrite: LateRewrite, fingerprint: str) -> dict:
    """Every key one authorization goes down as, with what it carries.

    Assembled as one mapping rather than written a field at a time, because
    the record is believed as one: a reader holds it whole or not at all, so
    the write that produces it is one statement of what the whole is.
    """
    return {
        LATE_REWRITE_KIND: str(rewrite.kind),
        LATE_REWRITE_PHASE: str(LateRewritePhase.AUTHORIZED),
        LATE_REWRITE_FROM_SHA: rewrite.from_sha,
        LATE_REWRITE_FROM_BASE_SHA: rewrite.from_base_sha,
        LATE_REWRITE_TO_SHA: rewrite.to_sha,
        LATE_REWRITE_TO_BASE_SHA: rewrite.to_base_sha,
        LATE_REWRITE_FINGERPRINT: fingerprint,
        LATE_REWRITE_FINGERPRINT_FORMAT: FINGERPRINT_FORMAT,
        LATE_REWRITE_PR_NUMBER: rewrite.pr_number,
        LATE_REWRITE_SOURCE_STAGE: str(rewrite.source_stage),
        LATE_REWRITE_LEASE: rewrite.lease,
    }


def _unusable_terms(rewrite: LateRewrite, fingerprint: str) -> str:
    """Why this rewrite is not one an authorization may be written for, or "".

    One answer for every term, because a caller that cannot name any of them
    has the same problem: it is asking this domain to record evidence a later
    reader could not check, and the reader's only move is to undo a human's
    verdict on the strength of it.
    """
    if rewrite.kind not in LateRewriteKind:
        return f"a rewrite kind is not one this build authorizes ({rewrite.kind!r})"
    if not _formats.whole_number(rewrite.pr_number) or rewrite.pr_number <= 0:
        return (
            "a rewritten publication is not an identity "
            f"({type(rewrite.pr_number).__name__})"
        )
    if not entered_from(rewrite.kind, rewrite.source_stage):
        return (
            f"a {rewrite.kind} rewrite is not one "
            f"`{rewrite.source_stage}` makes"
        )
    named = (
        (rewrite.from_sha, _formats.COMMIT_LENGTHS),
        (rewrite.from_base_sha, _formats.COMMIT_LENGTHS),
        (rewrite.to_sha, _formats.COMMIT_LENGTHS),
        (rewrite.to_base_sha, _formats.COMMIT_LENGTHS),
        (rewrite.lease, _formats.COMMIT_LENGTHS),
        (fingerprint, _formats.DIGEST_LENGTHS),
    )
    for given, lengths in named:
        if not _formats.is_hex_of(given, lengths):
            return f"a rewrite authorization is not one ({type(given).__name__})"
    return ""


def unreported_transfer(state: PinnedState) -> LateRewriteProof | None:
    """The proof a settled transfer still owes the sinks a record of, or None.

    A transfer is settled by the write that receipts its push, and the record
    of it goes to the sinks behind that write -- so a process lost in between
    leaves a verdict that moved and nothing anywhere saying so. What cannot be
    re-derived later is which reading PROVED the push landed, since the
    receipt looks identical either way, so the proof is kept on the comment
    until the record is out and dropped by the write that follows it.

    None wherever there is nothing owed: a comment carrying no proof, one
    whose transfer this build cannot read back whole, one still outstanding,
    and one whose proof is not a reading this build knows. Each of those is a
    record nothing may be reported from, which is the same answer every other
    reader in this domain gives evidence it cannot check.

    Answering None is not the same as saying the comment is sound, and the
    caller that has to know the difference asks `stranded_transfer_proof`
    beside this.
    """
    proof = _payloads.as_member(LateRewriteProof, state.get(LATE_REWRITE_PROOF))
    if proof is None:
        return None
    authorization = read_rewrite_authorization(state)
    if authorization is None:
        return None
    if authorization.phase != LateRewritePhase.PUBLISHED:
        return None
    return proof


def stranded_transfer_proof(state: PinnedState) -> bool:
    """Whether a proof stands here that nothing can be reported from.

    Presence rather than truth, which is the difference the reader above
    cannot express. A proof is written by the one statement that settles a
    transfer and dropped by the write behind the record it feeds, so the key
    being there at all says a settlement happened and its record may still be
    owed. Where that key is present and no proof comes back, the comment is
    saying two things that cannot both be true: a settled transfer, and a
    record naming a reading, a phase, or an authorization this build cannot
    account for.

    Read as simply "nothing owed", such a comment lets a recovery finish --
    clearing the anchor that is the only thing bringing it back, filing no
    record of the move, and leaving the damaged proof for nobody. So it is
    answered as the damage it is, and every road that asks fails closed on it.

    False for the ordinary comment, which carries no proof at all: the key is
    dropped by the report, by a rollback, and by the grant that replaces the
    transfer it belonged to.
    """
    if LATE_REWRITE_PROOF not in state.data:
        return False
    return unreported_transfer(state) is None


def forget_transfer_proof(state: PinnedState) -> None:
    """Drop the proof a report has just been made from."""
    state.data.pop(LATE_REWRITE_PROOF, None)


def record_rewrite_publication(
    state: PinnedState, proof: LateRewriteProof,
) -> LateRewrite:
    """Spend the permission standing here, carrying the exemption with it.

    The one write in this domain that MOVES a verdict, and the three fields it
    moves go down in one statement because a reader is entitled to find them
    agreeing: the exemption becomes the commit the rewrite produced, the
    identity beside it becomes what that commit contributes over its own base,
    and the phase says the transfer is over. Split across writes, a crash
    between any two leaves a comment whose phase and whose commit disagree --
    which every reader here refuses, and rightly, since it cannot be told from
    a hand edit.

    Held to the RECORD rather than to the caller. The permission is read back
    whole and has to still be outstanding, so what is spent is a transfer this
    build granted and can still account for: a group missing a member, one
    bound to a commit this issue does not exempt, one whose kind or phase came
    from somewhere else, and one already `published` are each a record nothing
    may move a human's verdict on, and each refuses rather than being
    repaired. The digest is the record's own for the same reason -- it is what
    the permit was granted over, re-derived and compared before the grant, and
    a reading taken here would fingerprint a checkout that has been writable
    since.

    The PROOF rides the same statement, and it is the one field here that is
    not about the move: it says which reading showed the push had landed, and
    nothing later can re-derive it. Kept on the comment until the record it
    belongs to reaches the sinks, so a process lost between this write and
    that record leaves the next reader something to report from rather than a
    settled transfer nobody ever announced.

    Staged rather than persisted, like every other writer in this domain: what
    makes the move durable is the caller's own write, which is what lets the
    exemption, the identity, and the account of what the remote now holds land
    together or not at all.

    Answers with the rewrite it spent, since the caller that has just moved a
    verdict is the one that owes a record of the move and holds nothing else
    to describe it from.
    """
    authorization = read_rewrite_authorization(state)
    if authorization is None:
        raise _formats.InvalidLateValue(
            "a rewrite publication has no authorization to spend",
        )
    if authorization.phase != LateRewritePhase.AUTHORIZED:
        raise _formats.InvalidLateValue(
            "a rewrite publication is not one still outstanding",
        )
    rewrite = authorization.rewrite
    _exemption.record_exemption(state, rewrite.to_sha)
    _exemption.record_semantic_identity(
        state,
        base_sha=rewrite.to_base_sha,
        candidate_sha=rewrite.to_sha,
        fingerprint=authorization.fingerprint,
    )
    state.set(LATE_REWRITE_PHASE, str(LateRewritePhase.PUBLISHED))
    state.set(LATE_REWRITE_PROOF, str(proof))
    return rewrite


def clear_rewrite_authorization(state: PinnedState) -> None:
    """Drop the whole authorization and the proof it would have reported.

    Every other field is left alone. The proof goes because it describes the
    transfer being dropped: kept, it would stand over no authorization at all,
    which every reader here refuses as damage.
    """
    for key in _AUTHORIZATION_KEYS:
        state.data.pop(key, None)
    forget_transfer_proof(state)
