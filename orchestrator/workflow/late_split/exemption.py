# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The one commit an accepted candidate is let past the size gate on.

A `single` verdict says an oversized candidate is one coherent change after
all, and the whole of what that decision is worth has to outlive the
generation that earned it: the gate re-measures whatever a stage is about to
publish, so a candidate handed back with its generation cleared and nothing
else would be measured past the ceiling again and adjudicated again, forever.
The exemption is what breaks that loop, and it is a commit rather than a flag
for the same reason every other late field is a commit -- a flag would exempt
whatever the worktree ends on next.

So it names exactly the commit that was measured and adjudicated, and nothing
else is exempt. A developer who commits again after the verdict has produced
work nobody adjudicated: the recorded SHA no longer matches, the exemption
does not apply to it, and the gate measures it as a fresh candidate. That is
the invalidation rule in full -- there is no clearing step to remember and no
window in which a stale exemption covers a moved head.

Beside that commit, and never in place of it, sits what the commit CARRIES:
the pair the adjudication was taken between, the canonical digest of the
contribution between them, and the version of the scheme that digest was taken
under. The exemption says which COMMIT a human ruled on; the identity says
which CHANGE they ruled on, which is the only question left once the commit
itself is gone -- rebased, squashed, or committed afresh over a base that
moved. It is derived from the frozen pair the decomposer inspected and from
nothing else: a digest taken over whatever the checkout stands on now, or over
a base read now, would name work nobody adjudicated and would name it under
the authority of a verdict about something else.

The identity is whole or it is not there, and it belongs to the commit the
field beside it named when it was written -- so a write that moves that field
to another commit takes the identity with it, rather than leaving a record a
later verdict for the first commit could be read against. Every field reads
back through the same fail-closed readers the exemption does, the recorded
candidate has to BE the exempt commit, and the recorded version has to be the
one this build takes a digest under -- so a half-written record, a hand-edited digest, a pinned
comment from before this field existed, and an id taken under a scheme this
build does not compute each read back as no transferable identity at all.
None of that reaches the exemption: the exact commit stays exempt on its own
field, which is the claim the gate reads and the one thing here that may never
be widened by an unreadable value beside it.

They live outside `LATE_STATE_KEYS` on purpose. Clearing late mode is defined
as dropping exactly the generation's own keys, and the exemption is the single
thing that has to survive that clear -- it is what the reconciliation writes so
the generation can be cleared at all, and the identity is written with it and
survives on the same terms.
Reading and writing them is fail-closed like every other late field: only a
whole git object id is an exemption and only a whole digest is a fingerprint,
so a hand-edited value never becomes a bypass, and a write that was handed one
refuses rather than recording a field the gate would read.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from orchestrator.git.measurement.models import FINGERPRINT_FORMAT
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import formats as _formats, payloads as _payloads

# The commit a decided `single` verdict published under. Spelled here because
# this is the field's owner, and it is deliberately not one of the keys
# `clear_late_generation` drops.
LATE_EXEMPT_SHA = "late_exempt_sha"

# What that commit carries, spelled beside it because the identity is the
# exemption's own record and shares its whole lifetime. The two ends are the
# generation's frozen base and the candidate the verdict accepted -- the
# candidate is recorded rather than inferred from the exemption so a reader
# can PROVE the digest belongs to the exempt commit instead of assuming it.
LATE_EXEMPT_BASE_SHA = "late_exempt_base_sha"

LATE_EXEMPT_CANDIDATE_SHA = "late_exempt_candidate_sha"

LATE_EXEMPT_FINGERPRINT = "late_exempt_fingerprint"

LATE_EXEMPT_FINGERPRINT_FORMAT = "late_exempt_fingerprint_format"

# What each recorded hex field has to be, at its exact length: an end of the
# diff is a whole git object id, and the fingerprint is a whole digest. An
# abbreviation is not a commit this domain froze and a truncated digest is not
# a hash of anything, so neither is a value a comparison could be made on.
_IDENTITY_SHAPES = MappingProxyType({
    LATE_EXEMPT_BASE_SHA: _formats.COMMIT_LENGTHS,
    LATE_EXEMPT_CANDIDATE_SHA: _formats.COMMIT_LENGTHS,
    LATE_EXEMPT_FINGERPRINT: _formats.DIGEST_LENGTHS,
})

# The identity as one group, because it is written, dropped, and believed as
# one: it describes the commit on the field beside it and nothing else, so a
# write that moves that field takes the whole group with it rather than
# leaving members a later exemption could be read against.
_IDENTITY_KEYS = (
    *_IDENTITY_SHAPES,
    LATE_EXEMPT_FINGERPRINT_FORMAT,
)

# Everything one accepted candidate leaves on the pinned comment. The clear
# takes the group rather than the commit alone: an identity for a commit
# nothing exempts describes a decision this issue does not record.
_EXEMPTION_KEYS = (LATE_EXEMPT_SHA, *_IDENTITY_KEYS)


@dataclass(frozen=True)
class LateSemanticIdentity:
    """What an exempt commit contributes, once every field of it proved out.

    Handed out whole or not at all, so nothing downstream has to decide what
    half of one means. `exempt_sha` and `candidate_sha` are the same commit by
    construction and are both carried because the reader PROVES that rather
    than assuming it: they are separate pinned fields, and a comment where
    they disagree is one this domain did not write.

    `fingerprint_format` travels with the digest because a digest is only ever
    spent compared, and two taken under different rules are not comparable.
    """

    exempt_sha: str
    base_sha: str
    candidate_sha: str
    fingerprint: str
    fingerprint_format: int


def read_exemption(state: PinnedState) -> str | None:
    """Return the commit this issue currently exempts, or None.

    Read through the domain's own object-id reader, so an abbreviation, prose,
    or a value an older binary wrote in some other shape reads back as no
    exemption at all rather than as one nothing can be compared against.
    """
    return _payloads.as_hex(
        state.get(LATE_EXEMPT_SHA), _formats.COMMIT_LENGTHS,
    )


def record_exemption(state: PinnedState, candidate_sha: str) -> None:
    """Exempt exactly this commit from the size gate.

    Refuses anything that is not a whole git object id. The field is read by
    the gate that decides whether a candidate may publish, so a value that
    cannot name one commit is a bypass rather than a record -- and recording
    it here would move the failure onto the reader, which has a candidate in
    hand and nowhere to put it.

    Moving the field to ANOTHER commit drops the identity standing beside it,
    and that is what keeps a stale one from becoming believable again. An
    identity describes the commit the field named when it was written, and a
    verdict that records only the commit -- every one whose fingerprint could
    not be read -- writes nothing over it. Left there, it would go on matching
    by name alone: an issue that accepts commit A with an identity, then B
    with none, then A again with none would hand A's first digest back as what
    the last adjudication decided, over a base that generation never measured.

    Re-recording the SAME commit keeps it, which is what a retry needs. A
    settlement that crashed between this write and its handoff comes back and
    writes the exemption again, over an identity its own earlier pass derived
    from the very pair this one is still frozen on.
    """
    if not _formats.is_hex_of(candidate_sha, _formats.COMMIT_LENGTHS):
        raise _formats.InvalidLateValue(
            f"an exemption is not a commit ({type(candidate_sha).__name__})",
        )
    if read_exemption(state) != candidate_sha:
        for key in _IDENTITY_KEYS:
            state.data.pop(key, None)
    state.set(LATE_EXEMPT_SHA, candidate_sha)


def record_semantic_identity(
    state: PinnedState,
    base_sha: str,
    candidate_sha: str,
    fingerprint: str,
) -> None:
    """Record what the exempt commit contributes, over the pair it was read on.

    Written beside an exemption that is already down and validated against it:
    an identity naming any other commit would describe a change this issue
    never adjudicated, and would describe it under the authority of a verdict
    about something else, so it refuses rather than recording one.

    Every field is held to the shape it claims for the reason the exemption
    itself is -- a value that cannot name a commit or a digest is not one, and
    writing it would move the failure onto a reader that has a comparison to
    make and nothing sound to make it against.

    The version is this build's own rather than the caller's. What it says is
    which scheme the digest beside it was taken under, and only the owner that
    takes one can answer that.
    """
    for given, lengths in (
        (base_sha, _formats.COMMIT_LENGTHS),
        (candidate_sha, _formats.COMMIT_LENGTHS),
        (fingerprint, _formats.DIGEST_LENGTHS),
    ):
        if not _formats.is_hex_of(given, lengths):
            raise _formats.InvalidLateValue(
                f"a semantic identity is not one ({type(given).__name__})",
            )
    if read_exemption(state) != candidate_sha:
        raise _formats.InvalidLateValue(
            "a semantic identity is not the exempt commit's",
        )
    state.set(LATE_EXEMPT_BASE_SHA, base_sha)
    state.set(LATE_EXEMPT_CANDIDATE_SHA, candidate_sha)
    state.set(LATE_EXEMPT_FINGERPRINT, fingerprint)
    state.set(LATE_EXEMPT_FINGERPRINT_FORMAT, FINGERPRINT_FORMAT)


def read_semantic_identity(state: PinnedState) -> LateSemanticIdentity | None:
    """Return what this issue's exempt commit contributes, or None.

    None wherever the record cannot vouch for itself, which is every way it
    can fail to: a field that is missing, a group where only some of them are
    there, a value that is not the shape its field takes, a candidate that is
    not the commit the exemption names, a pinned comment written before this
    field existed, and a digest taken under a scheme this build does not
    compute. Each of those is an id nothing may act on, and answering with a
    partial one would hand a caller a comparison it has no grounds to make.

    What none of them touches is the exemption. The exact commit is exempt on
    its own field and stays exempt here, so a damaged identity costs a later
    tick the transfer and never the decision a human already made.
    """
    exempt = read_exemption(state)
    recorded = {
        key: _payloads.as_hex(state.get(key), lengths)
        for key, lengths in _IDENTITY_SHAPES.items()
    }
    if exempt is None or not all(recorded.values()):
        return None
    if recorded[LATE_EXEMPT_CANDIDATE_SHA] != exempt:
        return None
    written = _payloads.as_identity(state.get(LATE_EXEMPT_FINGERPRINT_FORMAT))
    if written != FINGERPRINT_FORMAT:
        return None
    return LateSemanticIdentity(
        exempt_sha=exempt,
        base_sha=recorded[LATE_EXEMPT_BASE_SHA],
        candidate_sha=recorded[LATE_EXEMPT_CANDIDATE_SHA],
        fingerprint=recorded[LATE_EXEMPT_FINGERPRINT],
        fingerprint_format=written,
    )


def unreadable_exemption(state: PinnedState) -> bool:
    """Whether this comment CLAIMS an exemption it cannot show whole.

    Presence rather than truth, and the question a caller asks before it acts
    on the ABSENCE of one. The fail-closed readers beside this answer "no
    exemption" and "no identity" for a record something damaged just as
    readily as for a comment that never had one -- which is the right answer
    for the gate, whose only move is to measure a candidate afresh, and the
    wrong one for a caller whose move is to walk past an issue as though no
    verdict were in flight. A half-written group, a hand-edited digest, and a
    field carrying `null` all read as nothing there, and an adjudicated commit
    would be left behind on the strength of it.

    Two shapes count. A comment carrying any member of the group whose
    exemption field cannot be read back as a commit is claiming one it cannot
    show. And an identity group with a member present that does not read back
    whole is a claim about what that commit contributes which nothing can
    check.

    The LEGACY shape is neither, and it is why the identity is asked by
    presence rather than by truth: a comment written before this group existed
    carries the exempt commit and nothing beside it, which is complete for
    what it says. It reads as sound here, and what it costs a later tick is
    the transfer rather than the verdict.
    """
    if not any(key in state.data for key in _EXEMPTION_KEYS):
        return False
    if read_exemption(state) is None:
        return True
    if not any(key in state.data for key in _IDENTITY_KEYS):
        return False
    return read_semantic_identity(state) is None


def clear_exemption(state: PinnedState) -> None:
    """Drop the exemption and its identity, leaving every other field alone."""
    for key in _EXEMPTION_KEYS:
        state.data.pop(key, None)


def is_exempt(state: PinnedState, candidate_sha: str) -> bool:
    """Whether THIS commit is the one an adjudication let through.

    Both sides have to be a whole object id and they have to be the same one.
    A candidate the caller could not name, and a recorded exemption that is
    not a commit, each answer False -- the gate's job is to measure what it
    cannot prove was already decided.

    The identity recorded beside the field is deliberately not consulted here.
    This is the claim the gate reads before anything publishes, and it is the
    exact one either way: a commit made on top of the accepted one is a
    different commit carrying different work, and it is measured as the fresh
    candidate it is whatever else the record remembers beside it.
    """
    exempt = read_exemption(state)
    if exempt is None:
        return False
    return exempt == _payloads.as_hex(candidate_sha, _formats.COMMIT_LENGTHS)
