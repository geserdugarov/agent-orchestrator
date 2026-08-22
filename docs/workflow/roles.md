# Agent roles and sessions

The workflow has three agent roles, each spawned by a different set of stage handlers. Roles are independent: each can
use `codex` or `claude` and each carries its own optional CLI args, parsed from its own env var by the grammar in
[`command-specs.md`](command-specs.md).

Stage and label names are spelled apart here as they are in
[`../state-machine/labels-and-state.md#workflow-labels`][workflow-labels]: a bare tag names the **stage** — the
handler, the subpackage under `orchestrator/workflow/stages/` holding it, and the identifier a session's analytics row
is attributed to — while `workflow:<tag>` is the **wire label** the GitHub issue carries. `in_review`, `question`,
`discussion`, and the `done` / `rejected` terminals were never namespaced, so those read the same on both sides.

## The three roles

- **Decomposer** (`DECOMPOSE_AGENT`, default `claude`) — spawned by `_handle_decomposing` (and its `awaiting_human`
  resume); `_handle_question` (and its `awaiting_human` resume), `_handle_discussion`, and the late adjudication an
  oversized committed candidate earns all reuse the same backend. Session: locked per issue after first spawn
  (decomposing → `decomposer_agent`; question → `question_agent`; discussion → `discussion_agent`; late adjudication
  → `late_agent`, each a separate pin).
- **Implementer / dev** (`DEV_AGENT`, default `claude`) — spawned by `_handle_implementing`, `_handle_documenting`,
  `_handle_validating` (awaiting-human resume; the `CHANGES_REQUESTED` dev fix is dispatched here but relabels to
  `workflow:fixing` BEFORE the spawn and records `stage="fixing"` analytics, so the dev-fix subphase reads as fixing
  rather than validating on both the wire label and the analytics row), `_handle_fixing` (in_review-route PR-feedback
  resume + validating-route awaiting-human rescan), `_handle_resolving_conflict` (conflict resume + awaiting-human
  resume). Session: locked per issue after first spawn.
- **Reviewer** (`REVIEW_AGENT`, default `codex`) — spawned by `_handle_validating` (fresh every round). Session: fresh
  per round; current config always wins.

The defaults (`claude` decomposes, `claude` implements, `codex` reviews) use both backends; both CLIs need to be
authenticated on the host before the orchestrator starts.

## Where a role is spawned from

Every stage handler lives on responsibility-named owners under `orchestrator/workflow/stages/`, one subpackage per
stage — `decomposition` (the `decomposing` / `ready` / `blocked` / `umbrella` handlers), `implementing`, `documenting`,
`validating`, `in_review`, `fixing`, `conflicts`, `question`, and `discussion` — which own entry checks, session
execution, drift handling, persistence, and terminal routing. Nothing answers for a stage beside those owners, so each
handler is reached on the one module that holds it, the dispatcher and the same-tick pickup start that module
directly, and a patch meant to intercept a handler has to land on it. `orchestrator.workflow` publishes six names and
nothing else — `WorkflowLabel`, `ControlLabel`, `guard_transition`, `is_allowed_transition`, `IllegalTransition`, and
`tick`.

Everything a stage borrows is named the same way. A cross-stage call names the owner it borrows from rather than a
facade; the worktree, HEAD, fetch, push, and PR-title helpers live on owners under `orchestrator/git/`; and the
tracked spawn every role goes through dispatches on `orchestrator/agents/runner.py`. Which owner defines which helper
and which stage borrows it is the module map in
[`../architecture/platform-modules.md`](../architecture/platform-modules.md) for the git and agent owners and
[`../architecture/workflow-modules.md`](../architecture/workflow-modules.md) for the stage tree; the
per-stage behavior is in
[`../state-machine.md#stage-handlers`](../state-machine.md#stage-handlers). What follows is the role-specific glue.

## Session lifecycles

- **Dev session reuse.** The implementer session is spawned once in `_handle_implementing` and then resumed by
  `_handle_documenting`, `_handle_validating`, `_handle_fixing`, and `_handle_resolving_conflict` whenever they need
  the dev to make a change. The locked `(backend, args)` spec is re-parsed on every resume from pinned `dev_agent` so
  a config flip mid-flight cannot retarget the session. `_resume_dev_with_text` on
  `workflow/stages/implementing/resume.py` is the one module every one of those resumes goes through; it declares the
  call signature its callers were written against, then binds those arguments into a typed request/context before
  executing the resume.
- **Reviewer freshness.** `_handle_validating` spawns a fresh reviewer subprocess every round with no resume, so
  `REVIEW_AGENT` changes take effect on the next validating tick. The current value is recorded in `review_agent` for
  traceability only.
- **Decomposer reuse.** `_handle_decomposing` spawns the decomposer once and resumes it on every awaiting-human reply.
  The `question` stage reads `DECOMPOSE_AGENT` only as the *fallback* on the first-ever question spawn, then pins what
  it ran under to `question_agent` (a separate key) so a multi-turn Q&A keeps its own lock independent of any
  decomposing session on the same issue. The `discussion` stage borrows the same role on the same terms and pins to
  `discussion_agent` + `discussion_session_id`, a third independent pair, and resumes that session on every human
  reply — so the pin protects both which backend and args a later round runs under and the conversation it continues,
  since a session id is only meaningful to the CLI that issued it.
- **Late adjudication.** An oversized committed candidate is adjudicated by the same role under the same
  `workflow:decomposing` label, by `_adjudicate_late_generation` on
  `workflow/stages/decomposition/late_coordinator.py`. It is an additive mode rather than a stage: an analytics row
  reads `agent_role=decomposer` and `stage=decomposing` exactly as the initial decomposer's does, the spawn spends
  the same per-issue retry budget and folds its usage into the same counters, and the initial decomposer's prompt,
  fence, and missing-manifest handling are untouched. What it does not share is the pin or the conversation:
  `late_agent` + `late_session_id` are a fourth independent pair, seeded from `DECOMPOSE_AGENT` on an issue's first
  late run and locked from then on. One late run in three resumes it: a human answering the categorized question the
  adjudicator asked is answering the agent that ASKED it, so that run continues the pinned session rather than opening
  a conversation which would have to be told the question before it could be told the answer. Every other late run is
  fresh — a first adjudication has none to continue, and a candidate the developer revised is a different question, so
  a session opened against the commit it replaced would hand the agent a transcript about work nobody is adjudicating.
  Both halves are proved rather than assumed: the caller says it is carrying an answer, and the record says its
  session really ran against this cycle, generation, and commit. The id is pinned at the two exits that persist, a
  timeout and a completed reply. The run happens in the issue's OWN worktree rather than a scratch checkout of the
  base branch, because the diff
  it is asked about is between two commits nothing has pushed. The coordinator is callable and complete, and **almost
  nothing calls it**: `_adjudicate_late_generation` has no caller in the tree, so no live issue reaches it. What IS
  wired is the pair of refusals below, both of which keep something else from deciding a live generation: one stops
  `DECOMPOSE=off` from routing an unadjudicated candidate to implementation, and one stops the dispatcher handing a
  hand-relabelled issue to whichever stage the new label named. Wiring the adjudication itself into the
  clean-committed pre-publication seam — the
  point at which a candidate is measured and found oversized — is a separate change.
- **Late developer revision.** Guidance a human writes about an oversized candidate is not a decomposition question,
  so it does not go to the late adjudicator: the work itself has to change, and the session that wrote it is resumed
  against the guidance in the worktree the candidate already lives in
  (`workflow/stages/decomposition/late_revision.py`). It runs under `agent_role=developer` and `stage=decomposing`,
  because that is what it is and where it happened — the issue never leaves `workflow:decomposing`. The prompt quotes
  the issue's CURRENT title and body beside the guidance, because a resume is exactly the case that cannot see them:
  the replayed transcript holds the issue as it read when the work started, and the commonest reason to be here is
  that a human edited it since. The budgets are
  the ones that already exist: the resume budget and the session rotation behind it belong to the shared developer
  resume this goes through rather than around, and the per-issue daily retry cap counts fresh spawns, so a resume
  driven by a human's reply is an unblock signal rather than a retry exactly as it is in every other stage.

What a resume re-parses, and why the pin is the full spec rather than the backend alone, is in
[`command-specs.md#in-flight-session-lock`](command-specs.md#in-flight-session-lock). The two conversation stages'
prompts and round contracts are in [`conversations.md`](conversations.md).

## What a late adjudication is asked, and what it may answer

The late prompt (`late_prompt.py`) carries the original issue and its trust-filtered thread, the declared scope this
generation owns, the two frozen commits with the `git diff <base>...<candidate>` that reads them, the measured additions
against the configured ceiling, the lineage, and the fact that committed work already exists and is not to be rewritten.
Three dots, not two: that is the prospective pull-request range the measurement was taken over
(`git/measurement/additions.py`), and on a diverged history the two-dot range would put the agent on changes nobody
measured — deciding a split over work this candidate does not add. The child cap, the lineage bound, and the category
vocabulary are read back off the owners that enforce them, so a bound the agent is told cannot drift from the bound it
is judged against.

The reply ends in exactly one fenced `orchestrator-late-manifest` block — a different fence from the initial
`orchestrator-manifest`, read by `late_reply.py` — declaring one of three outcomes:

- `single` — the committed work is one coherent change despite its size. A diff dominated by legitimate generated or
  data artifacts is the named false positive and gets this verdict with `"category": "generated_artifacts"`.
- `split` — a child manifest that partitions the declared scope completely, held to the same rules the initial mode
  uses: the child cap, each child's shape, and the acyclicity of the graph they declare.
- `question` — a categorized question for a human, which is also where artifacts that look like they should NOT have
  been committed go. The category is mapped onto the closed vocabulary, so an agent's own spelling records as
  `unknown` rather than widening the field.

Unlike the initial mode, prose alone is not an outcome: a reply with no block, or with more than one, parks for a
human rather than being read as the agent asking a question, because a late question has its own structured decision
to travel in. Automatic splitting stops at `MAX_LINEAGE_DEPTH` (3), and a split proposed at the bound is recorded as
the categorized question the workflow actually owes a human (`lineage_bound`) rather than acted on — so the next tick
asks the human instead of paying for the same forbidden split again.

A completed run is recorded whole so a crashed tick does not pay for a second one — a second run is not free, and it
is free to decide differently. What "whole" means is per verdict: a `single` needs nothing beside itself, a `question`
carries its category and the sentence it asked, and a `split` carries the ordered child manifest that *is* its
decision. Whether it fits is measured on the whole comment the write would produce — the preserved plan-PR body and
every other stage's keys included — because a result small on its own can still be the one that pushes the comment
past what GitHub accepts, and learning that from the failed write means the agent has already been paid for. An
outcome past that budget is refused entire rather than shortened, and the issue parks for a human; an incomplete one
read back later is not an answer at all and the adjudicator runs again. The record goes
out before the question it announces does, so the narrow crash window between them costs a repeated comment rather
than a repeated agent run, and the next tick posts the recorded question instead of earning it again. Which keys carry
that, why a recorded answer names its cycle as well as its generation and commit, and why the pre-spawn write leaves
the retry counter alone are in [`../state-machine/labels-and-state.md#the-late-run`][late-run].

The read-only promise the prompt makes is proved rather than trusted. The late adjudicator runs in the developer's own
worktree — the frozen candidate is not on any remote yet — and the CLI it runs under can write there whatever the
prompt says, so before the reply is read at all the candidate is proved unmoved (HEAD still IS the frozen commit, not
merely contains it) and the tree proved clean. An agent that committed over the evidence or left changes beside it has
contaminated the one artifact every later step acts on: the issue parks for a human and the verdict is not used. That
check sits ahead of the interruption refusal for the same reason the initial decomposer's dirty check does — a run the
shutdown sweep killed can have written before it died.

None of it starts on a generation that cannot be acted on. The prompt names both frozen commits and tells the agent to
diff between them, the hold marks a pull request in the generation's name, and the verdict is reported under its
identities — so the identities and both SHAs are proved before the plan PR is touched or an agent is started. That
includes the generation naming THIS issue, which a positive `late_current_issue` on its own does not say. A candidate
whose base was never recorded would otherwise produce a `git diff` against nothing and a record two sinks refuse
afterwards, with the run already paid for; one carrying somebody else's number would show the agent a prompt naming two
issues and file the verdict against the one it names. Either parks instead, saying which field is wrong.

Before any of that runs, a reusable open plan PR is put under a cycle-marked hold (`late_hold.py`). *Plan* is
checked, not assumed: `pr_number` names whichever pull request the issue currently records, and that is an
implementation as often as a plan, so the hold reads the discussion provenance through the implementing stage's own
`_recorded_pr_is_the_plan` — about the one snapshot it read, since past the handoff a plan is told from an
implementation by the commit its head is on and two reads would leave a window for a human push. That snapshot is read
whole where the fetch is guarded, because a PyGithub pull request is lazy and the request that can fail is the first
attribute access rather than the fetch itself; anything unreadable parks rather than escaping. An implementation PR is
left alone — rewriting an implementation PR's description would replace a human's account of a change under review with
a notice about a different one. A provenance that could not be established is not the same answer and fails closed. Past
that gate, the original body is written to pinned state BEFORE the pull request is edited, so a crash can lose the edit
— which the next tick re-applies, since every branch is idempotent — but never the only copy of the description.
What the retry re-applies over is decided by the WHOLE body, not by the hidden marker inside it: exactly two bodies
are this issue's to replace — the hold it wrote, **verbatim**, and the description recorded beside the identity, which
is what a crash between the persist and the edit leaves and what the first application starts from. Anything else is a
human writing over the notice, the marker they happened to leave in place included: a sentence changed inside the hold
is their edit as surely as a wholesale rewrite is, and calling that held would have the release put the preserved copy
back over their words a step later. That comparison is only affordable because the hold body is exactly
reconstructible, which is why the marker is scoped to the **cycle** and the notice quotes nothing that moves inside
one — the generation counter advances on every reconciliation that lands, so a body keyed to it would leave every
re-measured candidate wearing a notice its own record no longer recognized, and the measurement belongs on the issue
thread, where each reading is announced anyway. Being reconstructible is a property of a *spelling*, and a hold
outlives the binary that wrote it, so the spelling before this one — marked by generation as well as cycle, and
quoting the measurement — is kept as something to **recognize** and never to write: a body found in it is ours, and
the same edit that would have applied a fresh hold rewrites it in the current spelling, so every later comparison has
one answer to make. A hold a binary cannot reconstruct is a "do not merge" notice nothing can ever take back off, on
a pull request nothing can start an agent under. Leaving it alone is
not the same as being *held*, though, and the answer says so: an open plan PR whose notice a human removed is a change
they can merge with nothing on it saying an adjudication is running, so the reconciliation reports it **displaced** and
no late decomposer is started under it. An answer already recorded may still be settled — settling releases a hold
that is already gone, and only a NEW run would leave a human free to merge under one. A write
that *refuses* is the same rule read the other way: with no preserved copy there is no hold to take, so nothing is
edited and the issue parks. How long a description may be preserved is decided the same way, before it is replaced: the
whole prospective comment is rendered with the run's record already in it — the spec this issue is locked to, an
operator's command line bounded by nothing here, included — because the write that starts the run has no safe failure of
its own. A body too long to hold beside that is refused while nothing has been touched. A hold that cannot be reconciled
parks WITHOUT spawning — once, since the retry that changes nothing repeats no notice — and the park it leaves is
retired the moment a later attempt reconciles it — a stale `awaiting_human` would otherwise silence a question, whether
the retry recorded it or a crashed run had already recorded one whose announcement never landed. A pull request a human
merged or closed meanwhile is simply not held, and nothing re-anchors the frozen candidate SHA off it: the recorded
commit is the evidence every later step acts on, never the pull request's current head. What the run itself records —
role, locked spec, session, cycle, source commit, generation, and the whole of what the verdict decided — is in
[`../state-machine/labels-and-state.md#the-late-run`][late-run].

## The owner read a finished run has to pass

Everything above is about the candidate. This is about the issue the candidate belongs to, and it exists because of
how long a late run takes: the issue was fetched when the tick began, an agent then ran for minutes to hours, and
everything a completed run leads to — publishing an accepted candidate, taking a snapshot, superseding a plan pull
request, activating children, even announcing a question — lands on an issue somebody may have closed in between. So
after **every** completed late run, the adjudicator's and the developer revision's alike, the result is persisted and
then the owner is read again, once, before any of that happens (`late_owner.py`).

Every completion, not only the ones that decided something. A `question`, a timeout, an unusable reply, a candidate
the adjudicator moved, an outcome too large to record, and a developer reconciliation that could not be made are all
runs the issue paid for, and a closure during any of them strands the same generation and the same plan-PR hold as a
closure during a `single` would. The one exception is a
run the tick *declined* — an operator's `paused` label, a shutdown sweep — which is not a completion at all and must
leave durable state exactly as the prior tick left it.

The ordering is the same at every one of them: **persist, then read, then speak.** What the run produced — a recorded
verdict, a re-measured candidate, the park a failed reconciliation earns — is durable before the read; the read
decides whether anything is said at all; and the sentence goes out only past an open answer. So a closed or
unreadable owner receives no completion notice, and no comment failure can cost a run that already finished.

Three answers, and each is a different obligation.

- **Open** lets the tick carry on, and is also where a check owed from an earlier tick is retired.
- **Closed** ends the cycle rather than the tick. The generation is marked cancelled — irreversibly within the cycle,
  so a human who reopens the issue gets a fresh cycle rather than this one resumed — the mark is durable before the
  `late_cancellation` record of it is emitted, and nothing is reclaimed here: what the remote is owed stays on the
  generation's ledgers for the cleanup path to settle. Nothing is announced either: a question recorded on an issue
  whose owner has just closed it is not asked.
- **Unreadable** leaves the check owed on the generation itself (`late_owner_check_pending`), and fails
  closed twice over: an exception is unreadable, and so is a state that is neither of the two GitHub reports, since
  defaulting a read that established nothing to "open" would act on the strength of it.

That marker is the load-bearing half, because nothing else would bring a tick back to the read. A revision that came
back under the ceiling is not adjudicable and an issue parked for a human is not adjudicating, so a retry hanging off
either would never run — which is why reconciling a pending check is the **first** thing a tick does, ahead of the
size gate, the plan-PR hold, and any spawn. The retry costs no agent: the run has already been paid for and its
result is already recorded, so the recorded answer is what settles the candidate once the read succeeds.

It is written **before** the read rather than derived from its failure, because a read that fails is not the only one
that does not come back: a process killed mid-read would leave nothing at all behind, on exactly the two routes above
that carry the next tick past the point a retry could hang off.

And it is written by the **completion**, not by the guard — in the one write that records what the run left. That is
the same rule for a verdict, a re-measured candidate, a timeout, an unusable reply, an outcome too large to record, a
worktree the read-only agent moved, and a developer reconciliation nobody could make: each is a finished run the
issue has already paid for, and each is durable, park and claim together, before the tick does anything that might
not come back. Taking the claim on the way into the read instead would leave a tick that died in between with no
park, no claim, and a generation still reading as `adjudicating` — so the next tick would pay for another agent
against a candidate this one had answered, and a human who closed the issue in that window would never be found out
about. The guard asks whether the claim is standing and reads only past it; a caller whose own write did not carry
one gets it there rather than a read nothing would bring a tick back to. Everything a completion staged rides that
same write — which is what puts the durable half of a
park out ahead of any comment, so a comment GitHub refuses can no longer take a finished run's result with it and buy
a second run of an agent that had already answered.

The park beside the marker is the visible half, and it is taken only when the issue is not **already** stopped on
something a human has to answer. Replacing a question, a timeout, or a stalled revision with "the owner could not be
read" would swap out the thing the human is actually being asked for one they cannot answer; on those the marker
alone carries the retry.

What happens to the sentence that park staged turns on whether anything will ever say it instead. Holding one back is
a *deferral*, and only where a later attempt supersedes the park: that attempt re-takes it and announces the reason it
fails for *then*, which is the current one. A park no attempt supersedes — a stalled revision waiting to be told what
a dirty checkout now means — has no such tick coming, and its sentence is the only thing that will ever say what the
human has to do. So an unreadable owner releases that one anyway, on a thread it could not prove is open: a stray
comment costs less than an `awaiting_human` standing unexplained for as long as the read keeps failing.

A park still stuck repeats no notice; a park that clears itself posts one
follow-up saying so, at most once per episode. The follow-up goes out **before** the write that clears the park, so
the window a crash can land in loses the write and not the sentence, and "at most once" is answered from the thread
past `last_action_comment_id` rather than from a receipt — the comment and the clear cannot be made one operation.

**A park is not delivered until its comment is.** Persisting the flag ahead of the notice leaves one gap: GitHub
refuses the comment, and every later tick reads an `awaiting_human` it cannot tell from one whose comment landed, so
it takes the human as told and says nothing. For a park a fresh attempt supersedes that costs one tick; for a
question, a content-drift hold, or a stalled revision it is unbounded, since those parks *are* what the issue is
waiting on. So the sentence is written down beside the flag (`late_park_notice`) and dropped only by the post that
discharges it: a park whose sentence is still owed never counts as a repeat, is re-said at the top of the next
eligible tick — ahead of every gate a parked issue routes past — and, on the guard's own park, rides out on whichever
answer the owed read gets. A notice too long for the pinned comment is refused whole and loudly, since a record that
broke the write would take the park it explains down with it.

## What a verdict the read cleared earns

A **question** earns exactly one thing: the announcement. It is posted here rather than where the question was
recorded, and that is the whole reason it moved — the record has to go out before anything is said and the owner read
has to go out between them, so a question is not posted to a thread somebody closed while the agent was still
answering it.

A **split** is handed on rather than acted on. Creating the children, taking the snapshot they are cut from, and
superseding the plan pull request are one transaction; what the guard owes it is the guarantee it cannot check for
itself, that the outcome was re-checked against an owner read taken after the agent finished. Nothing is created here.
What that transaction then does is [the section below](#what-a-cleared-split-actually-does).

A **single** is reconciled as an EXEMPTION for the measured commit (`late_settlement.py`). The
candidate is already committed in the developer's own worktree, and the ordinary implementing publication is what
pushes it and hands it to review — so what this step
owes is a durable record that this exact commit has been adjudicated, or the gate would measure the same candidate
past the same ceiling and adjudicate it again forever. `late_exempt_sha` names the measured commit and only it, which
is the whole invalidation rule: work committed on top of an accepted candidate is work nobody adjudicated, and it is
measured as the fresh candidate it is
([`../state-machine/labels-and-state.md#late-generation-state`][late-state]).

Beside the exemption goes the **exact-commit reconciliation** of the pull request the issue records, and it is the
half the ordinary publication cannot do: that one searches for an OPEN pull request on the branch, while `pr_number`
by this point may name a plan PR a human merged, or the commit may already be sitting on a pull request a crashed
publication opened and never recorded. Neither is cosmetic. `implementing` asks its recorded pull request first, and
a **merged** one that is no longer the plan ends the issue as `done` — with the adjudicated candidate never
published; and a commit already on a pull request nobody records is published a second time, since the reuse looks
for an open one and finds none. So the commit is what the pull request is found by, in any state
(`find_pr_for_commit`): one that carries it is recorded whatever state it is in, and when nothing carries it the
recorded number is kept only while it is still open — a settled one is dropped rather than handed on. A lookup, or a
recorded pull request, that could not be read parks (`late_pr_unreconciled`) rather than publishing on an answer
nobody gave.

Two things the reconciliation deliberately does not do. It creates **no snapshot** — a snapshot exists so children
can be cut from a candidate about to be superseded, and an accepted candidate supersedes nothing, so preserving a
copy of it would record an obligation with nothing on the other end. And it does not rewrite the held plan PR: the
description this generation replaced is restored over the hold text, and what happens to that pull request afterwards
is the ordinary reconciliation's — the publication that follows reuses it and rewrites its body when the push lands
on it, and leaves it alone when it does not. Only a body that IS this cycle's hold, verbatim, in either spelling
this orchestrator can reconstruct, is restored — so a description a human rewrote, or edited a sentence of, marker
and all, while the hold stood stays theirs, and a settled plan PR still wearing an older binary's hold is still put
back. That settled case is the one release the retry above cannot have migrated first: a pull request nobody can
merge is left exactly as it is by the reconciliation, so what the release meets there is whichever spelling wrote it.

What a failed release may *stop* is narrower than what a failed hold stops, and for the reason the hold exists: the
danger is a change a human can still merge while it wears a notice saying not to, which is a property of an **open**
pull request. So only a reusable one parks the candidate (`late_plan_pr_hold_failed`), with the generation untouched,
which is what makes that retry free. One a human has already merged or closed is tidied where the edit lands and
stepped over where it does not — refusing to publish an adjudicated candidate over the description of a settled pull
request would be a permanent block bought for nothing, and the ordinary exact-commit reconciliation is what the
candidate goes on to.

The order is chosen so every window a crash can land in is one the next tick repairs. The hold is released first,
while nothing else has moved; the exemption is written next, with the generation still live behind it; only then is
`workflow:implementing` handed back; and only after that is the generation retired — behind the one comment naming the
accepted commit and the measurement it was judged on, posted immediately before the write that drops the generation,
so a crash between them costs at most a repeated comment. What that write keeps is the two external ledgers: an
obligation the remote is owed does not stop being owed because the adjudication that recorded it ended well. A
`decomposing` issue with no generation on it is one the INITIAL decomposer would pick up and re-decompose, and an
`implementing` issue with a live generation is one the relabel guard puts back and the next tick re-settles — so the
ordering is what keeps the first of those from ever existing.

## What a cleared split actually does

`late_transaction.py` is the order, and the order is the contract: every step is preceded by the durable fact that
lets the next tick tell "already done" from "never started", and every step is idempotent where that fact turns out
to be ambiguous. Three refusals come first, because no step below could repair any of them. A lineage already at
`MAX_LINEAGE_DEPTH` creates nothing (the bound is enforced where the children would be born as well as where the
reply was parsed). A recorded **ancestry that disagrees** with the generation's lineage creates nothing either, and
that is the same bound read from the other side: a child of an earlier split carries the lineage it was created
under, its own generation is minted from that record, and a generation naming a shallower depth or a different root
is one minted without it — which is exactly how a lineage would buy itself a generation past the cap. And an
obligation ledger holding an entry this binary cannot type stops the whole transaction, since a split records a
snapshot and one consumer per child on exactly that ledger and merging into one written back verbatim would drop
whatever it did not understand.

**The snapshot, before any child** (`late_snapshot.py`, over `git/snapshots/`). A split ends with the parent's branch
superseded, its pull request closed, and the parent itself an umbrella that implements nothing — so once children
exist, the only thing between them and the work they were told to reuse is one ref. Four properties, each a refusal
rather than a convention:

- **One namespace.** `refs/orchestrator/late-split/issue-<n>/cycle-<c>/gen-<g>`, built from the generation's own
  identity and validated against the one pattern this orchestrator writes — a custom ref namespace rather than a
  branch precisely because a branch is listed, protected by rulesets, attached to pull requests, and auto-deleted on
  merge, and a snapshot has to outlive all of that. A ref assembled from a damaged pinned field is refused rather
  than pushed. Whether a production token may write the namespace under production rulesets is proved before rollout
  by the capability check in
  [`../configuration/snapshot-capability-check.md`](../configuration/snapshot-capability-check.md).
- **No blind overwrite.** The remote is read first, so a retry finds the ref it already pushed and spends a read
  rather than a write; every write is lease-pinned to what that read established, a create leasing the ref as
  absent. A ref already carrying **another** commit is reported as a mismatch and left exactly as it is, because the
  automatic alternative is destroying the only copy of somebody else's candidate.
- **Exact SHA, proved twice.** The remote has to answer with the exact frozen candidate, and then the ref is fetched
  back into the clone the worktrees share and resolved locally — a namespace a token can write and not read would
  otherwise pass every check here and fail the first child that tried to use it.
- **The obligation is durable on both sides of the push.** The intended ref is written to `late_resources` as
  `pending` before anything is pushed (a push that landed and a process that died a statement later look identical
  from the outside) and moved to `retained` once proved. Every failure writes `failed` and parks with the recorded
  verdict standing, so the retry costs a GitHub read rather than an agent.

**Then the children** (`late_children.py`). The umbrella flag and `expected_children_count` go down before the first
child exists — that pair is what tells a partial split from a finished one — and each child's number is recorded on
the generation's own ordered register (`late_split_children`), in `late_consumers`, and as its own `child`
obligation in **one** write, before anything else is done with it. That write is the direct-consumer ledger slot the
reclamation rule waits on, which is why it has to be durable before the child can run.

The register is the generation's rather than the stage's shared `children` list, and that is load-bearing: an issue
that was decomposed, saw its children resolve, flipped back to `ready`, and implemented an oversized candidate still
carries the earlier decomposition's `children` and `dep_graph`, so a walk reading that list would adopt **completed**
issues by manifest index, reseed them with an ancestry they have nothing to do with, and activate them. The stage's
list and graph are written *from* the register instead, which is also what replaces the earlier decomposition's
dependency graph rather than leaving a stale one standing over the new children.

A resumed walk therefore adopts every child **this generation** already records rather than opening a second issue
for the same slice, and the recorded list is monotonic: a resumed pass never writes back fewer children than the
previous one knew about. Past that register there is one more recovery, for the crash between `create_child_issue`
returning and the write recording it — a window in which nothing outside GitHub knows the number. Every child is
created carrying a hidden marker naming this **issue**, this cycle, this generation, and its slice index, and a walk
about to create looks for that marker among the open issues on the child's own workflow label first, adopting the
orphan instead of opening a duplicate. The issue is in the marker because a cycle identity is minted per issue and
repeats across them — two parents adjudicating their first candidate are both cycle 1 — while the lookup walks a
label rather than one parent's children, so without it one parent would adopt, reseed, and activate another's child.
The lookup costs one listing and is taken only where something has to be created, so a fully adopted resume pays
nothing for it — and it fails closed: a 404 on the label means the repository has none, and therefore no orphan
either, since creating a child is what puts that label there; every other label failure is raised and parks, because
"could not ask" read as "there is no orphan" is exactly what opens a second issue for a slice that already has one.

Each child is born knowing what it needs and nothing more: its declared scope in the words the adjudication used, the
current base branch, the ancestor snapshot ref and exact commit, and the lineage and cycle identity a later record is
correlated by — written to the child's own pinned state as the `late_ancestry_*` group
([`../state-machine/labels-and-state.md#late-generation-state`][late-state]) and read fail-closed like every other
late field. Its body says how the work may be reused: **cherry-pick a coherent commit**, or **copy selected paths**,
and never split hunks mechanically to make the change smaller. File and hunk boundaries do not express issue scope, so
a change partitioned along them is one nobody can build or review — the judgment about what belongs to a slice stays
with the developer who implements it. The seed is re-applied on a resume by reading the child's state and adding to
it, never by writing a fresh record: by the time a retry reaches a child that was already created, that child may be
implementing.

**Only then the links and the supersession.** The parent says what it became and where its work went, exactly once,
and both halves of "once" are needed. The generation's own `late_links_announced` flag is the cheap gate and the one
that holds on the ordinary retry — it is scoped to this adjudication, unlike `decomposed_at`, which an **earlier**
decomposition of the same issue already wrote and which would therefore suppress the announcement entirely. The
thread is the gate that covers what a flag cannot: a comment that landed and a process that died before the write
are indistinguishable from the outside, so the marker this generation stamps into its own sentence is looked for
among the issue's comments before another is posted. That search is asked only when the flag is unset, so a resume
past the announcement costs nothing.

The held plan PR then gets its original description back and is superseded through the new `supersede_pr` helper:
one marked notice linking forward to the umbrella, every child, the snapshot ref, and the exact commit, and a close
if it is still open. Idempotent through the **thread** rather than through a receipt, since the comment and the
record of it cannot be made one operation. Both markers are scoped to the exact adjudication — the pull request
outlives a cycle and the issue thread outlives everything, so an unscoped one would read an earlier episode's
receipt as this one's — and both are honored only on a comment **this orchestrator authored**, since an HTML comment
is invisible in the rendered thread and anybody could otherwise post the marker to suppress the sentence it gates. A
merged or closed pull request is told and left alone; one that could not be read, or a release that failed on a
still-open plan PR, parks — and nothing is activated while a pull request carrying the superseded work is still
open. That last part is why the supersession runs on **every** pass, including one whose ledger already reads
`reconciled`: that entry records what an earlier pass did, and a human who reopens the pull request between the write
and the resume would otherwise have the resume skip straight past, report settled, and let the children loose beside
a change still carrying the superseded work. Re-asking costs one fetch and one comment listing, and neither step
repeats anything.

**Then the label, the retirement, and the activation, in that order.** The generation is retired in the same write
that hands the issue to `workflow:umbrella`: identity, both commits, and both ledgers kept, the measurement dropped,
and the recorded `pr_number` cleared. Dropping the measurement is what makes the label stick — a parent that has
become an umbrella has no candidate to measure, and a record still answering "oversized" is exactly what pins
`workflow:decomposing` and would have the relabel guard put the umbrella label back every tick. Activation runs after
that write for the reason the initial split's does: a crash between them must not leave a runnable child under a
parent still labelled `decomposing`, and a child with no recorded dependencies is picked up by the umbrella's own
walk as the retry.

**Cleanup last, and never in the way.** The superseded branch is written to the ledger as `pending` in that same
retirement write and attempted *after* activation: an attempt that does not finish records `failed`, emits
`branch_cleanup_failed`, and holds no child back. Children waiting on a branch deletion would be work stalled on
tidiness.

"The branch" is every surface it exists on — the remote ref, the checkout holding it, and the local ref — and the
entry reads `reconciled` only once all three are provably gone. A remote delete that succeeded beside a worktree
that would not come down is not settled: what is left is a checkout on a superseded branch that the per-tick base
refresh treats as a pre-PR tree and goes on merging into. The local half is *verified* rather than trusted, because
`git worktree remove` and `git branch -D` are best-effort helpers that report nothing — so the entry is decided by a
read taken afterwards, and that read fails closed. Taking the checkout down at all is safe here for one reason: the
snapshot was created and proved before any of this, so the commit it holds is no longer the only copy.

Only a branch this generation owns is ever deleted. The target comes off a ledger a human can edit and is spent on a
destructive call, so it is checked against the orchestrator namespace and against this issue's own number before the
remote is touched; anything else is recorded `failed` and left for a human, which is the one answer that neither
deletes somebody's branch nor quietly forgets the obligation.

What the obligation **does** block is the umbrella's own terminal completion, and the retry lives there rather than
in the transaction — an issue that has become an umbrella never reaches the transaction again, so nothing else would
bring a tick back to it. `late_cleanup.py` is asked at the one boundary where an unsettled obligation still matters:
every umbrella tick that finds every child resolved settles whatever is still owed, and the parent closes only once
nothing is. A refusal keeps the label, which *is* the retry, and leaves the parent visibly open instead of closed
over a remote nobody will ever reap.

That boundary is also the first at which the **snapshot** can go, and under the rule that owns it: a ref may be
deleted only once every recorded direct consumer is terminal, and all-children-resolved is exactly when that becomes
true for the consumers this split created. The dispositions are read off the child scan the umbrella already took,
so proving it costs no request of its own, and `done` covers a nested split too — a child that reached it has
published, so its own descendants are past needing the ancestor. Anything that cannot be proved keeps the ref: a
consumer missing from the scan, one wearing a label this binary does not recognize, or a consumer ledger it could
not type. Deletion is idempotent because an absent ref is a success, so the crash between the push that removed it
and the write that would have recorded it costs one request on the retry — and it is named against the commit the
split preserved rather than against whatever a fresh read observes, so a ref somebody re-pointed is a mismatch left
for a human instead of the one blind write in the whole namespace, aimed at destruction.

`retained` never blocks the terminal and `failed` always does, and that asymmetry is the safety argument. A ref kept
because a consumer could not be proved terminal is one a later sweep settles — the closed-owner sweep and the
cancellation rules are the change that follows this one — and blocking on it would hold the umbrella open for a
condition nothing here can clear. A ref the remote *refused* to delete is a permission or ruleset problem an
operator has to see, and the parent staying open is how they see it.

Two more things block it outright, and both are the same rule: nothing that cannot be *proved* settled lets a
terminal fire. An obligation ledger this orchestrator could not fully type blocks whatever the typed view says — the
entries it could not read are still obligations, and closing on the strength of a projection is the reading the
verbatim copy exists to prevent. So does a ledger holding anything at all on a record whose cycle identity is
damaged: there is nothing to correlate a reclamation to and no issue number to prove a branch belongs to this
generation, so the umbrella stays open and says so where an operator reads it. An issue that never entered the late
gate carries no ledger and answers without a write, which is every umbrella the initial decomposer made.

## What the humans can still change while a candidate is frozen

Adjudication takes minutes to hours, and the issue is a live thread the whole time. Two local fingerprints watch it —
`late_title_body_hash` over the title and body, and `late_comment_hash` beside the `late_comment_watermark_id` it
covers from — and they are deliberately separate from the global `user_content_hash`, which keeps its single baseline
and its meaning so nothing here fires the re-decompose or dev-resume routes that read it. What counts as a human's
words is that hash's own trust filter all the same, so an outsider, a third-party bot, and the orchestrator's own
comments shift nothing. The fields themselves are in
[`../state-machine/labels-and-state.md#late-generation-state`][late-state].

The first tick of an adjudication takes the baseline: whatever the issue says then is what the candidate was frozen
against, and nothing on the thread counts as an answer to it. Every tick after that compares.

**Drift outranks every answer.** An edit to the title, the body, or a comment already counted into the baseline
changes what the candidate is supposed to BE, and an answer that arrived in the same window was written about the scope
as it stood before — applying it would adjudicate a reply against requirements it never saw. So the tick that first
sees drift parks (`late_content_drift`) and consumes nothing: the frozen commit, the late session, the recorded
generation, and any plan-PR hold are all left exactly as they were, because none of them is wrong, only unadjudicable
until a human says what the edit meant. A comment rewritten in place is drift for the same reason a title edit is —
it moves no comment id, so there is no new comment to read the change out of.

The park is a *response boundary*, not a one-tick delay. What counts as a reply is read against the higher of the
generation's own comment watermark and the issue-wide `last_action_comment_id`, which every announced park advances
past the notice it just posted — so an answer written before the human was told anything cannot resolve the park on
the next poll either. Nothing advances that watermark without consuming what it advances past, so the conservative
reading costs no real reply.

**Then the reply resolves it, and the two kinds of reply mean opposite things.** A bare `/orchestrator continue` is a
certificate: the committed work still answers the updated issue, so the fingerprints are re-baselined onto the content
as it now reads and the SAME frozen candidate goes on to be adjudicated — against the updated requirements, which is
why a verdict recorded before the edit is dropped rather than reused. The certificate covers the commit, not an answer
taken against a scope that has since moved: acting on one would be the drift rule refused a step later, with a split
creating children that describe requirements nobody is asking for any more. What it does buy is everything else —
the candidate is not re-derived and no developer is paid for. Substantive guidance is not a certificate — it says the
work has to change, so the developer session is resumed against it (above) and the candidate is re-frozen from what
comes back.

An edit taken back needs no reply at all: the candidate matches the issue again, so the park is cleared and the
adjudication resumes. Leaving it standing would not be harmless — `awaiting_human` is exactly the flag that
suppresses the announcement a question verdict earns, so a reverted edit would silence a question recorded and never
said out loud. Guidance that came with the revert is still guidance, though, and it is routed before any of that:
taking the edit back decides which requirements the change is asked against, not whether it was asked for. Absorbing
it into the baseline instead would consume a human's instruction without acting on it, and then reuse a verdict
nobody re-earned.

**A revised candidate is proved, not trusted.** The tree has to be clean before anything is read off it — a candidate
measured beside uncommitted changes is not the one a publication would push — and the commit the checkout ends on is
frozen and measured again from scratch under the ceiling as it stands now. What is not allowed is
skipping the measurement, which is why the generation counter advances on every reconciliation that lands — a recorded
verdict answers a cycle, a generation, AND a commit, so an acknowledged candidate is adjudicated against the
requirements that changed rather than answered from the record taken before they did.

The resulting SHA is allowed to be the one that went in, but only when the developer *said* so. The prompt asks for
the same `ACK: <justification>` marker every other drift resume asks for, and that marker is what an unchanged commit
needs before it is re-measured. Without one, an unchanged commit is not an acknowledgment — it is a run that said
nothing, asked a question, or timed out before it could do either, and all three look identical from the checkout.
Reading any of them as "the work already covers it" would advance a generation and adjudicate a candidate nobody
vouched for, so they park (`late_revision_unanswered`) with whatever the developer *did* say quoted, so a question
reaches the human it was meant for. A commit that MOVED needs no marker: work that changed HEAD speaks for itself. The
one path where an unchanged commit passes without one is the human's own `/orchestrator continue` on a stalled
revision — they have read the park and accepted the commit as it stands, which is the same thing the marker says.

A reconciliation that could not
be completed parks (`late_revision_dirty` / `late_revision_unmeasured`) with the generation exactly as it was, and a
bare continue re-runs that reconciliation alone rather than paying for a second developer run that already finished.

**Guidance means the same thing with nothing parked.** An adjudication in flight, or one that already recorded a
verdict, is still work a human can ask to be different — so the developer is resumed there too, and the re-measured
candidate that comes back advances the generation, which is what stops a verdict taken over the old work from
applying to the new. Folding the comment into the baseline instead would consume an instruction without acting on it.
The one reply that lands here with nothing to do is a bare `/orchestrator continue`: no park is waiting on it and no
candidate needs certifying.

**A categorized question is reopened only by a real answer.** Substantive trusted guidance drops the recorded outcome
— the record is exactly what suppresses the next spawn — so the adjudicator runs again against what the human said. A
bare continue may not: a question is not a step that failed, and "proceed" is not an answer to "which half of this is
in scope". The command is consumed, the refusal is posted once, and the issue stays parked on the question it is
really waiting on.

**Nothing outside the adjudication may decide it either.** While a generation is live — recorded, not cancelled, and
either oversized or still owing an owner read — `workflow:decomposing` is the label it sits on, and both ways that can
be taken away amount to publishing an unadjudicated candidate. The owed read is the half that is easy to miss: a
revision that came back UNDER the ceiling closes the size question and leaves the owner one open, so every gate keyed
to size would wave it through while nobody had established that the issue is still there.

`DECOMPOSE=off` routes a `decomposing` issue into the legacy implementing flow, which is
right for an issue only waiting to be decomposed and wrong for one whose implementation is already committed and
measured past the ceiling, so the route is refused while a generation is live — the switch still keeps NEW candidates
out of the gate, it just does not decide the ones already in it. A hand relabel is caught a step later, since the
label is
already gone by the time anything reads it: the dispatcher asks before it routes anything, and an issue whose label a
human moved is put back on `workflow:decomposing`, told why, and left for the next tick rather than handed to the
stage the new label named (`late_relabel.py`). The refusal is the safety property and the relabel only the repair, so
a label write that cannot land still stops the dispatch, and so does a pinned read that cannot be taken. The
restoration itself goes out UNGUARDED and posts its notice only after the label lands: the transition graph describes
the moves this orchestrator makes, and putting back one a human made is not among them, so under
`WORKFLOW_TRANSITION_GUARD=enforce` a guarded `validating → decomposing` repair would raise every tick and strand the
generation under the wrong label — announcing itself again each time. That last
one is the single place this guard does not follow the additive-safety-net convention the pause probe reads by,
because the costs are not symmetric: failing open publishes an unadjudicated candidate — the handler behind it reads
the same pinned comment, and a first read that failed transiently is followed by a second that may well succeed —
while failing closed costs one tick of one issue, retried on the next poll, during an outage in which nothing else
was going to make progress either. Neither
clears, cancels, or decides anything — a
generation an operator really wants gone goes through the late domain's own cancellation, which records what the
remote is still owed.

## Local verify gate (not an agent)

After the reviewer emits `VERDICT: APPROVED`, `_handle_validating` runs the configured `VERIFY_COMMANDS` directly in
the per-issue worktree — these are plain shell commands, not an agent role, so no `*_AGENT` env var applies. The gate
runs before the approval comment, the squash, the watermark seeding, and the `workflow:documenting` (final-docs) label
flip. A clean run advances the issue; any failure parks on `workflow:validating` with a typed `park_reason`
(`verify_failed` / `verify_timeout` / `verify_dirty` / `verify_head_changed`). See
[`../configuration.md#local-verification-gate`](../configuration.md#local-verification-gate) for the env-var
reference.

[workflow-labels]: ../state-machine/labels-and-state.md#workflow-labels
[late-run]: ../state-machine/labels-and-state.md#the-late-run
[late-state]: ../state-machine/labels-and-state.md#late-generation-state
