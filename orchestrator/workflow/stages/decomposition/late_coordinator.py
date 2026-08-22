# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""One late adjudication, from the plan-PR hold to the run it settles.

The coordinator an oversized committed candidate is adjudicated by. It is
callable and complete, and nothing calls it yet: the seam that decides a
candidate is oversized -- the clean-committed pre-publication boundary -- is
wired separately, so this owner can be exercised on its own before any real
publication depends on it. What a finished reply then becomes is the
`late_outcome` owner beside it.

The order is the contract, and each step persists what it reached before it
acts. A generation that is not a live oversized one is not this owner's
business at all. Past that, the plan PR is held BEFORE anything is spawned,
because the hold is what stops a human from merging a change while the
question of whether it should exist as one issue is still open -- so a hold
that could not be reconciled parks and spawns nothing, and every retry
re-reconciles the same pull request rather than mutating it again. Then a
result already recorded for this cycle, generation, and exact commit short
circuits the spawn entirely: an agent that finished is not paid for twice
because the tick that read its answer died before acting on it, and a second
run is free to decide differently. Either way, the park a previous attempt
left is retired the moment the hold reconciles -- that attempt is the answer
to it, and a stale `awaiting_human` would go on to silence the announcement a
question verdict earns, whether the question came from this run or from a
recorded one whose own announcement never landed.

What the humans have said since the candidate was frozen is settled next, and
deliberately before that short circuit rather than after it: an answer to a
categorized question has to be able to drop the recorded outcome, and a
recorded outcome consulted first would suppress the very spawn the answer
earns. It is also where the whole tick can end -- requirements that moved park
the candidate without discarding it, and guidance that resumes the developer
re-freezes and re-measures the candidate this call was about, so there is
nothing left for the same tick to adjudicate. Everything that owner stages, it
persists, which is what lets the retired park be handed on rather than written
twice.

Past all of that, what comes back is the whole outcome rebuilt from the record
-- the children a split named included -- and whatever that answer still owed
the issue is reconciled instead of re-earned.

Every completed run goes through one more gate before anything acts on what
it left: the owner is read again. This call began by fetching an issue and
then spent minutes to hours running an agent, so the snapshot it is holding
cannot say whether a human has closed the issue since -- and publishing,
snapshotting, superseding, activating, or even announcing on the strength of
it would act on an issue nobody wants. It is asked of every completion, a
question and a timeout included, since a closure during one of those strands
the same generation and the same plan-PR hold. A read that fails records
itself on the generation, which is why the very first thing this call does is
reconcile one an earlier tick left owed -- ahead of the live-generation gate,
because the state that gate routes past is exactly where such a read gets
stranded. What the read costs each of the three answers is the `late_owner`
owner's; what a verdict past it EARNS is `late_settlement`'s; and the
transaction a cleared `split` becomes -- the snapshot every child is cut from,
the children themselves, the supersession of the held plan PR, and the cleanup
obligation left behind -- is `late_transaction`'s, which this owner reaches
only through the guarded handoff that read carries.

Nothing gets that far on a generation that cannot be acted on. The prompt, the
hold, and every record afterwards are derived from the frozen fields, so the
identities and both commits are proved before the plan PR is touched or an
agent is started: a candidate whose base was never recorded produces a diff
against nothing, and finding that out from a refused telemetry record means
the run has already been paid for.

Everything after the spawn is the shared post-run contract every other stage
runs: a `paused` label applied mid-run wins over the whole disposition and
leaves durable state exactly as the prior tick left it, a timeout parks, and
an interrupted run is dropped without being interpreted. The usage fold, the
retry budget, and the tracked spawn are the ones the rest of the workflow
already uses -- late adjudication spends the same per-issue budget as any
other decomposing run and is attributed to the same stage. "Leaves durable
state exactly as the prior tick left it" is what makes the pre-spawn write
the one place accounting is held back: the late identity goes out before the
agent starts, the retry slot does not, and a declined run therefore costs the
issue nothing.

One check has no counterpart in the initial mode, because the initial
decomposer runs in a scratch checkout and this one does not. The late
adjudicator reads the frozen candidate in the developer's OWN worktree, and
the CLI it runs under can write whatever it likes there whatever the prompt
says. So the candidate is proved unmoved and the tree proved clean before the
reply is read at all: an agent that committed over the evidence, or left
changes beside it, has contaminated the one artifact every later step acts on,
and its verdict is worth nothing next to that.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator.agents import AgentResult
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.late_split import formats as _formats
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.late_split import validation as _late_validation
from orchestrator.workflow.late_split.models import (
    LateFailure,
    LateGeneration,
    LatePhase,
)
from orchestrator.workflow.stages.decomposition import (
    late_guidance as _late_guidance,
)
from orchestrator.workflow.stages.decomposition import late_hold as _late_hold
from orchestrator.workflow.stages.decomposition import (
    late_outcome as _late_outcome,
)
from orchestrator.workflow.stages.decomposition import late_owner as _late_owner
from orchestrator.workflow.stages.decomposition import (
    late_session as _late_session,
)
from orchestrator.workflow.stages.decomposition import (
    late_settlement as _late_settlement,
)
from orchestrator.workflow.stages.decomposition import (
    late_transaction as _late_transaction,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateAdjudicationRun,
    _LateContext,
    _LateDisposition,
    _LateRun,
    _OwnerState,
)
from orchestrator.workflow.stages.implementing import session as _dev_session

log = logging.getLogger("orchestrator.workflow")

_DECOMPOSING_STAGE = "decomposing"

_LAST_AGENT_ACTION_AT = "last_agent_action_at"

# The per-issue accounting the pre-spawn write leaves exactly as it found it.
# Both fields move together -- an expired window is reopened at zero before it
# is incremented -- so a write that carried one without the other would record
# a count against a window nothing opened.
_ACCOUNTING_FIELDS = ("retry_count", "retry_window_start")

_HOLD_FAILED_PARK = (
    "could not put the adjudication hold on this issue's plan PR, so no late "
    "decomposer was spawned and the committed candidate stays unpublished. "
    "Settle the pull request, then the next tick retries the same "
    "reconciliation against the same frozen commit."
)

_HOLD_DISPLACED_PARK = (
    "this issue's plan PR carries a description this orchestrator did not "
    "write, so the adjudication hold cannot be put back without overwriting "
    "it -- and no late decomposer is started while that pull request is open "
    "with nothing on it saying the committed candidate is still being "
    "adjudicated. Settle the pull request, or put its description back, and "
    "the next tick continues against the same frozen commit."
)

_INCOMPLETE_PARK = (
    "this issue records an oversized committed candidate that cannot be "
    "adjudicated: {reason}. Nothing was spawned and no pull request was "
    "touched. The recorded generation has to be repaired -- what an agent "
    "would be shown is derived from those fields, and a diff taken against a "
    "commit nobody froze is not a reading of this candidate."
)

_MISSING_WORKTREE_PARK = (
    "the committed candidate for this issue is not on this host: its "
    "worktree is gone, and the frozen commit cannot be adjudicated without "
    "it. Restore the worktree on this host rather than re-running the "
    "developer -- the recorded commit is the evidence, not the current head."
)

_TIMEOUT_PARK = "late decomposer timed out after {seconds}s"

_MOVED_HEAD_PARK = (
    "the late decomposer was read-only, but the candidate worktree is no "
    "longer on the frozen commit {frozen}. Its verdict is not being used. "
    "Put the worktree back on that commit before resuming -- the recorded "
    "SHA is the evidence every later step acts on, and whatever HEAD points "
    "at now is not it."
)

_DIRTY_TREE_PARK = (
    "the late decomposer was read-only, but it left changes in the candidate "
    "worktree (or the tree could not be read). Its verdict is not being "
    "used. Clean the worktree back to the frozen commit {frozen} before "
    "resuming, so the candidate a later step publishes is the one that was "
    "measured."
)


def _adjudicate_late_generation(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
) -> _LateAdjudicationRun:
    """Adjudicate this issue's recorded late generation, if it has a live one.

    The whole late question in one call: hold the plan PR, settle what the
    humans have said since the candidate was frozen, then reuse a completed
    answer or spawn for a new one and record what it decided. Nothing is
    published here and no label is written -- the caller owns what a verdict
    earns.
    """
    context = _LateContext(
        gh=gh,
        spec=spec,
        issue=issue,
        state=state,
        generation=_late_state.read_late_generation(state),
    )
    blocked = _blocked_before_running(context)
    if blocked is not None:
        return _late_outcome._finished(context, blocked)
    retired = _late_outcome._retire_park(context)
    settled = _late_guidance._reconcile_late_content(context)
    if settled.disposition is not None:
        return _late_outcome._finished(context, settled.disposition)
    retired = retired and not settled.persisted
    recorded = _late_session._read_late_run(state)
    if recorded.answers(context.generation):
        log.info(
            "issue=#%d late generation %d already decided as %s; not "
            "spawning a second adjudication",
            issue.number, context.generation.generation, recorded.verdict,
        )
        return _guarded(
            context, _late_outcome._reused(context, recorded, retired=retired),
        )
    return _run_and_decide(context)


def _blocked_before_running(
    context: _LateContext,
) -> Optional[_LateDisposition]:
    """What stops this tick before any agent could run, if anything does.

    The owner read this generation still owes is asked ahead of every other
    gate, because a read that is owed is owed whether or not the candidate is
    adjudicable: a revision that came back under the ceiling routes past the
    size gate, and an issue parked for a human routes past everything, so a
    retry hung off either would never run at all.

    A park notice a refused comment stranded is redelivered next, for the
    same reason and one step later: it is owed whether or not the candidate
    is adjudicable, and every gate below this one is a gate a parked issue
    routes past. One step later because the read comes first -- an owner that
    turns out to be closed cancels the cycle, and a cancelled cycle's parks
    explain a candidate nobody is adjudicating any more.

    What comes before BOTH is reconciling that obligation against the thread,
    since the read is one of the steps that would otherwise read it wrong: a
    notice whose post landed and whose write did not is owed by the record and
    not by the issue, and the guard would take it as proof that nobody was
    told and clear its park without the follow-up it promised.

    The evidence and the hold are asked as one short circuit past all of it.
    Both park on their own and both stop the tick, and the hold is still only
    reached by a generation whose evidence was proved first.
    """
    _late_outcome._reconcile_notice_delivery(context)
    owed = _late_owner._reconcile_pending_owner_check(context)
    if owed is not None:
        return owed
    _late_outcome._redeliver_park_notice(context)
    if not _is_adjudicable(context.generation):
        return _LateDisposition.NOT_LATE
    if not _has_frozen_evidence(context) or not _hold_plan_pr(context):
        return _LateDisposition.PARKED
    return None


def _guarded(
    context: _LateContext, finished: _LateAdjudicationRun,
) -> _LateAdjudicationRun:
    """Read the owner again now this run is over, then act on what it left.

    Every completion comes through here, not only the ones that decided
    something: a question, a timeout, an unusable reply, and a reply refused
    for a moved candidate are all runs the issue paid for, and a closure
    during any of them strands the same generation and the same plan-PR hold.

    A run the tick DECLINED is the one exception, and it is not a completion:
    an operator's `paused` label and a shutdown sweep both mean this tick did
    not happen, and durable state has to be left exactly as the prior tick
    left it -- which a write here would break.

    A split is the one verdict the settlement does not finish. It hands back
    an outcome carrying the guarantee the transaction cannot check for itself
    -- that this verdict was re-checked against an owner read taken after the
    agent finished -- and the transaction that creates the children runs from
    here, past that read, on that handoff and on no other shape.
    """
    if finished.disposition == _LateDisposition.DEFERRED:
        return finished
    reading = _late_owner._guarded_owner(context)
    if reading == _OwnerState.CLOSED:
        return _late_outcome._finished(context, _LateDisposition.CANCELLED)
    if reading == _OwnerState.UNREADABLE:
        return _late_outcome._finished(context, _LateDisposition.PARKED)
    settled = _late_settlement._settle_adjudication(context, finished)
    if settled.guarded_split is None:
        return settled
    return _late_transaction._run_late_split(context, settled)


def _is_adjudicable(generation: LateGeneration) -> bool:
    """Whether this issue carries a live oversized generation to adjudicate.

    An absent generation is an issue that never entered the gate, a measured
    candidate at or below its ceiling is one that publishes as it always did,
    and a cancelled cycle is cleanup-only -- none of the three may spawn.
    """
    return (
        generation.is_present
        and generation.is_oversized
        and not generation.cancelled
    )


def _has_frozen_evidence(context: _LateContext) -> bool:
    """Prove this generation is one that may be acted on, or park loudly.

    Everything past this point is derived from the record: the prompt names
    both commits and tells the agent to diff between them, the hold marks a
    pull request in the generation's name, and the verdict is reported under
    its identities. A generation missing one of those does not produce a
    smaller reading of the candidate -- it produces a `git diff` against
    nothing and a record two sinks would refuse afterwards, having already
    paid for the run that made it.

    Nothing is emitted for the refusal. A record with no usable identity is
    exactly what the sinks may not carry, so the report is the park and the
    log line, which name the field and not its content.
    """
    unusable = _incomplete_evidence(context.generation, context.issue.number)
    if unusable is None:
        return True
    log.error(
        "issue=#%d has an oversized late generation that cannot be "
        "adjudicated (%s); parking rather than spawning",
        context.issue.number, unusable,
    )
    _late_outcome._park(
        context,
        _INCOMPLETE_PARK.format(reason=unusable),
        reason=_late_outcome.PARK_INCOMPLETE,
    )
    return False


def _incomplete_evidence(
    generation: LateGeneration, issue_number: int,
) -> Optional[str]:
    """Why this generation may not be adjudicated here, or None if it may.

    The domain's own record gate answers the first part -- the identities a
    later record is correlated by, and the shape of every field one would
    carry. Both frozen commits are required beside it: they are optional to
    that gate because a restart's fresh cycle has deliberately let them go,
    and they are not optional here, because they are the two ends of the diff
    this whole adjudication is about.

    The last part is the one the gate cannot ask, because it does not know
    which issue is being adjudicated. A generation is a record ABOUT an issue,
    and a positive `late_current_issue` is not the same claim as one naming
    THIS issue: a record carrying somebody else's number would show the agent
    a prompt that names two issues, mark a pull request in a foreign
    generation's name, and file the verdict against the issue it names rather
    than the one it ran on.
    """
    try:
        _late_validation.check_generation(generation)
    except _formats.InvalidLateValue as refused:
        return str(refused)
    if not generation.candidate_sha or not generation.base_sha:
        return "the frozen candidate and base commits are not both recorded"
    if generation.current_issue != issue_number:
        return f"it was recorded against issue #{generation.current_issue}"
    return None


def _hold_plan_pr(context: _LateContext) -> bool:
    """Reconcile the cycle-marked hold, or park without spawning."""
    context.generation = replace(
        context.generation, phase=LatePhase.HOLDING_PLAN_PR,
    )
    hold = _late_hold._reconcile_plan_pr_hold(
        context.gh, context.issue, context.state, context.generation,
    )
    context.generation = hold.generation
    context.displaced_hold = hold.displaced
    if not hold.failed:
        return True
    _late_outcome._emit_failure(context, LateFailure.PLAN_PR_HOLD_FAILED)
    _late_outcome._park(
        context, _HOLD_FAILED_PARK, reason=_late_outcome.PARK_HOLD_FAILED,
    )
    return False


def _run_and_decide(context: _LateContext) -> _LateAdjudicationRun:
    """Spend a retry slot on one adjudication of the frozen candidate.

    A hold a human displaced stops this the way a failed one does. Their words
    are left where they wrote them, but the pull request is now open with
    nothing on it saying an adjudication is running -- so no agent is started
    under it. This refusal is here rather than beside the hold because an
    answer already recorded is still allowed to settle: settling releases a
    hold that is already gone, and only a NEW run would leave a human free to
    merge under one.
    """
    if context.displaced_hold:
        _late_outcome._emit_failure(context, LateFailure.PLAN_PR_HOLD_FAILED)
        _late_outcome._park(
            context, _HOLD_DISPLACED_PARK,
            reason=_late_outcome.PARK_HOLD_FAILED,
        )
        return _late_outcome._finished(context, _LateDisposition.PARKED)
    worktree = _worktree_paths._worktree_path(
        context.spec, context.issue.number,
    )
    if not worktree.exists():
        _late_outcome._park(
            context, _MISSING_WORKTREE_PARK,
            reason=_late_outcome.PARK_WORKTREE_MISSING,
        )
        return _late_outcome._finished(context, _LateDisposition.PARKED)
    unspent = _accounting(context.state)
    if not _dev_session._check_and_increment_retry_budget(
        context.gh, context.issue, context.state, stage=_DECOMPOSING_STAGE,
    ):
        _late_outcome._persist(context)
        return _late_outcome._finished(context, _LateDisposition.PARKED)
    started = _late_session._spawn_record_for(
        context.state, context.generation, resuming=context.answering,
    )
    _begin(context, started, unspent)
    return _settle(
        context,
        _late_session._spawn_late_adjudicator(context, started, worktree),
        worktree,
    )


def _begin(
    context: _LateContext, run: _LateRun, unspent: dict,
) -> None:
    """Record what this run IS, and the phase it reached, before it starts.

    Deliberately NOT the accounting. The identity of the attempt has to be
    durable before the agent starts -- it is what a crashed tick reads back
    instead of paying for a second run -- but the retry slot this run holds
    must not be, because a run the tick then declines is one every other stage
    drops by returning without writing. Flushing the slot here would spend the
    issue's daily budget on a run whose outcome nothing kept, so a shutdown
    sweep landing on late adjudication over and over could exhaust the cap
    without ever producing an answer. So the write goes out with the counters
    as they stood, and the increment becomes durable only on a path that
    records what the run decided.
    """
    # Past this point the tick has an agent's answer to report, so a park it
    # takes is news even when it carries the reason the last one did: a second
    # question is a different question. What the retired-park memory quiets is
    # the reconciliation retry that spawned nothing and found the same wall.
    context.retired_park = None
    context.generation = replace(
        context.generation, phase=LatePhase.ADJUDICATING,
    )
    _late_session._record_late_spawn(context.state, run)
    spent = _accounting(context.state)
    _apply_accounting(context.state, unspent)
    _late_outcome._persist(context)
    _apply_accounting(context.state, spent)


def _settle(
    context: _LateContext, agent_result: AgentResult, worktree: Path,
) -> _LateAdjudicationRun:
    """Fold this run's usage and decline the outcomes that are not answers."""
    if _guards._paused_during_agent_run(context.gh, context.issue):
        return _late_outcome._finished(context, _LateDisposition.DEFERRED)
    context.state.set(_LAST_AGENT_ACTION_AT, _usage._now_iso())
    if not agent_result.interrupted:
        _usage._accumulate_issue_usage(context.state, agent_result.usage)
    declined = _declined_run(context, agent_result, worktree)
    if declined is not None:
        return _guarded(context, declined)
    _late_session._record_late_session(context.state, agent_result)
    return _guarded(
        context, _late_outcome._decide(context, agent_result.last_message),
    )


def _declined_run(
    context: _LateContext, agent_result: AgentResult, worktree: Path,
) -> Optional[_LateAdjudicationRun]:
    """The refusals a finished run earns before its reply is read at all.

    The mutation check sits ahead of the interruption refusal for the reason
    the initial decomposer's dirty check does: a run the shutdown sweep killed
    can have written before it died, and a contaminated candidate is a thing
    an operator has to be told about whether or not the run that caused it
    counted.
    """
    if agent_result.timed_out:
        return _late_outcome._parked_run(
            context,
            agent_result,
            _TIMEOUT_PARK.format(seconds=config.AGENT_TIMEOUT),
            reason=_late_outcome.PARK_TIMEOUT,
        )
    mutated = _candidate_mutation(context.generation, worktree)
    if mutated is not None:
        log.error(
            "issue=#%d the late decomposer left the candidate worktree "
            "changed; refusing its verdict",
            context.issue.number,
        )
        return _late_outcome._parked_run(
            context, agent_result, mutated,
            reason=_late_outcome.PARK_WORKTREE_MUTATED,
        )
    if _guards._ignore_if_interrupted(context.issue, agent_result):
        return _late_outcome._finished(context, _LateDisposition.DEFERRED)
    return None


def _candidate_mutation(
    generation: LateGeneration, worktree: Path,
) -> Optional[str]:
    """The park a worktree the read-only agent changed earns, or None.

    Both halves are proved rather than assumed. HEAD has to still BE the
    frozen commit -- not merely to contain it -- because a commit made on top
    of the candidate is what a later publication would push, and an unreadable
    HEAD proves nothing and reads the same way. The tree is asked through the
    status form for the same reason: a caller whose next step ends in a push
    has to prove the tree is clean, and a read that established nothing is not
    that proof.
    """
    head = _verification_probes._head_sha(worktree)
    if head != generation.candidate_sha:
        return _MOVED_HEAD_PARK.format(frozen=generation.candidate_sha)
    tree = _verification_probes._worktree_status(worktree)
    if not tree.readable or tree.paths:
        return _DIRTY_TREE_PARK.format(frozen=generation.candidate_sha)
    return None


def _accounting(state: PinnedState) -> dict:
    """The per-issue retry accounting as it stands, for a write to leave out."""
    return {name: state.get(name) for name in _ACCOUNTING_FIELDS}


def _apply_accounting(state: PinnedState, accounting: dict) -> None:
    """Put the retry accounting back to exactly the values captured.

    A field that was absent is dropped rather than written as null, so a
    round trip through here leaves the pinned comment as it found it.
    """
    for name, counted in accounting.items():
        if counted is None:
            state.data.pop(name, None)
        else:
            state.set(name, counted)
