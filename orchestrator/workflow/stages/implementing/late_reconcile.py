# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""A pair this issue froze for a published pull request and never counted.

The freeze is durable and the count that follows it is not, so a tick that dies
between them leaves a record naming both commits with no number on it -- and
nothing on the stage it was entered on would go back for that by itself. The
handler there spawns a reviewer, resumes a developer, or reads a pull request
still standing where the gate froze it, while the record goes on freezing the
branch out of the base refresh and describing a reading nobody took.

So the reading is taken HERE, ahead of every handler, and the three answers are
the gate's own: measured small retires the record and leaves the commit owed a
push, measured past the ceiling routes the issue to the adjudication, and a
refusal parks. Two states have no reading to take at all, and both stop the
tick rather than letting the stage carry on over unmeasured, unpushed work: a
checkout that is not on this host, and a record entered on a stage the issue
has since left.
"""
from __future__ import annotations

import logging

from github.Issue import Issue

from orchestrator import config
from orchestrator.git.base_sync import (
    frozen as _base_sync_frozen,
    state as _base_sync_state,
)
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.late_split.models import LateGeneration
from orchestrator.workflow.stages.implementing import (
    late_claims as _claims,
    late_debt as _debt,
    late_parks as _parks,
    late_push as _push,
    late_records as _records,
    late_rotation as _rotation,
    state as _state,
)
from orchestrator.workflow.state import WorkflowLabel

log = logging.getLogger("orchestrator.workflow")


# What the absent-checkout refusal is logged and reported as: not a reading
# that failed but one there is nowhere to take.
_ABSENT_CHECKOUT = "the checkout the frozen pair names is not on this host"


# What the stranded-reading refusal is logged and reported as.
_STRANDED_READING = "the frozen pair was entered on a stage this issue left"


_STRANDED_READING_PARK = (
    "{mentions} this issue froze `{candidate}` for a size reading on "
    "`{frozen}` and never finished it, and the issue is on `{label}` now. The "
    "reading may not be re-entered from there -- it would be measured against "
    "a publication it was never taken on -- and that stage may not run over "
    "it either, since the candidate is unmeasured and unpushed. Nothing was "
    "pushed and nothing was discarded. Put the label back, or repair the "
    "pinned comment, and the same pair is measured again."
)


# What a reconciliation whose own push did not land is reported as.
_UNPUBLISHED_RECONCILIATION = (
    "the candidate this reading allowed could not be pushed onto pull "
    "request #{number}"
)


_UNPUBLISHED_PARK = (
    "{mentions} this issue froze `{candidate}` for a size reading it never "
    "finished, and the reading taken now says it may join pull request "
    "#{number} -- but the push did not land, so the pull request has not "
    "received it and no stage has been run over it. A push refused here is "
    "usually the lease doing its job, which means something landed on that "
    "pull request while the reading was outstanding. Reconcile the branch "
    "with what landed and the same commit is published from there, without "
    "re-running any agent."
)


_ABSENT_CHECKOUT_PARK = (
    "{mentions} this issue froze `{candidate}` for pull request #{number} and "
    "has no checkout to measure it in, so nothing has been pushed and no "
    "stage has run over it: a candidate whose cumulative size is unknown is "
    "not a small one. The commit is on whichever host made it. Restore that "
    "worktree -- or repair the pinned comment if the work is gone -- and the "
    "same pair is measured again."
)


def _reconciles_published_work(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    label: WorkflowLabel | None,
    state: PinnedState,
) -> bool:
    """Answer a pair this issue froze and never counted, before anything else.

    The freeze is durable and the count is not, so a tick that dies between
    them leaves a generation naming both commits with no number on it. Nothing
    on the stage it was entered on would go back for that on its own: the
    handler spawns a reviewer, resumes a developer, or reads a pull request
    that is still standing on the head the gate froze -- while the record goes
    on freezing this branch out of the base refresh and describing a reading
    nobody took. So the reading is taken HERE, ahead of the handler, and the
    same answers apply: measured at or under the ceiling the candidate is
    published -- named against the commit that was measured and pinned to the
    head the pair froze -- measured past it the issue is routed to the
    adjudication, and a refusal parks. So does a push that was allowed and did
    not land, since a settled reading whose effect never happened is the one
    thing the stage may not run behind.

    Scoped by the record rather than by the label the issue happens to wear.
    Only a generation carrying a whole publication group is one of these, and
    only one recorded against the stage this issue is ON may be re-entered
    here -- a record entered on `fixing` and read while the issue sits
    somewhere else would be measured under a publication it was never taken
    on. That one is refused rather than waved past: the reading is still
    unresolved, the commit it named is still unpushed, and whichever stage the
    label now names would run over both.

    And only a generation that still OWES a count. A split that settled keeps
    the group for the releases and the branch delete its umbrella has left to
    do, and drops the measurement because a record answering "oversized"
    would pin `workflow:decomposing` -- which is the group with no number the
    reading above looks for, on an issue whose label has moved to
    `workflow:umbrella` by design. Read as a pair somebody froze, the finished
    adjudication is refused as one read off its own stage and the umbrella
    never releases another child; a park this owner already left on such a
    record is retired here rather than carried, since nothing about it is a
    human's to answer.

    True is a tick this owner finished. False is every other issue on every
    other tick, and also the small candidate this call just published: the
    record is gone, the pull request carries the commit, the debt it earned is
    paid, and the handler below carries on with an issue whose size question
    is answered and whose branch is where that answer says it may be.

    A record that CLAIMS one of these and cannot produce it is refused ahead
    of both, and that order is the point: every field here is read
    fail-closed, so a publication group missing a member parses as no group
    and an approval missing its lease parses as no approval -- and read that
    way, both of the questions below answer "nothing owed" and the stage runs
    over a claim nothing can check.

    A checkout that is not on disk stops the tick too. There is nothing to
    measure without one, and the stage below would carry on regardless -- the
    fixing bounce would relabel to `validating` and hand the reviewer a head
    the pull request never received, on a candidate nobody has read the size
    of. The recorded pair keeps the branch and the record exactly as they are
    until a host that has the checkout comes back, which is what the recorded
    pair is for.

    The record a settled TRANSFER never got to report is made here too, ahead
    of every one of those answers. That is a different debt with a different
    owner -- `late_rotation` makes it -- and what this seam supplies is the
    only tick that is guaranteed to come. The write receipting a landed push
    is what settles a transfer and the record of it goes to the sinks BEHIND
    that write, so a process lost between the two leaves a verdict that has
    moved and nothing anywhere saying so, over the one fact no later reading
    could re-derive. Every rewrite this workflow settles has that window,
    since all of them go through the same push tail: the squash a reviewer's
    approval earns, the replay `workflow:resolving_conflict` publishes, and
    the base refresh's own rebase. Only the last has a recovery route that
    would come back for it; the other two resume into a stage with nothing to
    say about a transfer, and this reconciliation is the seam all three reach.
    It settles nothing and stops nothing -- the verdict has moved and the
    receipt beside it already says which commit the remote holds -- so the
    answers below and the handler behind them run exactly as they would have.

    A note that cannot produce that account is a claim like any other here,
    and `_unreadable_record` above asks the transfer's own reader for it. The
    report reader cannot say so: it answers "nothing to report" for a note
    standing over a permission, a phase, or a reading nothing can account for
    exactly as it does for a comment carrying no note at all. Left at that,
    this seam walks past the one state it exists to close -- the account is
    never made, the corrupt note stands for the life of the issue, and the
    stage runs behind a verdict nothing here can account for. So it parks
    with the rest of the damage instead, once, and nothing is discarded for
    it.
    """
    recorded = _late_state.read_late_generation(state)
    damage = _claims._unreadable_record(label, state)
    owed = _debt._owes_a_published_push(label, state)
    gate = _records._gate(
        gh, spec, issue, state,
        _worktree_paths._worktree_path(spec, issue.number),
    )
    _rotation._reports_a_settled_transfer(gate)
    if damage:
        return _claims._parks_the_damage(gate, damage)
    if _defers_to_the_rebase_recovery(issue, state):
        return True
    if owed:
        return _debt._publishes_the_debt(gate, label)
    if _claims._awaits_its_count(recorded):
        return _answers_the_frozen_pair(gate, recorded, label)
    return _retires_a_settled_park(gh, issue, state, recorded)


def _retires_a_settled_park(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    recorded: LateGeneration,
) -> bool:
    """Answer the ordinary issue, which owes this reconciliation nothing.

    False every time, because nothing here stops a tick: what the pair before
    it looks for is not on this comment, so the label's own handler runs. The
    one write it may still make is the measurement park a split that has
    already become children carried past its own settlement -- nothing about
    that record is a human's to answer, and the branch it was freezing goes
    back into the base refresh with it.
    """
    if _parks._retire_settled_park(state, recorded):
        log.info(
            "issue=#%d carried a measurement park over a split that has "
            "already become children; clearing it and letting the "
            "label's own handler run",
            issue.number,
        )
        gh.write_pinned_state(issue, state)
    return False


def _defers_to_the_rebase_recovery(
    issue: Issue, state: PinnedState,
) -> bool:
    """Whether an interrupted auto rebase still owns what is standing here.

    The base refresh pins its anchor before `git rebase` runs and drops it
    only when the attempt is finished, reset, or parked -- so an anchor still
    on the comment at dispatch is an attempt no tick has resolved yet, and
    every window it can have been lost in is one where the branch is not
    where the pull request has it. Acted on here, whatever it left is
    answered by the wrong owner: this road publishes and settles the replay
    while the recovery's own finish -- which is what clears the anchor, resets
    the reviewer's round, and routes them at the rewritten head -- never
    happens. The stage then runs over a branch the refresh rewrote with the
    round the reviewer spent before the rewrite. The windows that leave
    NOTHING else on the comment are the same refusal with less to see: no
    debt and no count means this owner has no answer to give, so returning
    one lets the handler run behind the same unfinished recovery.

    So the tick stops instead, and nothing is written for it. The recovery
    reaches the same records on the refresh ahead of the next handler --
    which is normally the same tick, and a later one wherever the pull request
    could not be read -- and finishes them on its own terms or parks. Stopping
    is the fail-closed half of that: the stage may not run over a publication
    whose owner has not settled it yet.

    Unless the refresh is standing down for the very record this owner is
    holding, and that is not a courtesy either -- deferring there is a
    deadlock. The freeze ahead of the refresh sets one thing aside for this
    anchor and one only, the approval leased to it; every other record it
    reads holds the branch still, so a generation the gate froze and never
    counted stops the refresh on every tick while this owner waits for it. So
    the question is asked of the freeze itself rather than re-derived: a
    comment carrying nothing that holds the branch is one the recovery can
    reach, and a comment that holds it is this owner's to answer.
    """
    anchor = state.get(_base_sync_state._PENDING_PUSH_SHA)
    if not anchor:
        return False
    frozen_by = _base_sync_frozen._held_records(state)
    if frozen_by:
        log.info(
            "issue=#%d holds an auto rebase anchored at %s and a record that "
            "freezes the refresh out of it (%s); answering it here rather "
            "than waiting for a recovery that cannot run",
            issue.number, str(anchor)[:8], ", ".join(frozen_by),
        )
        return False
    log.info(
        "issue=#%d holds an auto rebase anchored at %s that no tick has "
        "finished; leaving what it recorded to that recovery rather than "
        "publishing it here and running the stage behind it",
        issue.number, str(anchor)[:8],
    )
    return True


def _answers_the_frozen_pair(
    gate: _records._Gate,
    recorded: LateGeneration,
    label: WorkflowLabel | None,
) -> bool:
    """Take the reading a pair frozen and never counted is owed, or refuse it.

    Scoped by the record rather than by the label the issue happens to wear:
    only one recorded against the stage this issue is ON may be re-entered
    here, since a pair frozen on `fixing` and read while the issue sits
    somewhere else would be measured under a publication it was never taken
    on. And there is nothing to measure without a checkout, which is its own
    refusal rather than a reason to let the stage carry on.
    """
    if recorded.source_stage != label:
        return _stranded_reading(gate, recorded, label)
    if not gate.worktree.exists():
        return _absent_checkout(gate, recorded)
    log.info(
        "issue=#%d records a frozen pair for pull request #%d with no count "
        "on it; measuring it before the stage runs",
        gate.issue.number, recorded.published_pr_number,
    )
    return _settles_the_frozen_pair(gate, recorded)


def _settles_the_frozen_pair(
    gate: _records._Gate, recorded: LateGeneration,
) -> bool:
    """Take the reading the crash interrupted, and spend what it earns.

    The whole gated call rather than its answer alone, because an allowed
    candidate earns a PUSH and this is the tick that owes it. Read for the
    answer only, the record retires naming a commit still owed a publication
    and the handler below runs over a pull request that never received it: the
    reviewer is spawned again over the head it already rejected, and an
    approval past that finds one commit on the branch, squashes nothing, and
    hands an unpushed head to the docs pass -- which reads it as recovered
    work and skips the pass it was relabelled for.

    So the effects come first and the stage runs behind them. Held is the tick
    this owner finished. Landed leaves the pull request carrying the commit,
    the debt paid, and the receipt written, so the handler runs over the same
    world the tick that froze the pair would have handed it. A push that was
    allowed and did not land is neither, and it stops the tick: the reading is
    settled but its effect is not, and the stage would run over a publication
    the branch never reached.

    What the hold owed is read BEFORE the call, because the retirement an
    allowed candidate earns drops the record those fields were written beside
    -- read after it they are gone. Which event closes them differs by exit: a
    routed hold spends inside the gate, ahead of the relabel it makes, and an
    allowed candidate is closed by the push it earns.

    That push carries them, in the write the receipt already makes rather than
    in one behind it. There is no tick behind this push to do the closing, so
    a second write is a window: a process dying in it comes back to a
    published commit, a paid debt, and an uncounted round with nothing left on
    the comment saying one was owed. Every gated push closes what its caller
    owed the same way and for the same reason; what is particular here is only
    where the pairs come FROM -- the record, since no run behind this tick
    could re-derive them.
    """
    owed = _records._Spends(fields=_late_state.read_late_spends(gate.state))
    published = _push._publishes(
        gate,
        _worktree_paths._resolve_branch_name(
            gate.state, gate.spec, gate.issue.number,
        ),
        # What the tick that froze this pair said its hold owed. Restored
        # rather than re-derived, because there is no run behind this one to
        # derive it from: the reviewer round a fix spends, the bookmarks a
        # consumed batch clears, the head a finished docs pass produced, the
        # outcome a resolution earned. Without it an oversized retry routes to
        # the adjudication having closed none of it, and the stage the
        # settlement hands back to reruns a developer over feedback that was
        # already answered.
        _records._Entered(
            # The reading this call is answering is the one the pinned record
            # names, which is what the switch has nothing left to say about:
            # publishing the head here would publish the very commit whose
            # reading somebody asked for.
            reconciling=True, answering=True, spends=owed,
        ),
    )
    if published.held:
        # No handler runs behind this, so the write the park's own caller
        # would have made is this owner's: a park posts its notice and leaves
        # the flags in memory, and one that never reached the pinned comment
        # is a mention nobody can answer on an issue nothing is waiting on.
        gate.gh.write_pinned_state(gate.issue, gate.state)
        return True
    if not published.landed:
        return _unpublished_reconciliation(gate, recorded)
    return False


def _unpublished_reconciliation(
    gate: _records._Gate, recorded: LateGeneration,
) -> bool:
    """Stop a tick whose reading was settled and whose push was not.

    The measurement is durable and the record it settled is gone, so nothing
    goes back for this on its own: what is left on the pinned comment is the
    approval naming the commit, and the retry publishes it from there. What
    may not happen meanwhile is the stage, which would work from a pull
    request the candidate never joined.
    """
    _parks._parked(
        gate, _records._reportable(gate, recorded),
        _UNPUBLISHED_RECONCILIATION.format(
            number=recorded.published_pr_number,
        ),
        _UNPUBLISHED_PARK.format(
            mentions=config.HITL_MENTIONS,
            candidate=recorded.candidate_sha,
            number=recorded.published_pr_number,
        ),
    )
    gate.gh.write_pinned_state(gate.issue, gate.state)
    return True


def _holds_absent_checkout(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
) -> bool:
    """Whether a frozen pair with no checkout stops a publication outright.

    The dispatcher asks the same question ahead of every handler, and this is
    the answer for the seam that reaches a missing checkout on its own: a
    publication that finds no worktree has always simply not published, and
    with a pair frozen and never counted that is not enough. The caller's next
    move -- the no-feedback bounce's relabel -- would hand the reviewer a head
    the pull request never received on a candidate nobody has read the size
    of, so the tick stops here instead.
    """
    recorded = _late_state.read_late_generation(state)
    if not _claims._awaits_its_count(recorded):
        return False
    return _absent_checkout(
        _records._gate(
            gh, spec, issue, state,
            _worktree_paths._worktree_path(spec, issue.number),
        ),
        recorded,
    )


def _stranded_reading(
    gate: _records._Gate,
    recorded: LateGeneration,
    label: WorkflowLabel | None,
) -> bool:
    """Stop a tick whose frozen pair belongs to a stage the issue has left.

    The record names one publication and one stage, and both are the terms the
    reading was taken under. Re-entering it here would measure it under a
    publication it was never taken on; letting the handler run instead is
    worse, because the reading is still unresolved and the commit it named is
    still unpushed -- so whichever stage the label now names would work over
    an unmeasured candidate and, on the roads that publish, push it.

    Nothing this process can repair either: the label was moved by something
    outside the gate, and only a human can say whether it should go back or
    the record should be dropped. So the refusal owes a human, and owes them
    one notice rather than one per poll.
    """
    if gate.state.get(_state._PARK_REASON) == _parks.PARK_MEASUREMENT_FAILED:
        log.warning(
            "issue=#%d still carries a frozen pair entered on %s while it is "
            "on %s; holding the tick without a second notice",
            gate.issue.number, recorded.source_stage, label,
        )
        return True
    log.error(
        "issue=#%d records an unmeasured candidate entered on %s and is on "
        "%s now; refusing to run that stage over a reading nothing settled",
        gate.issue.number, recorded.source_stage, label,
    )
    _parks._parked(
        gate, _records._reportable(gate, recorded), _STRANDED_READING,
        _STRANDED_READING_PARK.format(
            mentions=config.HITL_MENTIONS,
            candidate=recorded.candidate_sha,
            frozen=recorded.source_stage,
            label=label or "no workflow state",
        ),
    )
    gate.gh.write_pinned_state(gate.issue, gate.state)
    return True


def _absent_checkout(
    gate: _records._Gate, recorded: LateGeneration,
) -> bool:
    """Stop a tick whose frozen pair has no checkout to be measured in.

    Fail closed rather than open, because open means the stage runs: the
    bounce relabels, the reviewer is handed a head the pull request never
    received, and the candidate the record still names goes on being one
    nobody has read the size of. Nothing here can be repaired by this process
    either -- the commit is on a host this one is not -- so what the refusal
    owes is a human.

    Announced ONCE. The park is durable and the condition is not one that
    clears on its own, so a checkout that stays gone would otherwise put a
    fresh notice on the thread every poll and bury the first one. A tick that
    finds the park already standing is held silently, and the moment the
    checkout is back the ordinary reading resumes: the measurement park is
    retired by the freeze that re-reads the pair it names.
    """
    if gate.state.get(_state._PARK_REASON) == _parks.PARK_MEASUREMENT_FAILED:
        log.warning(
            "issue=#%d still has no checkout at %s for the pair it froze; "
            "holding the tick without a second notice",
            gate.issue.number, gate.worktree,
        )
        return True
    log.error(
        "issue=#%d records a frozen pair and has no checkout at %s to "
        "measure it in; refusing to run the stage over an unread candidate",
        gate.issue.number, gate.worktree,
    )
    _parks._parked(
        gate, _records._reportable(gate, recorded), _ABSENT_CHECKOUT,
        _ABSENT_CHECKOUT_PARK.format(
            mentions=config.HITL_MENTIONS,
            candidate=recorded.candidate_sha,
            number=recorded.published_pr_number,
        ),
    )
    gate.gh.write_pinned_state(gate.issue, gate.state)
    return True
