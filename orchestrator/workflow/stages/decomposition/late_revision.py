# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The developer run a human's guidance earns, and the candidate it re-freezes.

Guidance about an oversized candidate is not a decomposition question, so it
does not go to the late decomposer. The work itself has to change, and the
session that wrote it is the one that knows what it wrote -- so the ORIGINAL
developer session is resumed, with the human's comments quoted, in the
worktree the candidate already lives in. It runs under `agent_role=developer`
and `stage=decomposing`, because that is what it is and where it happened: the
issue never leaves `workflow:decomposing`, and an analytics row that claimed
otherwise would put a developer run in a stage the issue was not in.

The budgets are the ones that already exist. The resume budget and the
session rotation behind it belong to the shared developer resume, which this
goes through rather than around; the per-issue daily retry cap counts fresh
spawns, and a resume driven by a human's reply is an unblock signal rather
than a retry, exactly as it is in every other stage that resumes on one.

What comes back is not trusted, it is proved. The tree has to be clean before
anything is read off it -- a candidate measured beside uncommitted changes is
not the candidate a publication would push -- and the commit the checkout ends
on is frozen and measured again from scratch, under the ceiling as it stands
now. What is NOT allowed is skipping the measurement, which is why the
generation counter advances on every reconciliation that lands -- the recorded
verdict is keyed on cycle, generation, and commit, so a candidate adjudicated
before the requirements moved has to be adjudicated again rather than answered
from the record taken before they did.

The resulting SHA is allowed to be the one that went in, but only when the
developer SAID so. "The committed work already covers this" is a real answer
and it is written down: the prompt asks for the same `ACK:` marker every other
drift resume asks for, and a marker is what an unchanged commit needs before it
is re-measured. Without one, an unchanged commit is not an acknowledgment --
it is a run that said nothing, asked a question, or timed out before it could
do either, and all three look identical from the checkout. Reading any of them
as "the work already covers it" would advance a generation and adjudicate a
candidate nobody vouched for, so they park instead, quoting whatever the
developer did say so a question reaches the human it was meant for.

A reconciliation that could not be completed parks and keeps the generation
exactly as it was. Neither of those parks is superseded by another attempt --
a dirty checkout and a measurement nothing could take are both waiting on a
human to touch the worktree -- so a bare continue re-runs this reconciliation
alone, without paying for a second developer run that already finished.

Nothing before that reconciliation is durable. The guidance is consumed, the
park is cleared, and the session is recorded in memory; the write that keeps
any of it is the one the reconciliation itself makes. A mid-run pause and a
shutdown sweep therefore leave the issue exactly as the prior tick did, with
the human's guidance still unread -- which costs one repeated developer run
and never a dropped instruction.

Every reconciliation of a developer run ends where a finished adjudication
does: the owner is read again. The developer has just run for as long as it
ran, and a human who closed the issue in between has ended the whole cycle
rather than this tick -- which is as true of a reconciliation that parked as
of one that landed, so both are read past. The read sits after everything the
reconciliation made durable and before anything it would SAY: the re-measured
candidate and the park are written whatever the answer turns out to be, so a
comment GitHub refuses costs neither, and the notice each of them owes is
posted only once the issue is known to still be there.

The obligation to take that read is part of what is written, not something
the step after it adds. A re-measured candidate is the one result that can
route a later tick past the read entirely -- under the ceiling it is not
adjudicable, and over it the advanced generation has no recorded answer to
short-circuit the spawn -- so the two go down together and a tick that dies
between them leaves an issue that still owes the read.

A reconciliation that PARKED writes on the same terms and for a sharper
reason. The developer finished, and the guidance that bought it was consumed
in memory on the way in -- so a park left staged until the guard would, on a
tick that died before it, take the consumed reply down with it: the next tick
would find the same comment unread and resume the developer a second time,
against a checkout nobody has cleaned. The park, the consumption, and the
owed read are therefore one write, made here, before the read.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator.git.measurement import additions as _measurement
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import paths as _worktree_paths
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.engine import messages as _messages
from orchestrator.workflow.engine import prompts as _prompts
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.late_split import events as _events
from orchestrator.workflow.late_split import telemetry as _telemetry
from orchestrator.workflow.late_split.models import LateFailure, LatePhase
from orchestrator.workflow.stages.decomposition import (
    late_content as _late_content,
)
from orchestrator.workflow.stages.decomposition import (
    late_outcome as _late_outcome,
)
from orchestrator.workflow.stages.decomposition import late_owner as _late_owner
from orchestrator.workflow.stages.decomposition.late_models import (
    _LateContentSettlement,
    _LateContentSignal,
    _LateContext,
    _LateDisposition,
    _OwnerState,
)
from orchestrator.workflow.stages.implementing import resume as _dev_resume

log = logging.getLogger("orchestrator.workflow")

_DECOMPOSING_STAGE = "decomposing"

_LAST_AGENT_ACTION_AT = "last_agent_action_at"

_REVISING_NOTICE = (
    ":pencil2: resuming the developer against your guidance; the committed "
    "candidate is re-measured from whatever it ends on."
)

_REMEASURED_NOTICE = (
    ":triangular_ruler: the revised candidate `{revised}` adds {additions} "
    "lines over `{base}` (ceiling {threshold}); adjudicating it as "
    "generation {generation}."
)

_DIRTY_PARK = (
    "the developer was resumed against your guidance but left changes in the "
    "worktree (or the tree could not be read), so nothing was re-measured "
    "and the recorded candidate is unchanged. Commit or clear what is there, "
    "then reply `/orchestrator continue` -- that re-reads the checkout "
    "without paying for another developer run."
)

_UNREADABLE_HEAD_PARK = (
    "the developer was resumed against your guidance, but the commit its "
    "worktree ends on could not be read, so there is nothing to re-measure. "
    "Restore the checkout on this host, then reply `/orchestrator continue` "
    "to re-read it."
)

_STRANDED_PARK = (
    "this issue's committed candidate cannot be revised: the adjudication "
    "before this one already created {children} from it, and a new candidate "
    "would be split into a manifest that has nothing to do with them. The "
    "children, the recorded verdict, and your comment all stand. Decide what "
    "the existing children should be first -- close them and clear this "
    "issue's `late_split_children`, or let them run -- and the next tick "
    "continues from there."
)

_UNMEASURED_PARK = (
    "the revised candidate `{revised}` could not be measured ({failure}), so "
    "it is not being adjudicated and the recorded generation is unchanged. A "
    "candidate whose size is unknown is not a small one. Settle what the "
    "reading stopped on, then reply `/orchestrator continue` to re-measure "
    "the same commit."
)

_REVISION_PROMPT = (
    "The human replied about this issue while your committed work was being "
    "adjudicated for its size, and the issue ITSELF may have been edited "
    "since you last read it. What follows is the current requirements, not "
    "the ones your session started from -- re-read all of it, decide what it "
    "means for the work you already committed, and COMMIT any further "
    "changes in your current worktree. Do NOT push -- the orchestrator "
    "measures whatever commit the worktree ends on and takes it from "
    "there.\n\n"
    "Issue title: {title!r}\n\n"
    "Issue body:\n\n{body}\n\n"
    "Guidance:\n{guidance}\n\n"
    "Leave the worktree CLEAN: anything uncommitted is not part of the "
    "candidate and stops the re-measurement.\n\n"
    "If your existing commits already satisfy the guidance and no further "
    "change is needed, leave the commit exactly as it is and end your final "
    "message with EXACTLY this marker, alone on its own line:\n\n"
    "  ACK: <one-line justification>\n\n"
    "The marker is the only thing that lets an unchanged commit through: "
    "without it, a run that changed nothing is read as one that could not "
    "answer, and the orchestrator parks for a human instead of re-measuring. "
    "So use `ACK:` ONLY when you are certain the committed work covers the "
    "guidance. If you have a clarification question or are unsure, do NOT use "
    "it -- reply with the question and the orchestrator will park awaiting a "
    "human, quoting what you asked.\n\n"
    "{commit_style}\n\n"
    "{foreground}"
)

_NO_BODY = "(no body)"

_SAID_NOTHING = "(the developer returned no message)"

_UNANSWERED_PARK = (
    "the developer was resumed against your guidance and left the committed "
    "candidate exactly as it was, without vouching for it -- so there is "
    "nothing new to measure and nothing saying the existing commit already "
    "covers what you asked. The recorded generation is unchanged. Here is "
    "what it said:\n\n{reply}\n\nReply with the answer it needs, or with "
    "`/orchestrator continue` to accept the commit as it stands and measure "
    "it again."
)


def _stranded_by_children(
    context: _LateContext,
) -> Optional[_LateContentSettlement]:
    """Refuse to replace a candidate whose split has already created children.

    A revision ends in a NEW candidate under a new generation, and everything
    that generation decides is decided about work the old one has already
    handed to real GitHub issues. Those children exist, carry an ancestry
    naming the adjudication that made them, and are recorded as the consumers
    a snapshot is retained for -- so a second manifest over the top of them
    would strand every one: nothing polls a child the parent no longer
    records, and no automatic rule can say which of two manifests a human
    meant.

    So the issue is handed back instead. What the human asked for is not lost
    -- their comment stands, the children stand, and the recorded verdict
    stands -- and settling it is a decision about issues that already exist,
    which is theirs to make.
    """
    if not context.generation.split_children:
        return None
    return _parked(
        context, _STRANDED_PARK.format(
            children=", ".join(
                f"#{number}" for number in context.generation.split_children
            ),
        ),
        reason=_late_outcome.PARK_REVISION_UNANSWERED,
    )


def _revise_from_guidance(
    context: _LateContext, signal: _LateContentSignal,
) -> _LateContentSettlement:
    """Resume the locked developer session with this guidance, then remeasure.

    The guidance is consumed in memory before the run, so the comments quoted
    into the prompt and the ones the watermark covers are the same set, and it
    becomes durable only on a path that reconciles what the run left. That is
    the same order every stage that resumes on a human reply keeps: a mid-run
    pause and a shutdown sweep both mean this tick did not happen, and a
    consumption made durable by one of them would drop a human's instruction
    on the floor with nothing left on the issue pointing at it. The cost is
    the one every declined run has -- the next tick resumes the developer
    again on the same reply, and it sees its own prior commit.

    The park this answers goes the same way. Clearing it is staged here so a
    run that then fails re-parks with the reason it actually failed for rather
    than leaving the issue claiming it is still waiting to be told what the
    edit meant.
    """
    stranded = _stranded_by_children(context)
    if stranded is not None:
        return stranded
    _comments._post_issue_comment(
        context.gh, context.issue, context.state, _REVISING_NOTICE,
    )
    _consume(context, signal)
    _late_outcome._answer_park(context)
    worktree, agent_result, paused = _dev_resume._resume_dev_with_text(
        context.gh,
        context.spec,
        context.issue,
        context.state,
        _revision_prompt(context.issue, signal.guidance),
        stage=_DECOMPOSING_STAGE,
        pause_guard=True,
    )
    if paused or _guards._ignore_if_interrupted(context.issue, agent_result):
        return _LateContentSettlement(
            disposition=_LateDisposition.DEFERRED,
        )
    context.state.set(_LAST_AGENT_ACTION_AT, _usage._now_iso())
    if agent_result.timed_out:
        log.warning(
            "issue=#%d the developer revision timed out after %ds; reading "
            "the worktree it left anyway",
            context.issue.number, config.AGENT_TIMEOUT,
        )
    return _reconcile_revised_candidate(context, worktree, agent_result)


def _retry_revision(
    context: _LateContext, signal: _LateContentSignal,
) -> _LateContentSettlement:
    """What a human's reply to a stalled revision earns.

    Guidance means the work still has to change and buys another developer
    run. A bare continue does not: the developer already finished, and what
    failed was the reading of what it left -- so the checkout is re-read, the
    commit re-frozen, and the size measured again, with no agent spawned at
    all.
    """
    if signal.guidance:
        return _revise_from_guidance(context, signal)
    if not signal.bare_continue:
        return _LateContentSettlement(disposition=_LateDisposition.PARKED)
    stranded = _stranded_by_children(context)
    if stranded is not None:
        return stranded
    _consume(context, signal)
    return _reconcile_revised_candidate(
        context,
        _worktree_paths._worktree_path(context.spec, context.issue.number),
    )


def _reconcile_revised_candidate(
    context: _LateContext, worktree: Path, agent_result=None,
) -> _LateContentSettlement:
    """Prove the tree clean, freeze what it ends on, and measure it again.

    Clean first, because everything after it is a claim about one commit. A
    tree carrying uncommitted work would have that work in the checkout a
    publication pushes from and out of the diff a verdict was taken on, and a
    status read that established nothing is not proof of anything either.

    An unchanged commit is the one reading the checkout cannot settle on its
    own: a developer that vouched for its existing work and one that said
    nothing, asked a question, or timed out all leave HEAD exactly where it
    was. So it is settled by what the run SAID, and a commit nobody vouched
    for parks with the reply quoted rather than being re-measured as an
    answer.

    `agent_result` is absent when no developer ran this tick -- the retry a
    human's own `/orchestrator continue` drives. That command is the
    acknowledgment in that case: the human has read the park and accepted the
    commit as it stands, which is exactly what the marker says on the path
    where an agent is the one speaking.
    """
    tree = _verification_probes._worktree_status(worktree)
    if not tree.readable or tree.paths:
        return _parked(
            context, _DIRTY_PARK,
            reason=_late_outcome.PARK_REVISION_DIRTY,
        )
    revised = _verification_probes._head_sha(worktree)
    if not revised:
        return _parked(
            context, _UNREADABLE_HEAD_PARK,
            reason=_late_outcome.PARK_REVISION_UNMEASURED,
        )
    if revised == context.generation.candidate_sha and not _vouched_for(
        agent_result,
    ):
        return _parked(
            context,
            _UNANSWERED_PARK.format(reply=_quoted_reply(agent_result)),
            reason=_late_outcome.PARK_REVISION_UNANSWERED,
        )
    return _remeasured(context, worktree, revised)


def _vouched_for(agent_result) -> bool:
    """Whether an unchanged commit is a real answer rather than a non-answer.

    No run at all is one, because the only way to reach this without one is a
    human's own `/orchestrator continue` on a park that quoted the commit to
    them. A run that made one carries the same `ACK:` marker every other drift
    resume is answered by, and nothing else counts: prose that merely sounds
    like agreement is exactly what the marker exists to keep out.
    """
    if agent_result is None:
        return True
    return _messages._drift_ack_reason(
        agent_result.last_message or "",
    ) is not None


def _quoted_reply(agent_result) -> str:
    """The developer's own words, for the human the park hands the issue to.

    Quoted rather than summarized because the commonest reason to be here is a
    question, and a question paraphrased is one the human answers wrong.
    """
    said = (agent_result.last_message or "").strip() if agent_result else ""
    return _messages._as_blockquote(said or _SAID_NOTHING)


def _remeasured(
    context: _LateContext, worktree: Path, revised: str,
) -> _LateContentSettlement:
    """Re-freeze this candidate under the ceiling as it stands now.

    The generation counter advances even when the commit did not. A recorded
    verdict answers a cycle, a generation, AND a commit, so an acknowledged
    candidate whose SHA is unchanged would otherwise read back as already
    decided -- decided against the requirements that have since moved, which
    is the one answer this whole path exists to refuse.
    """
    measured = _measurement._measure_candidate(
        context.spec, worktree, revised,
    )
    if not measured.is_measured:
        _late_outcome._emit_failure(context, LateFailure.MEASUREMENT_FAILED)
        return _parked(
            context,
            _UNMEASURED_PARK.format(
                revised=revised, failure=measured.failure,
            ),
            reason=_late_outcome.PARK_REVISION_UNMEASURED,
        )
    context.generation = replace(
        context.generation,
        generation=context.generation.generation + 1,
        candidate_sha=measured.candidate_sha,
        base_sha=measured.base_sha,
        threshold=config.MAX_ADDED_LINES,
        additions=measured.additions,
        phase=LatePhase.MEASURING,
        # The split transaction's own receipts belong to the generation that
        # wrote them and go with it. They are positional and one-shot: an
        # ordered child register carried forward would have a new manifest
        # adopt the old one's children by index, and a link receipt carried
        # forward would suppress the very announcement the new split owes.
        # What does NOT go with them is either external ledger -- a ref the
        # remote holds is owed whatever this generation decides next.
        split_children=(),
        links_announced=False,
        # The owner read this run still owes goes down WITH the result, in the
        # one write. Claimed a step later by the guard, a tick that died in
        # between would leave a re-measured candidate nothing brings a later
        # tick back to: under the ceiling it is not adjudicable, and over it
        # the advanced generation has no recorded answer, so the next tick
        # pays for an agent before finding out whether anybody still wants
        # the issue.
        owner_check_pending=True,
    )
    _late_outcome._answer_park(context)
    _late_outcome._persist(context)
    _telemetry.emit_late_event(
        context.gh,
        _events.LateEvent(family=_events.LateEventFamily.MEASUREMENT),
        context.generation,
        stage=_DECOMPOSING_STAGE,
    )
    return _guarded_revision(
        context,
        _LateDisposition.REVISED,
        announce=_REMEASURED_NOTICE.format(
            revised=measured.candidate_sha,
            additions=measured.additions,
            base=measured.base_sha,
            threshold=config.MAX_ADDED_LINES,
            generation=context.generation.generation,
        ),
    )


def _guarded_revision(
    context: _LateContext,
    settled: _LateDisposition,
    *,
    announce: str = "",
) -> _LateContentSettlement:
    """Read the owner again now the developer has finished, and report it.

    The same guard a finished adjudication passes, for the same reason and at
    the same point: the developer ran for as long as it ran, and the next
    thing this candidate is worth is an adjudication that ends in a
    publication or a split. A human who closed the issue while it ran has
    said the whole cycle is over, and the mark this leaves is what the
    cleanup path settles it from.

    Asked of a reconciliation that PARKED as well as one that landed, because
    the run was paid for either way and a closure during it strands the same
    generation and the same plan-PR hold. What the answers change differs:
    a park keeps its own reason and records the read as still owed, since
    replacing what a human is being asked about with a read failure they
    cannot answer would cost them the question.

    Taken AFTER whatever this reconciliation made durable and BEFORE anything
    it would say. An owner that turns out to be gone therefore costs no
    developer run -- the candidate the run produced is frozen and recorded
    whatever the read says, and a reopened issue starts from a fresh cycle
    rather than from work nobody kept -- and it costs no comment either, since
    `announce` is what the issue is told only once the read comes back open.
    """
    owner = _late_owner._guarded_owner(context)
    if owner == _OwnerState.CLOSED:
        return _LateContentSettlement(
            disposition=_LateDisposition.CANCELLED, persisted=True,
        )
    if owner == _OwnerState.UNREADABLE:
        return _LateContentSettlement(
            disposition=_LateDisposition.PARKED, persisted=True,
        )
    if announce:
        _comments._post_issue_comment(
            context.gh, context.issue, context.state, announce,
        )
        _late_outcome._persist(context)
    return _LateContentSettlement(disposition=settled, persisted=True)


def _consume(context: _LateContext, signal: _LateContentSignal) -> None:
    """Record the conversation this tick is acting on as read.

    Two watermarks, because two different consumers read the same thread. The
    generation's own covers the late fingerprints, so the same comments do not
    come back as fresh guidance. The issue-wide `last_action_comment_id` is
    ratcheted for the reason every other developer resume ratchets it: the dev
    has seen these comments, and the later validating -> in_review handoff
    would otherwise replay them as fresh PR feedback and resume it a second
    time on input it already handled.

    Both cover the whole trusted run this reading folded in rather than the
    guidance alone. A bare continue that re-read the checkout was acted on
    just as a quoted comment was, and leaving it behind the shared watermark
    would hand it to that same handoff as feedback nobody had answered.
    """
    context.generation = _late_content._rebaselined(
        context.generation, signal.fingerprint,
    )
    _late_outcome._mark_replies_read(
        context, signal.fingerprint.comment_watermark_id,
    )


def _revision_prompt(issue: Issue, guidance: tuple) -> str:
    """The followup one developer revision is resumed with.

    The title and body are quoted beside the guidance because a resume is
    exactly the case that cannot see them: the session's replayed transcript
    holds the issue as it read when the work started, and the commonest reason
    to be here is that a human edited it since. A developer left to act on the
    text it remembers would revise against requirements nobody is asking for.
    """
    quoted = "\n\n".join(
        _comments._quote_comment_line(issue_comment)
        for issue_comment in guidance
    )
    return _REVISION_PROMPT.format(
        title=(issue.title or "").strip() or f"#{issue.number}",
        body=_messages._as_blockquote(
            (issue.body or "").strip() or _NO_BODY,
        ),
        guidance=quoted or f"(see issue #{issue.number})",
        commit_style=_prompts._COMMIT_STYLE_NOTE,
        foreground=_prompts._FOREGROUND_ONLY_NOTE,
    )


def _parked(
    context: _LateContext, message: str, *, reason: str,
) -> _LateContentSettlement:
    """Hand the issue back with the generation exactly as it arrived.

    The owner is read past the park for the same reason it is read past a
    landed revision: the developer run this is reconciling has finished, and
    a human who closed the issue while it ran has ended the cycle rather than
    handed it back.

    The park is STAGED rather than said, so the write below is what makes it
    durable and the notice waits for a read that proves the issue is still
    there. A comment refused halfway would otherwise take the whole
    reconciliation down with it -- the consumed guidance included -- and buy a
    second developer run of one that had already finished.

    That write is this step's own, and it goes down BEFORE the guard for the
    same reason the re-measurement's does: a reconciliation that failed is a
    developer run that finished. The guidance is consumed, the checkout has
    been read, and what came of it is a park -- so a tick that died on the way
    to the guard would leave a generation still reading as adjudicating, with
    the human's instruction spent and nothing on the issue saying why, and the
    next tick would resume the developer on a reply it had already acted on.
    """
    log.error(
        "issue=#%d the revised candidate could not be reconciled (%s)",
        context.issue.number, reason,
    )
    _late_outcome._stage_park(context, message, reason=reason)
    _late_outcome._completed(context)
    return _guarded_revision(context, _LateDisposition.PARKED)
