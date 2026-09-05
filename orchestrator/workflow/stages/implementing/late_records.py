# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What one gate call is about, and the identities its records carry.

The tick's own subject and the answer it hands back sit here with the minting
that gives both a name. They are together because they are the same layer:
none of them reads a repository, writes a comment, or decides anything -- they
say what the call is ABOUT, so every owner past this one can be about the
candidate rather than about assembling the description of it.

Whether a recorded identity is one at all is answered here too, and in one
place on purpose. Every refusal in this domain is reported against a
generation, so an identity the sinks would refuse -- or one naming somebody
else's issue -- costs the report rather than merely the record: the failure
goes down with the pinned comment it was about, or is filed where nobody
looking at this issue would find it. The same answer decides whether a
recorded measurement may be acted on, so a record cannot be good enough to
publish on and too damaged to write down.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

from github.Issue import Issue

from orchestrator import config
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import (
    endings as _endings,
    formats as _formats,
    identity as _identity,
    lineage as _lineage,
    rewrites as _rewrites,
    validation as _late_validation,
)
from orchestrator.workflow.late_split.models import LateGeneration, LatePhase
from orchestrator.workflow.state import WorkflowLabel

log = logging.getLogger("orchestrator.workflow")

_UNRECORDABLE_IDENTITY = (
    "the identity it would be correlated by is not one a record may carry "
    "({refusal})"
)

_FOREIGN_RECORD = (
    "it was recorded against issue #{recorded} rather than this one"
)

@dataclass(frozen=True)
class _Spends:
    """The route bookkeeping a hold closes on its caller's behalf.

    A hold is the end of the tick for the caller: the commit is on the branch,
    the issue is on `workflow:decomposing`, and the caller returns without
    pushing, relabelling, or counting anything. But the round IS spent -- the
    head a reviewer rejected is superseded either way -- and no later tick of
    that stage can count it, because a settled `single` verdict publishes the
    accepted commit itself and the resumed stage finds nothing left to push.

    So the caller says up front what its hold owes, and the hold writes it in
    the same durable write that carries the measurement, AHEAD of the relabel.
    Applied by the caller afterwards instead, it would be lost to a crash in
    exactly the window the relabel opens: the issue is already the
    adjudication's and nothing goes back for the count.

    Spelled as pinned fields rather than as a call into the route's own owner,
    so a stage's bookkeeping stays that stage's to describe and this owner
    stays free of the stage packages that import it.
    """

    fields: tuple = ()


# What a caller with no route bookkeeping behind it owes, which is every
# publication taken before there is a pull request to spend a round on.
_SPENDS_NOTHING = _Spends()


def _spend(state: PinnedState, spends: _Spends) -> None:
    """Close the route bookkeeping a caller said its hold owed.

    Spelled beside the record rather than at either site that applies it, so
    the routed hold and the recovery that publishes what a crash interrupted
    write the same fields the same way. Both are the same claim -- this is
    what the tick that reached the gate would have done -- and only the moment
    differs: the hold writes it ahead of its own relabel, the recovery once
    the push it makes has landed.
    """
    for key, spent in spends.fields:
        state.set(key, spent)


@dataclass(frozen=True)
class _PublicationEntry:
    """The publication a gate call was entered on, or why there is none.

    What tells a candidate the remote already carries from one nothing has
    published, and the only three facts about it a reconciliation could not
    re-derive: the stage the gate is taking the issue out of, which the
    adjudication label replaces the moment it is applied; the pull request the
    work already has, which the hold beside it names only because this entry
    named it first; and the head that pull request was left standing on, which
    the next push to the branch moves. All three are read once, before any
    effect, and travel frozen for the same reason every other late field does.

    Absent where the gate was entered before anything was published, which is
    what the whole implementing seam is, and refused with its reason where the
    three could not be established -- `is_frozen` is what a caller asks, since
    a group short of any one of them is a publication nothing could name.
    """

    stage: WorkflowLabel | None = None
    pr_number: int = 0
    published_sha: str = ""
    refusal: str = ""

    @property
    def is_frozen(self) -> bool:
        """Whether this entry names a publication a record may carry."""
        return not self.refusal


@dataclass(frozen=True)
class _Gate:
    """The one candidate a gate call is deciding about.

    The worktree travels with the issue because the two are read together at
    every step and neither is derivable from the other here: the commit is
    proved, the base is frozen, and the diff is counted in that checkout,
    while the record, the park, and the label all belong to the issue.
    """

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    worktree: Path
    # Whether a developer ran on this tick. Nothing in the checkout can be a
    # run's output where none did, which is what makes a head that moved off
    # the recorded candidate something that moved rather than fresh work.
    reconciling: bool = False
    # Whether this tick is answering a reading a PREVIOUS one recorded. The
    # narrower of the two, and the one the switch is asked against: a rebase,
    # a resolution, and a recovery push are each work no developer ran for
    # and work the gate has never seen, so reading "no developer ran" as
    # "already in the gate" would measure candidates `DECOMPOSE=off` exists
    # to publish untouched.
    answering: bool = False
    # The commit the caller named as the one it means to publish, where it
    # read one for itself. Empty where the caller has none -- a bounce over a
    # checkout it did not just write, a recovery answering a recorded pair --
    # and the head this owner proves is the whole of the answer there.
    candidate: str = ""
    # The publication this call was entered on, where there is one. It is the
    # whole of what makes this gate reusable past the initial push: nothing
    # else here changes when the work already has a pull request, and every
    # record the call writes carries the group rather than reading as an entry
    # taken before anything was published.
    entry: _PublicationEntry | None = None
    # What this call's caller owes for the candidate, whichever way it goes.
    # A hold closes it ahead of the relabel it makes, and a landed push closes
    # it in the write that carries the receipt -- one durable write either
    # way, since past that write nothing on the comment names the round any
    # more. Empty for every publication with no round behind it, and for the
    # implementing seam, which has no reviewer to have spent one.
    spends: _Spends = _SPENDS_NOTHING
    # The rewrite this candidate came out of, where the caller made one. It
    # is the only evidence a permit may be granted on, and the only account
    # anything later has of how an exemption came to license a commit no
    # human ever saw. It is the caller's because everything in it is gone
    # from the checkout and the remote by the time this owner could ask.
    rewrite: _rewrites.LateRewrite | None = None
    # Whether a rewrite permit is the ONLY thing that may let this candidate
    # publish. Off for every ordinary caller, and on for the crash recovery,
    # which reaches this gate holding a transfer it already knows about.
    permit_only: bool = False


@dataclass(frozen=True)
class _Entered:
    """The terms the CALLER entered this gate call on.

    Every field is something this owner could read for itself and must not, or
    could not know at all. A stage read back off a cached issue names the
    label the fetch carried rather than the one a same-tick relabel wrote, and
    a head read again is not the head the caller pinned its own decision to.
    `reconciling` says no developer ran on this tick, which is what tells a
    checkout that MOVED from a resumed developer's fresh commit. `answering`
    is the narrower claim behind it -- that this call is answering a reading a
    previous tick RECORDED -- and it is what the switch is asked against, so a
    rebase or a recovery push that no developer ran for is still the new work
    `DECOMPOSE=off` publishes untouched. `spends` is the route bookkeeping a
    hold has to close on the caller's behalf. `rewrite` is the before-state a
    caller that REPLACED a commit destroyed getting here, which is the one
    thing no reading taken now could recover.

    Empty is the ordinary answer and means the caller established none of it:
    the label is current, the remote has not been read, a run has just
    finished, and there is no reviewer round behind the push.
    """

    stage: WorkflowLabel | None = None
    head: str = ""
    # The commit the caller means to publish, where it read one for itself.
    # This owner proves the checkout's head independently, and between the
    # caller's read and that one the worktree is writable -- so a commit
    # landing in the window would be measured, pushed, and recorded here while
    # the caller went on to stamp the id IT read. Named, the two are one
    # decision and a checkout that moved refuses before anything is persisted.
    candidate: str = ""
    reconciling: bool = False
    answering: bool = False
    spends: _Spends = _SPENDS_NOTHING
    # What the caller REWROTE to arrive at the candidate above, where it
    # rewrote anything. It is the evidence a change a human already
    # adjudicated may be recognized in a commit that did not exist when they
    # ruled on it, and it is handed in rather than read because a rewrite
    # destroys its own before-state.
    rewrite: _rewrites.LateRewrite | None = None
    # Whether the permit is the whole of what may license this publication.
    #
    # Off is the ordinary answer and the one every publishing seam gives: a
    # permit that refuses leaves the candidate to the cumulative reading, and
    # a count under the ceiling publishes the same commit on the count rather
    # than on the exemption. That is right for a caller DECIDING whether to
    # publish a rewrite it has just made.
    #
    # On is the recovery, and it is right there for the opposite reason. It
    # holds a transfer it already knows about -- a permission the grant left,
    # or evidence re-derived from the record -- and what it is finishing is a
    # publication, not deciding one. Measured instead, a count under the
    # ceiling would report the recovery as landed with the verdict still on
    # the commit a human ruled on, and a count over it would route an
    # adjudicated change into a second adjudication with the pull request
    # already open over the work.
    permit_only: bool = False


# What a caller that established nothing hands in.
_UNENTERED = _Entered()


@dataclass(frozen=True)
class _GateVerdict:
    """What the gate decided, and the exact commit it decided about.

    The SHA travels because the caller's next step is a PUSH, and a push that
    named nothing would publish whatever the checkout points at when it runs.
    Everything this gate does is a claim about one object id -- it proved that
    commit, measured that commit, and recorded that commit -- so handing back
    a bare "go ahead" would drop the one fact the publication needs to be the
    same event the measurement was about.

    Empty where this gate has nothing to name: a candidate the switch kept
    out of it was never proved here, so there is no commit THIS answer can be
    published under. The publication resolves the checkout's own head there
    rather than pushing an unnamed branch -- the switch keeps candidates out
    of the measurement, not out of the record of what went out.

    `permitted_sha` is the second commit and answers a different question: not
    "may this publish" but "did a rewrite permit prove out for it on THIS
    tick". It is empty for every road but one, the ordinary measurement's
    included -- a permit that refused leaves the candidate to the cumulative
    gate, and a candidate the gate then lets through publishes on the count
    rather than on the exemption.

    The two are separate because the write past the push turns on the second
    and only the second. A permission standing on the comment is evidence a
    permit was once granted, not that it still holds: a repointed pull
    request, a relabelled issue, a moved remote, or a contribution that no
    longer fingerprints alike each refuse it while the ordinary reading may
    still publish the same commit. Read off the record instead, that
    publication would rotate a human's verdict onto a rewrite this tick
    declined to vouch for.


    `refused` is the third answer, and only a `permit_only` caller can get
    it: the permit declined and nothing was measured in its place. It is a
    hold like any other -- the caller publishes nothing -- except that the
    gate has taken no park and made no route, so the caller owns what happens
    next.
    """

    held: bool
    candidate_sha: str = ""
    permitted_sha: str = ""
    refused: bool = False


# What a permit-only caller gets when its permit declines: nothing measured,
# nothing parked, nothing routed, and the answer handed back for the caller to
# fail closed on.
_REFUSED = _GateVerdict(held=True, refused=True)


# What every held answer is, since a hold names no commit: there is nothing
# for the caller to publish and nothing for it to publish it under.
_HELD = _GateVerdict(held=True)


def _gate(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    worktree: Path,
) -> _Gate:
    """The subject one gate call is about, from what its caller was handed.

    A factory rather than five keywords at each site: every owner in this
    domain opens by building one, and spelling the same five fields out again
    each time is how one of them ends up carrying a different worktree from
    the checkout it is reading.
    """
    return _Gate(
        gh=gh, spec=spec, issue=issue, state=state, worktree=worktree,
    )


def _minted(
    gate: _Gate,
    recorded: LateGeneration,
    candidate_sha: str,
    base_sha: str,
) -> LateGeneration:
    """The generation this freeze records, under identities nothing reused.

    The cycle is this issue's own while one is live, and the number after the
    one a retirement dropped otherwise, so two attempts at the same issue are
    never the same attempt in a record. The generation counter advances with
    every candidate frozen inside a cycle, which is what keeps a verdict
    recorded against an earlier commit from reading as an answer to this one.

    The lineage comes off the ancestry wherever there is one, because that is
    the record a split WROTE about this issue and the one the split
    transaction checks its own generation against. An issue no split created
    is the root of its own lineage at depth 0; one whose ancestry records no
    readable depth stays unknown, which reads as "may not split" rather than
    as a root with room to spare.

    The readings a retry has already lost travel with the CANDIDATE rather
    than with the generation counter beside it, and that is what makes the
    bound reachable at all: a base the remote would not name records no base,
    so the next tick freezes afresh -- under a new generation, over the same
    commit -- and a count reset there would start every retry of that pair
    back at nothing. A candidate that MOVED is fresh work whose reading nobody
    has lost yet, so its own count starts at zero rather than inheriting one
    taken over a commit nothing measures any more.
    """
    return replace(
        _identified(gate, recorded),
        candidate_sha=candidate_sha,
        base_sha=base_sha,
        threshold=config.MAX_ADDED_LINES,
        additions=None,
        **_misses_of(recorded, candidate_sha),
    )


def _misses_of(recorded: LateGeneration, candidate_sha: str) -> dict:
    """What a mint over this commit carries of the readings already lost."""
    if recorded.candidate_sha != candidate_sha:
        return {"measurement_miss_count": 0, "measurement_failure": None}
    return {
        "measurement_miss_count": recorded.measurement_miss_count,
        "measurement_failure": recorded.measurement_failure,
    }


def _identified(gate: _Gate, recorded: LateGeneration) -> LateGeneration:
    """The identities and the lineage a record of this attempt is joined by.

    Everything a record needs to be correlatable and nothing about a commit,
    so a failure taken before either end of the diff was established is still
    reportable under the cycle a later freeze writes.
    """
    ancestry = _lineage.read_late_ancestry(gate.state)
    root, depth = _lineage_of(gate, recorded, ancestry)
    return _entered(gate, replace(
        recorded,
        cycle_id=recorded.cycle_id or _identity.next_identity(
            _endings.read_retired_cycle(gate.state),
        ),
        generation=_identity.next_identity(recorded.generation),
        root_issue=root,
        current_issue=gate.issue.number,
        lineage_depth=depth,
        scope=recorded.scope or ancestry.scope,
        phase=LatePhase.MEASURING,
    ))


def _entered(gate: _Gate, generation: LateGeneration) -> LateGeneration:
    """Stamp the publication this call was entered on onto one record.

    Every record a call writes goes through here, the measurement and the
    refusal alike, because the group is context rather than a result: what an
    operator has to be able to ask of a stream is which of two questions a
    record answers -- whether an unpublished candidate may be pushed at all,
    or whether a pull request the remote already carries may be pushed to
    again -- and a failure taken before either end of the diff was established
    is as much one of those as a count is.

    A call entered before anything was published stamps nothing, and that
    absence IS the answer: a record with no group describes an initial
    publication, which is what every record written from the implementing seam
    is and what a live pinned comment already says without having been
    migrated to say it.

    A record that already carries a WHOLE group is left exactly as it is, and
    that is a refusal rather than an optimization: the evidence a generation
    was frozen against is the evidence it is reconciled against, and a stamp
    that replaced it would let a reading taken over one publication be settled
    against another. The caller proves the two agree before anything reaches
    here -- a group that disagrees, or one too damaged to compare, refuses the
    tick outright -- so the only thing this can be asked to overwrite is a
    group identical to what it holds.
    """
    if gate.entry is None or generation.has_publication_context:
        return generation
    return generation.with_publication(
        stage=gate.entry.stage,
        pr_number=gate.entry.pr_number,
        published_sha=gate.entry.published_sha,
    )


def _lineage_of(
    gate: _Gate, recorded: LateGeneration, ancestry: _lineage.LateAncestry,
) -> tuple:
    """The root and the depth this generation is minted at.

    Both together because both come from the same place and have to agree: the
    ancestry is the record a SPLIT wrote about this issue, and the split
    transaction later checks its own generation against exactly that pair. A
    root taken from one source and a depth from another is the disagreement
    that refusal exists to catch.

    An issue no split created is the root of its own lineage at depth 0. One
    whose ancestry records no readable depth keeps that unknown rather than
    being read as a root, because a lineage that cannot show it has room may
    not split -- and reading a damaged field as 0 is how one buys itself
    another generation past the bound.
    """
    if ancestry.is_present:
        return ancestry.root_issue, ancestry.lineage_depth
    root = recorded.root_issue or gate.issue.number
    return root, (recorded.lineage_depth if recorded.is_present else 0)


def _unusable_identity(gate: _Gate, recorded: LateGeneration) -> str | None:
    """Why this record is no generation of THIS issue, or None if it is.

    Asked through the domain's own record gate rather than by a second reading
    of the same fields, so the identity a record may be ACTED on under is
    exactly the identity a record of it may be WRITTEN under -- a rule spelled
    twice would let the pinned comment publish what the sinks refuse.

    The issue is the one part that gate cannot ask, because it does not know
    which issue is being decided. A positive `late_current_issue` is not the
    same claim as one naming this one: a record carrying somebody else's
    number describes a reading taken over there, and both sinks would file
    this issue's failure against that one.
    """
    try:
        _late_validation.check_generation(recorded)
    except _formats.InvalidLateValue as refused:
        return _UNRECORDABLE_IDENTITY.format(refusal=refused)
    if recorded.current_issue != gate.issue.number:
        return _FOREIGN_RECORD.format(recorded=recorded.current_issue)
    return None


def _named(
    gate: _Gate, recorded: LateGeneration, candidate_sha: str,
) -> LateGeneration:
    """The record an attempt that NAMED a commit is retried under.

    A reading can fail with an id in hand: a revision that resolved and would
    not peel is the commonest, and the id it resolved to is the only record of
    which commit the attempt was about. Minting a generation around it is what
    turns "we could not read something" into "we could not read THIS", which
    is the difference between a retry that asks for one exact object and one
    that proves whatever the checkout points at by then.

    An attempt that named nothing, and one whose id the record already
    carries, are both left as they are: the first has no commit to mint
    around, and the second is already the record the retry will read.
    """
    if not candidate_sha or recorded.candidate_sha == candidate_sha:
        return _reportable(gate, recorded)
    return _minted(gate, recorded, candidate_sha, "")


def _reportable(gate: _Gate, recorded: LateGeneration) -> LateGeneration:
    """The identity a failure is reported under, minted where there is none.

    A candidate the gate could not name is one no generation has been written
    for, and a record with no cycle is exactly what the sinks may not carry --
    so the identity is minted here rather than the failure going unreported.
    A DAMAGED record is the same problem wearing a cycle: the record gate
    refuses it just as flatly, and a record whose `late_current_issue` names
    another issue is worse than refused, since both sinks would accept it and
    file this issue's failure over there. Either way the refusal would be lost
    with the record it is about -- which is precisely the failure an operator
    has to be told about -- so the whole identity is asked, not just the
    cycle.

    Minted identities are deliberately not PERSISTED: a pinned record naming a
    cycle and no candidate freezes nothing, reconciles nothing, and would be
    read as a live cycle by the guard that ends one when the issue is closed.
    Minting is stable across retries -- the cycle is derived from what the
    record already says -- so a reading that keeps failing reports the same
    correlation each time rather than a new attempt per tick.
    """
    if _unusable_identity(gate, recorded) is None:
        return recorded
    return _identified(gate, recorded)
