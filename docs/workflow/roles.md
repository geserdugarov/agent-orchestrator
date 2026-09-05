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
  the dev to make a change — with one park excepted, on the same terms as the decomposer's below: an
  `implementing` issue stopped on its spent spawn budget (`retry_cap`) resumes on no reply at all, and the trusted
  `/orchestrator continue` that renews the budget retires the session as it buys the attempt (`dev_agent` kept), so
  what a human pays for is a fresh conversation rather than a replay of the one that ran out. The locked
  `(backend, args)` spec is re-parsed on every resume from pinned `dev_agent` so
  a config flip mid-flight cannot retarget the session. `_resume_dev_with_text` on
  `workflow/stages/implementing/resume.py` is the one module every one of those resumes goes through; it declares the
  call signature its callers were written against, then binds those arguments into a typed request/context before
  executing the resume.
- **Reviewer freshness.** `_handle_validating` spawns a fresh reviewer subprocess every round with no resume, so
  `REVIEW_AGENT` changes take effect on the next validating tick. The current value is recorded in `review_agent` for
  traceability only.
- **Decomposer reuse.** `_handle_decomposing` spawns the decomposer once and resumes it on every awaiting-human
  reply — with one park excepted. An issue stopped on its spent spawn budget (`retry_cap`) is waiting on a human
  deciding to spend more of this issue's day on it rather than on words for the agent, so a reply resumes nothing
  there: the tick holds until a trusted `/orchestrator continue` renews the budget, and what that buys is the fresh
  spawn the budget refused. The locked session is retired as it is bought, `decomposer_agent` kept, so the run it
  pays for is a new conversation rather than a replay of the one that ran out. What the park holds, what the command
  lifts, and what one lifts it to are in
  [state-machine/delivery-stages.md](../state-machine/delivery-stages.md#_handle_decomposing-label-workflowdecomposing).
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
  timeout and a completed reply. A spent spawn budget parks this road too, on the same terms as the two above and
  under the same `retry_cap` reason — a reply resumes nothing there, and only a trusted `/orchestrator continue` buys
  another adjudication. It is the one of the three that retires no session as it buys one: the pre-spawn record
  already drops `late_session_id` for every run that is not continuing a question a human has answered, so what the
  command pays for is a fresh conversation before the agent starts rather than a retirement taken beside the grant.
  The run happens in the issue's OWN worktree rather than a scratch checkout of the base branch, because the diff
  it is asked about is between two commits nothing has pushed. What reaches the coordinator is the first thing a
  `decomposing` tick asks (`_late_adjudication_owns_the_tick` on `stages/decomposition/run.py`): an issue whose record
  carries a live generation belongs to it entire, and no step of the initial decomposition runs for it. It is asked on
  every tick of that label rather than only the ones that look late, because the reconciliations it opens with are
  owed by exactly the records the gates below it route past. What PUTS an issue there is the size gate below, and the
  two refusals beside it keep anything else from deciding a live generation: one stops `DECOMPOSE=off` from routing an
  unadjudicated candidate to implementation, and one stops the dispatcher handing a hand-relabelled issue to whichever
  stage the new label named.
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

## The lifetime agent-run ledger every role spends from

Every role above reaches an agent through one boundary, and the boundary charges the issue before a process exists
([`../state-machine/labels-and-state.md#the-agent-run-circuit`][agent-run-circuit]). What it counts is
**processes**, not ticks and not rounds: a reviewer round that requests changes and resumes the developer inside the
same tick pays for two, a developer resume that lands on a transcript the backend has lost pays for the fresh spawn
it buys itself, and a run the shutdown sweep killed or an operator paused mid-flight pays like one that finished —
each cost the same minutes of somebody's compute, and only the disposition is thrown away.

The ceiling is `MAX_AGENT_RUNS_PER_ISSUE` (default 50, `0` = unlimited), or the issue's own `agent_run_allowance`
where a human has bought it one. It is a **lifetime** total, so nothing gives a run back — and in particular none of
the counters a role does reset is this one:

- `review_round` goes back to zero on a pushed base rebase and on a recovered conflict, so `MAX_REVIEW_ROUNDS` can
  be spent over and over on one issue;
- the 24h `retry_count` window empties on the clock, so `MAX_RETRIES_PER_DAY` is spendable again tomorrow;
- `dev_resume_count` reaching `DEV_SESSION_MAX_RESUMES` retires the transcript and the next round spawns fresh;
- an authorized restart of a cancelled late cycle projects a whole fresh cycle over the pinned comment, dropping
  every counter it is not told to keep — `conflict_round` among them, which nothing short of a restart resets — and
  the allowance and the spend are two of the few it keeps.

So an issue can move between `workflow:implementing` and `workflow:decomposing`, be adjudicated once per committed
candidate, rotate sessions, and answer review round after review round without any of those loops ending — and the
ledger is the one thing counting them. When it is spent, the issue parks on `agent_run_limit` and the dispatcher
holds it ahead of every handler above; only a trusted, bounded `/orchestrator add-agent-runs N` widens what it may
still spend ([`../state-machine.md#per-tick-flow-workflowtick`](../state-machine.md#per-tick-flow-workflowtick)).

The caps each role already had still refuse first — a cap that fired only after the charge would spend a run on work
nothing ran — so a spent review cap, conflict cap, or daily spawn budget parks its own way with the lifetime count
untouched.

## The size gate a committed candidate passes

No agent runs here. This is the seam that decides whether an adjudication happens at all, and it sits at the one
place every clean committed developer outcome publishes through (`_publish_committed_work` on
`stages/implementing/disposition.py`, measured by the `stages/implementing/late_gate.py` owners) — a run that
finished, a
timeout that had committed before it was killed, and a branch a crash stranded all reach it, which is what makes the
measurement a contract rather than a check.

Only a CLEAN tree is measured, and clean is **proved** rather than inferred from an empty answer. A candidate
measured beside uncommitted changes is not the candidate a push would publish, and the diff it would be adjudicated
on is not the one a human would read, so a dirty tree parks exactly as it always did and never reaches the gate. A
tree `git status` could not report on is refused there too, under its own `unreadable_worktree` park: the list form
of that read maps its own failure to "no paths", which is the answer a clean tree gives, and a seam whose next step
is a push may not rest on a probe that never ran.

The order is the failure contract. The candidate is proved to be a commit this host holds (resolved and peeled, so a
revision this repository has never seen is work made somewhere else rather than a head to stand in for it), the base
is frozen from what the **remote** says the branch is at (not `refs/remotes/...`, which lives in the object store the
agent's own worktree shares), and both are persisted with `late_phase=measuring` BEFORE a line is counted. A tick that
dies over the count comes back to the pair this one froze rather than to one re-derived from a branch and a remote
that have both moved, and the same freeze holds the branch out of the ordinary base refresh for as long as the
generation lives.

**A resumed run is judged against what it started on**, not only against what the branch inherited. The floor says
which tip the branch was already carrying — a certified handoff, a frozen candidate, or a commit an approval owes a
push for — and `before_sha` says which tip *this run* began at, and those are different commits whenever a human's
guidance resumed a developer on top of inherited work. A head that has not moved since the run began is a run that
committed nothing, whatever else is on the branch: published on the ahead-of-base reading alone, the developer's
clarifying question would be dropped and the very commit the human was still deciding about sent to review.

Both are comparisons, so **both ends have to have been read**, and a run either end cannot be established for
publishes nothing. `_head_sha` reports its own failure as `""` — the one value that cannot be a commit — so an
unread end differs from every commit there is, and the difference belongs to the probe rather than to the run. On a
branch that arrived already ahead of base (one a read-only relabel certified, one a size-gate park left a candidate
on, one guidance resumed a developer over) that difference publishes work the run never made, or hands the gate a
candidate nobody wrote. The refusal parks a finished run's commits behind a reading nobody got, which is a bounded
cost — the commit is still in the worktree and the park says so — where the other way round pushes somebody else's
work under this issue's name.

**What the gate hands back is the commit**, not merely its permission, and the push is named against it. The gate
reads the checkout and the publication writes it, and `HEAD` between those two moments is not necessarily the commit
that passed — another tick, an operator, or a descendant the timeout cleanup raced can move it — so a push that named
nothing would publish work no measurement ever saw while the record named the commit that did.

The **checkout** has to be on that commit too, and this is the one refusal here a human answers without writing
anything. The push would be safe on its own, but every stage past the handoff works from the worktree — the reviewer
reads a head ahead of the pushed branch as unpushed work, the squash rewrites what is on it, the docs pass commits on
top — so a checkout that has moved is parked as `late_candidate_moved` rather than handed to review. It is asked
*twice*, before the push and again once the pull request is open, because those three requests are where the window
is: the worktree is writable while they run, and a descendant the timeout cleanup raced is the commonest thing to
move it. The second refusal does not take the publication back — the commit is on the remote and its pull request
carries it, which the record says — it stops the *handoff*, so review never reads the descendant.

Both boundaries ask about the **tree** as well as the head, and for the same reason one step over. Loose work can
appear beside a commit without moving it, so every proof about the commit passes while the checkout stops being what
the gate measured — and the stage the handoff passes it to takes no reading of its own, so uncommitted work that
slipped in reaches the squash and the docs pass, which commit it or destroy it. A tree that is dirty, or that could
not be read at all, takes the same `late_candidate_moved` park on the same terms: nothing published before the push,
and the publication left standing with only the label withheld after it.

**One commit is decided on before any of it**, and everything downstream is named against that: the push carries it,
the receipt the handoff leaves says it, and the post-push proof asks the checkout for it. Where the gate proved a
commit, that is the one; where it did not — a candidate the switch kept out of the gate — the checkout names it,
because a push named against nothing publishes whatever the branch has become by the time git runs it and leaves
nothing on the issue afterwards saying which commit that was. And a checkout that cannot name one is refused rather
than pushed as it stands: with no name there is no receipt and nothing for either proof around the push to hold it
to, so it parks as `late_candidate_moved` and waits for a repository somebody can read.

The intent is made durable before the push and only
where the record does not already carry it, so the roads an approval or an adjudication already wrote pay nothing
and the rest pay one write: a tick that died between the push and the handoff would otherwise leave a published
branch and a record saying nothing was ever owed. The approved
commit is on the record as `late_approved_sha` before any of this, because by then nothing else on the issue names
it: the generation is retired ahead of the effects it licenses, on purpose. So the park has a way back that costs
nothing. Every tick
asks the checkout one local question against that record and says nothing until the answer changes, and a worktree put
back on the approved commit publishes on the next poll — no reply, no guidance, and no second developer run over work
that is already committed and already measured. It also freezes the branch out of the pre-tick base refresh, because
that is what makes the remedy reachable: a rebase between the operator's `git checkout` and the tick that would have
noticed moves the head off the approved commit again. A worktree deliberately left on the descendant is measured as the
fresh candidate it is instead.

**What a record already names is what a later tick reconciles**, and the current head is never a substitute for it.
Where `late_candidate_sha` is set, that commit is proved *first*: a host that cannot peel it is one the work was not
made on — a rebuilt checkout, a machine the branch never reached — and it parks rather than measuring, adjudicating,
or publishing whatever the branch points at there. `DECOMPOSE=off` does not license publishing it either; the switch
decides what is measured, not what may be pushed unproven. Only once both commits are proved present does a head that
differs mean what it usually means: the developer was resumed on a human's guidance and committed again, which is a
fresh candidate under a fresh generation of the same cycle — and, with the switch off, one published while the record
it supersedes is retired rather than left freezing the branch over work nobody will publish. The recorded **base** is
retried the same way: the object it names is asked for again (fetching once for it), never the remote, because a
remote re-read would answer with wherever the base branch has moved to and measure a different pair under the same
generation. That is why a base the remote named but this host could not read is recorded *beside* its failure — the
id is the only thing a retry has to ask for. A count already on the record is acted on only once it is a WHOLE
measurement: a base, a ceiling, and a boundary beside it, because the record's own comparison answers "not oversized"
on a missing threshold — which is a damaged record publishing as a small candidate — and because a count whose base
this host cannot show is a number with its evidence missing. The identity carries the same weight and is asked
through the late domain's own record gate, so what a measurement may be acted on under is exactly what a record of it
may be written under: a cycle, a generation, a root, and a `late_current_issue` naming *this* issue. Nothing
downstream reads those fields, which is why they are easy to lose and why losing them fails open — a count that
publishes but cannot be correlated is a reading no operator can defend afterwards, and one recorded against another
issue is not this issue's answer at all. Anything short of that parks for repair rather than being read as an answer,
and the refusal is reported under a minted identity rather than under the record it is about, so a damaged pinned
comment cannot take its own refusal down with it. That repair is the reporting identity every late refusal here goes
through, not the damaged-count case alone: the evidence refusals taken before any measurement — a reaped worktree, a
recorded object this host cannot show, a base it no longer holds — would otherwise emit nothing at all for a record
whose root is gone, and would file the failure against the wrong issue for one whose `late_current_issue` names
another.

The same rule reaches one step before the gate. A tick that recorded the pair and died before counting or parking it
leaves a frozen candidate with nothing on the issue saying the workflow is waiting, so the record is reconciled ahead
of any spawn: on a host that cannot show that commit — a rebuilt checkout, a machine the branch never reached — the
issue parks asking for the worktree rather than paying for a second developer over work the first one finished. The
checkout has to be *on* that commit too, because no developer ran on that path: a head past the record is a resumed
developer's new work only where a developer was resumed, and everywhere else it is a checkout somebody moved.

And one step *after* the verdict, where the same window opens with the record already gone. Every approval — the
retirement a small candidate earns, and the exemption a `single` verdict is settled by — drops the record that named
the commit and licenses a push that has not run yet, so `late_approved_sha` goes down in that same write and the
commit it names is proved before anything spawns. What is proved is the **head**, not the object: holding the object
says only that the store was never pruned, and the store outlives the branch — a worktree rebuilt or reset on the very
host that made the commit still has it sitting in the object store it shares. The proof is taken once the checkout has
been restored, which is what tells the two hosts apart: a commit the branch already carries comes back with it and the
tick proceeds, while a checkout standing anywhere else parks as `late_candidate_moved` — nothing published, no second
developer, and the same quiet republication settling it once the worktree is back on that commit with a provably
clean tree around it. What that park records is what the side of the gate it was taken on can promise: an INITIAL
publication writes the commit alone, since its push is the one that opens the pull request and reads the remote for
itself, while a push onto a pull request the remote already carries writes the commit AND the head to pin the
republication against — both being the commit that just landed, which is where that push left the branch. Half a pair
there is not a smaller debt but a claim the reconciliation refuses as damage, under a reason only a human clears, so
restoring the worktree would never finish the retry this park documents.
Both halves are asked there, or the republication would walk straight back into the refusal
the park was taken on and post a fresh notice every poll for a checkout nobody has touched. The record is spent by the
handoff that pays the debt, and spent *durably before the relabel*: past that label the issue belongs to
`validating` and implementing never runs on it again, so an approval still standing there is one nothing will ever
drop — and it goes on freezing the branch out of the base refresh for the rest of the issue's life. The same write
records which commit the push carried, because the label is the one effect that can fail on its own: refused, the
issue is still implementing with its branch pushed and its pull request open, and that record is what stops the next
tick re-deciding a published branch and what has it reuse the pull request and land the label instead. The head has to
be what is asked,
because a branch with no commits ahead of base reads as an issue with nothing to publish: a checkout holding the
object while standing on the base would be handed a second developer run for an implementation already written. An
adjudication takes the record off again, since a candidate being decomposed is one nobody is publishing yet.

**Answering a recorded reading is never new work**, and that is what keeps `DECOMPOSE=off` from failing open on it.
A tick answering a reading a previous one recorded — the bare-continue retry, the stranded pair — reaches the gate
with the switch off exactly as it does with the switch on. Publishing the current head there would publish the very
commit whose reading is what somebody asked for. New work is still bypassed; a question the gate already asked is
still answered.

The claim is narrower than *no developer ran*, and the two are separate facts for that reason. A base rebase, a
conflict resolution, a divergence publish, and a recovery push each reach the gate with no agent behind them and with
nothing on the record asking for their commit to be read — fresh work, which the switch off publishes unmeasured.
Answering the switch with the wider fact would measure them, which is the same failure one direction over: an install
that turned the gate off having its branches routed into an adjudication it never opted into.

**Every refusal keeps the identity it managed to establish**, and one that established none refuses the retry
outright. A revision that *resolved* and would not peel — an object a prune took, work made on a host this one is
not — comes back carrying the id it resolved to, and that id is recorded with the park: from there the retry asks for
that exact object, the pre-tick base refresh holds the branch still around it, and the reconciliation ahead of the
next spawn proves it before anything runs. A revision that would not resolve at all names nothing, and there the park
itself is the record. Its bare continue is refused rather than answered, because there is no pair to re-read: what a
retry would take is a *first* reading, of whatever the checkout points at by then, and nothing ties that head to this
issue — a rebase, a reset, or a rebuilt worktree each leave one. The way on is the developer, which is what guidance
buys. The park holds the branch out of the base refresh for as long as it stands, for the same reason: rebased under
it, the frozen commit is gone and the checkout the refusal was protecting is standing on the base, so neither the
exact-pair retry nor the refusal has anything left to be answered from.

A reconciliation stays bound to the recorded pair *for the whole tick*, not only at the door. Both roads prove the
head against the record before they start and the gate reads it again a moment later, and the checkout is writable
in between — so a
head that differs on the second reading is one something moved mid-tick, not a run's output, because no run of this
tick exists. Read as fresh work it would be measured and published with the switch on, and pushed unmeasured with it
off. So the second reading is refused exactly as the first would have been: nothing is published, the recorded pair
stays for the retry, and the ordinary disposition — where a moved head really *is* a resumed developer's new commit —
is deliberately not held to it. The refusal comes before the head is asked whether it is *readable*, because a head
that moved onto a commit this host cannot peel still names one — and a named commit handed on from there is one the
park downstream records, minting a generation around it and dropping the very pair the retry exists to re-read.

What the number earns:

- **At or below `MAX_ADDED_LINES`** — the ordinary publication, unchanged. The generation is dropped in the same
  breath, because a frozen candidate freezes the branch and a record carried into the stages that close the issue
  reads as a live cycle a close should end; `late_retired_cycle_id` outlives the drop, so the next candidate on this
  issue mints a cycle after it rather than reusing the number. That drop is also the barrier: past it come a pushed
  branch, an open pull request, and a relabel to `workflow:validating`, none of which may happen on an issue a human
  has closed. A latched close is asked for ahead of the write, and the write is held inside the observations owner's
  retirement window so a close arriving as the record stops naming its cycle is answered behind it — the generation
  goes back from the call's own memory and is cancelled from there, with nothing published to take back.
- **Strictly past it** — nothing is pushed, no pull request is opened, and the label moves to `workflow:decomposing`
  with the measurement durable ahead of it. The commit stays exactly where the developer left it, which is where the
  adjudicator reads it and where a `single` verdict publishes it from. A tick that dies between the write and the
  label leaves a live generation under `workflow:implementing`, which the dispatcher's relabel guard puts back.

The same three answers stand one seam later, where a pull request already carries the work. Every push onto one is
measured first — the reviewer's requested changes, a human's reply, a body edit, a stranded commit a bounce
republishes, a deferred push or a timeout's commit a recovery finishes, all three conflict publications, the base
sync's own auto-rebase and crash recovery, and the final docs pass —
and measured for what the pull request would **come to** rather than for what that push adds: the pair is the frozen
remote base and the candidate, so what a reviewer would open and read is the number the ceiling is applied to, and no
branch can be grown past it one small fix at a time. What such a call has to freeze first, and could re-derive from
nothing afterwards, is which publication it was entered on: the stage the gate is taking the issue out of, the pull
request the work already has, and the head that pull request is standing on. A candidate at or below the ceiling is
pushed as it always was — named against the commit that was measured and leased against that frozen head, so neither
a checkout that moved nor a pull request somebody pushed to can turn an approved reading into an unmeasured
publication. That head outlives the record it came from: the write that approves a candidate retires the generation
while the push it licenses has not run, so the head rides on the approval and pins every later push for that commit —
the retry after a failed one, which skips the measurement because the commit is already approved, and the ordinary
publication a settled verdict hands it back to. One past the ceiling is held with the pull request left exactly
where it stood, and the issue is adjudicated from whichever of `workflow:validating`, `workflow:documenting`,
`in_review`, `workflow:fixing`, or `workflow:resolving_conflict` that push was reached under. A tree that is not
provably clean, a pull request nothing could read, one that is closed or merged, and a head that moved off what a
live record froze are each a refusal rather than a reading: nothing is pushed and a human is asked.

The freeze that precedes the count is a step, not a window. A tick that dies between the two leaves a pair with no
number on it, and the next tick answers that reading before its stage runs at all — on the stage the record names,
with no developer having run, so a checkout that has left the recorded candidate is refused rather than measured. It
is the same contract the implementing recovery keeps, asked one seam later. A checkout that is not on this host at
all stops the tick rather than letting the stage carry on: the commit is on a machine this one is not, so the pair
keeps the branch and the record exactly as they are and a human is asked once.

A verdict taken past publication publishes the candidate itself and then continues at the stage the record names,
rather than sending every one of them back to `implementing`: that stage is the only owner of the completion the
candidate still owes, and the settlement is the last tick holding the head the reading was taken over. It proves its
pull request before either — and a pull request already standing on the accepted candidate is that proof answering
"this settlement's own push landed and the tick died before the label", not "somebody moved it", but only where a
durable record vouches for the push: the approval written in the write ahead of it, or the receipt the push itself
leaves read with the head that receipt replaced. On a fresh pass neither is written and nothing of this workflow's
has reached the remote, so that same head is something ELSE having pushed — an agent that published its own commit
is the plain case — and it refuses with every other moved head. The receipt needs its head because it is never
cleared: an accepted candidate published in an earlier round is one it goes on naming, and a pull request rewound
onto that commit would otherwise agree with every local fact there is. Vouched for, the retry finishes
what it never reached instead of refusing the publication this verdict made. A pre-publication one
searches for the pull request its commit is on and drops a recorded pointer that turns out settled, because losing it
costs nothing — the publication opens the pull request the work needs. This one already knows which pull request the
reading was about, so it checks instead: still open, and still standing where the reading found it. A check that
fails parks. Dropping the number there would push onto a branch whose pull request a human settled and open a second
one for a change adjudicated against the first, and publishing over a head that moved would publish on a reading the
branch has already overtaken.

- **Answered small after a revision** — the one record that wears `workflow:decomposing` with nothing left to
  decompose. A developer revision guidance bought comes back re-frozen and re-measured, and a candidate now at or
  below the ceiling has had its question answered: the label goes back to `workflow:implementing` and the ordinary
  publication reconciles the exact commit already on the branch. Handing it to the initial decomposer instead would
  re-plan an implementation that is already written. It owes the pull requests the same two reconciliations an
  accepted verdict does, and for the same reasons: the "do not merge" notice comes off the held PR before anything
  else moves — a refusal parks under `decomposing` with the record untouched — and `pr_number` is moved onto the
  pull request the measured commit is on, or dropped where the recorded one is settled, since a merged plan PR
  carried across ends the issue as `done` on a design. The record is kept across that handoff and retired by the gate
  on the other side, which is what makes it recoverable — it is the only thing saying the question was asked and
  answered, so dropping it before a label write that then failed would leave an issue nothing could tell from one
  that never entered the gate.
- **No reading at all** — never "small". What a failed `git` invocation writes to stdout is what a candidate
  that changes nothing writes, so a typed `late_failure` carrying `measurement_failed` goes to both sinks — for
  every refusal, including one taken before a generation existed, which is reported under a minted identity —
  the issue parks `late_measurement_failed`, and the pair that was frozen stays on the record. The record says the
  same three things the notice does: the family, the `measurement_failure` step it stopped at, and the `detail` line
  that step wrote, so an operator reading only the stream can tell these apart the way the human on the thread can
  ([`../observability/event-streams.md`](../observability/event-streams.md#late-split-records-both-sinks)). A base the
  TRANSPORT could not reach is the one exception, and a bounded one: `base_unreadable` and `base_absent` clear
  themselves, so the first three consecutive misses on a pair count one on `late_measurement_miss_count`, emit that
  same typed failure, log at WARNING and stop with nothing parked and nothing said — the next tick re-reads the pair
  by itself, spawning nothing — and only the fourth asks a human. Every park here asks once per thing there is to
  say, whatever its cause: a reading retaken with nobody asked and stopping where that park's own notice already said
  it stops — which is what `late_measurement_failure` records, written by the roads that tell somebody and by no
  other — is held silently, while one that stops at a *different* member is announced once and takes its place. What
  retakes it is the road the gate was entered on: past publication the reconciliation ahead of every handler re-reads
  the parked pair once a poll, which is what that silence is for, while before it the park owns the tick until the
  bare continue below arrives — and that reading, being a human's answer, is announced again if it misses. A base
  this host does reach ends the run of misses in the write that records the pair,
  and the member beside the count stays, since reaching the base is not the last step a reading can stop at. Both go
  together, with the park, on the verdict a reading that finally lands settles. Every notice names the member
  and explains it in a line written for the operator — which of a remote, a token, a checkout, or a planted
  attribute file they are looking at, and, for the remote read, the fetch and the two diff steps, the
  `orchestrator.git_plumbing` channel their invocation is logged under — with whatever the failing step wrote for
  itself, scrubbed, carried up beside it. A trusted bare `/orchestrator continue` re-measures exactly that pair and
  re-publishes through the same seam; it spawns no agent, since the developer that produced the commit finished long
  ago — and because no agent ran, a checkout that has left the recorded commit is refused rather than measured or
  published, under either switch setting.
  Guidance is the opposite reply and buys the opposite thing: the developer is resumed, and what it leaves is
  judged against the floor the park left on the branch rather than against the base, so a clarifying question is
  answered by parking on it rather than by publishing the very work whose size nobody could read. A worktree
  that is gone is answered by that same reconciliation rather than handed on: the generic parked-continue
  classifier would refuse the command as carrying no guidance, which is the wrong thing to tell an operator
  whose command is exactly right, so the park is re-taken saying the recorded commit is not on this host and the
  next continue retries it once the worktree is back.

Five candidates skip the measurement and none is a bypass. Three are commits this workflow has already *decided*
about, and each names one commit and only it, so work committed on top of any of them is measured as the fresh
candidate it is. `late_exempt_sha` names the commit an adjudication accepted; between the verdict and the publication
it also holds the branch out of the base refresh — but on two conditions, since the record is never cleared and
freezing on its presence would take every issue that ever earned a verdict out of the refresh for good. The head has
to still be that commit, and the stage that has to act on it has to still have the issue: past the handoff the branch
is review's, and keeping a pushed branch in step with base is the PR-aware sync's job.

`late_approved_sha` names the commit the gate itself approved and has still to push, which a crash
in that window brings back here with its generation already retired: re-deciding it there would measure a settled
question against a base that has moved since, and route work a human may already have adjudicated back into
adjudication. `implementing_published_sha` is that same window one step further on and the one that matters most,
because the effects are already out: past the push a pull request carries the work and only the relabel is owed, so a
reading that came back oversized there would hold nothing back and route a *published* branch to adjudication. And
`DECOMPOSE=off` keeps every new candidate out of the gate — but not one this issue already has a
recorded generation for, nor one it owes a push for, because the switch decides what ENTERS the gate and nothing
about what is already in it or already through it. Bypassing an approved commit would be the sharpest of those: the
publication is handed a candidate the gate never looked at while the record beside it names a different commit as the
one still owed a push.

The fifth is the only one no record names in advance, and the only one that has to *earn* its way past the reading:
a workflow rewrite of the exact commit an adjudication accepted. Three of them replace that commit with an object
carrying the identical contribution: a squash on approval, on the last push before the merge button; the clean base
rebase the per-tick refresh force-pushes once the stage that had to act on the exempt commit has handed the issue
on; and the replay `workflow:resolving_conflict` runs when a branch has stopped merging cleanly, which is the one
rebase that refresh never drives because it does not own that label. The one-commit rule that makes the exemption
safe is exactly what stops it answering for any of the three — so the same change would be measured past the same
ceiling with a pull request already open over it. Only the owner that RAN a rewrite may describe one: a resolution
an agent wrote over conflicted files, a developer's fix, a reviewer-fix round, the documentation pass, and whatever
an earlier tick left for the conflict stage's ahead-only recovery to find are all commits made by somebody other
than the caller pushing them, and no reading off the branch tells those from a replay. So the conflict rebase writes
down what it is about to replace -- that head, its fork point, and the pull request it is being made against,
recorded before the replay, and the commit it produced, stamped on before the size gate is entered -- and the
recovery is answered from that record or measured, never from a probe. `late_transfer.py` is what it is earned on: a
whole semantic record whose exempt commit is the one the rewrite came from, evidence naming a bounded rewrite kind
from a stage that really makes that kind — the two are one claim, and a `conflict_rebase` offered from `validating`
types in both halves while naming a rewrite that stage does not make — and both pre- and post-rewrite pairs, no
authorization this build cannot read already standing for that exemption, the publication this call itself froze and
the one the issue still records, a provably clean checkout standing on the rewritten commit, a leased head that
peels to a commit this host holds — the one end nothing else here reads as an object, since the lease may name a
different commit from the accepted one — an issue re-read and found *unchanged* — open, carrying no `paused` or
`backlog`, and still on the stage the rewrite recorded, since the entry read that stage off the issue the tick
opened with and a relabel during the rewrite is invisible to every other reading — and canonical fingerprints that
agree. The accepted one is re-taken over the pair the **record** names rather than the pair the caller claims — so
the record proves itself, base included, instead of having its digest read back against an end nothing checked — and
the caller's own claim about what it replaced is held to that same digest, as is the digest any permission already
standing there recorded: carried forward unchecked, a grant would write its own reading over it. Granted, one
durable write records the **permission** and the debt the push is still owed, before anything is pushed — the
exemption itself does not move there. That rotation belongs to `late_rotation.py`, on the write that receipts the
landed push, where the exemption, the identity it carries, and the account of what the remote holds go down together
or not at all, so a verdict is never left on a commit no remote carries; a force-push the remote refuses puts the
branch back onto the head the rewrite found it on — the accepted commit where a squash collapsed it, the leased
anchor where a rebase read it for itself — and what the rollback owes is dropping the permission it will never
spend, since the exemption never moved. Refused, nothing moves and the ordinary cumulative gate measures it like any
other candidate.

A crash between that grant and the push it licenses leaves the one approval that was never a reading this gate
took, and it is read as exactly that: an approval standing beside an outstanding permission defers back to the
permit, which is re-asked in full over the record the grant left — the recovery has no plan behind it, so both
pairs, the publication, and the lease come off the permission itself. Asked of the permission rather than of the
commit it names, because the two go down in one write for one commit and a hand-edited target would otherwise make
the permit invisible and leave the approval looking ordinary. It is also what tells a lost receipt from a moved
remote: a remote already standing on the rewritten commit, while the permission is still outstanding, is this
permit's own push having landed rather than somebody else's move, and remeasuring there would re-adjudicate a
squash the pull request already carries. The recovery republishes as the leased no-op it is, and the receipt behind
it settles the transfer the first tick could not — recorded as `already_published` rather than `pushed`, since which
reading proved the publication attributable is the one fact a local note can never supply. What ENDS the deferral is
that same receipt: past it the permission is spent, and a later approval on the same issue is the ordinary one
again. Until then the deferral holds, and where the permit refuses on the re-ask the ordinary gate measures the
rewrite like any other candidate — the same rule failing conservatively, at the cost of a reading rather than a
decision.

What the settlement turns on is the permit's own answer, carried down the push tail beside the commit it was given
for. The permission on the comment is not a substitute: a refusal is not a hold, so the rewritten commit falls
through to the ordinary cumulative gate and a count under the ceiling pushes the same commit — and a settlement that
read the record alone would rotate a human's verdict onto a rewrite nothing revalidated, under the very digest the
permit declined. A permission this tick's permit did not vouch for is left exactly where it stands: not spent,
because nothing licensed the move; not dropped either, because the remote is now on a head the permit accounts for
and a later tick whose refusal has cleared can settle it in full.

Given that permit, the settlement distinguishes three remotes and settles on two. **Standing where the permit was
granted**, the
leased force-push moves the publication and the receipt behind it carries the verdict over. **Standing on the
rewritten commit already**, the leased no-op proves the pull request still has this permit's own push and the same
receipt settles it. **Moved anywhere else**, the permit is refused before any of that, so nothing is spent and
nothing is reported — a reading nobody could take refuses the same way, and neither is ever read as equivalence.
A publication that went *past* an outstanding permission — some other commit reached the pull request — drops it
instead of spending it, on the rollback's own terms, since the head it was granted against is gone and no later tick
can be granted it. What a settled transfer leaves on both observability streams is one bounded `late_transfer`
record — the issue, the pull request, both pairs, the rewrite kind, and which reading proved the push — and
deliberately no second `late_verdict`, which would read as a second adjudication of work nobody was asked about
twice.

The base-refresh rewrite has one window a squash does not, and its own recovery closes it. The refresh pins a
recovery anchor before git runs and, as soon as git has made one, the replay it produced together with the pull
request and stage it was produced for — so a process lost between the rebase and the grant comes back to a checkout
it can prove is its own work, carrying a replay with *no* permission on the comment at all and nothing for the
re-ask above to be asked over. That recovery
re-derives the same evidence the dead tick would have assembled, from the record's own accepted pair, the head the
checkout stands on, the base the remote names, the pinned anchor as the lease, and the publication that tick
recorded — the last of those because terms taken from the issue as it reads now would compare today with today, and
a relabel or a repoint made while the process was down would be adopted as the dead tick's own. Without any of it
the replay of a change a human already ruled on is measured afresh and routed back into adjudication with the pull
request open over the work. The recovery also owns the settlement where the push landed and only its receipt was
lost, rather than relabelling and leaving it: the permit is scoped to the stage the rewrite was entered from, and
the refresh's own route moves the issue to `workflow:validating`, so a settlement one tick later is refused on the
stage alone. That settlement is taken on the permit ALONE — a refusal parks rather than falling through to the
ordinary reading, which would either report a landed publication with the verdict unmoved or adjudicate the change
a second time — and it is reached only over a permission whose paired debt agrees with it, since the grant writes
both in one statement and a permit re-asked over half of one would rebuild the other from a claim nobody checked.
Every road out of the recovery reconciles the pull request and the stage the attempt recorded against the ones the
issue holds now, because finishing any of them posts a notice, files an audit event, and drops the anchor that is
the only thing bringing the tick back — with the route's own last unmade steps recognized rather than
refused: a finish records the head it has announced before it relabels, so a tick lost between that and the write
that clears the record makes the relabel and the write, and says nothing a second time. The `late_transfer` record
is recoverable across the same kind of window, since the settlement keeps the proof it was made from on the comment
until that record is out. And
the exemption is read before any permission standing beside it -- a permission is a claim about moving one verdict,
so a group damaged after the grant leaves it reading back whole over a verdict nothing can name -- with every term
of a whole permission cross-bound to the attempt in hand, since fields each well-shaped on their own still describe
some other attempt when they disagree.

Every state neither of those covers — a permission this build cannot read, one naming some other commit, a receipt
nobody wrote, a debt nothing paid, a tree carrying uncommitted changes, a remote
somebody moved — settles nothing and parks fail-closed rather than finishing the route, because the route clears
the recovery anchor and the anchor is the only thing that brings the tick back to a verdict that may not have moved
([`../state-machine/labels-and-state.md#base-refresh`](../state-machine/labels-and-state.md#base-refresh)).

The approval holds the switch back for the commit it *names* and no other, which is why the switch is asked twice —
once at the door, cheaply, and once past the proof. An approval is a claim about one object id, and nothing can say
whether the head is that id until the head is proved; past that proof and not it, the approval describes work this
branch has moved past and the candidate in hand is new work, which is exactly what the switch keeps out. Asked only
at the door, a stale approval would drag a resumed developer's fresh commit into a gate the operator switched off and
route an oversized one to adjudication. The stale record goes with it: a publication naming a different commit drops
the approval, because a debt recorded for a commit nothing is going to push freezes the branch out of the base
refresh for the rest of the issue's life and parks every later tick asking for it back.

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
decision. Whether it fits is measured on the whole comment the write would produce — the preserved held-PR body and
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
identities — so the identities and both SHAs are proved before a pull request is touched or an agent is started. That
includes the generation naming THIS issue, which a positive `late_current_issue` on its own does not say. A candidate
whose base was never recorded would otherwise produce a `git diff` against nothing and a record two sinks refuse
afterwards, with the run already paid for; one carrying somebody else's number would show the agent a prompt naming two
issues and file the verdict against the one it names. Either parks instead, saying which field is wrong.

Before any of that runs, the reusable open pull request the candidate stands on is put under a cycle-marked hold
(`late_hold.py`). Which one that is, the owner decides from the record and in one order. A hold this generation
already took names its own, and outranks the rest: the preserved body beside that identity is the only copy of a
description there is, so a tick that went looking for a target instead could take a second hold and overwrite it —
which is exactly what an issue re-pointed at another change would otherwise arrange. It outranks them only up to a
record that has **moved**, though. A generation re-measured past its own push is adjudicating the change on the
published pull request, and a "do not merge" left standing on the plan one marks nothing while the change a human
could merge carries no notice at all — so a hold whose publication entry names a different pull request is settled
first: the old one released, the slot freed, the new one taken, never both at once, and a release that could not be
made on a reusable pull request parks instead of spawning. Failing that, a generation carrying a publication entry
names the implementation pull request the work is already on: the gate proved it open and froze it there, so nothing
is looked up and no provenance is derived. Failing both, there is only `pr_number` — whichever pull request the issue
currently records, which is an implementation as often as a plan — so the hold reads the discussion provenance through
the implementing stage's own `_recorded_pr_is_the_plan`, about the one snapshot it read, since past the handoff a plan
is told from an implementation by the commit its head is on and two reads would leave a window for a human push. That
snapshot is read whole where the fetch is guarded, because a PyGithub pull request is lazy and the request that can
fail is the first attribute access rather than the fetch itself; anything unreadable parks rather than escaping. An
implementation PR that no entry says the candidate was measured against is left alone — rewriting its description
would replace a human's account of a change under review with a notice about a different one. A provenance that could
not be established is not the same answer and fails closed. Past that gate, the identity, the head the pull request is
standing on, and the original body are written to pinned state in one write BEFORE the pull request is edited, so a
crash can lose the edit — which the next tick re-applies, since every branch is idempotent — but never the only copy
of the description. Two of three is not that record: a head no reading could name refuses the hold while nothing has
been touched, because the write drops an empty one and a notice already on somebody's change would be left with
nothing on the issue saying which change it was written over. The head is a reading rather than a claim: it says which
change wore the notice, so a pull request somebody pushes to while the adjudication runs is reported and left wearing
the same notice against the same recorded reading, and the `late_published_sha` a settlement pins its push to is never
restamped from it.
What the retry re-applies over is decided by the WHOLE body, not by the hidden marker inside it: exactly two bodies
are this issue's to replace — the hold it wrote, **verbatim**, and the description recorded beside the identity, which
is what a crash between the persist and the edit leaves and what the first application starts from. Anything else is a
human writing over the notice, the marker they happened to leave in place included: a sentence changed inside the hold
is their edit as surely as a wholesale rewrite is, and calling that held would have the release put the preserved copy
back over their words a step later. That comparison is only affordable because the hold body is exactly
reconstructible, which is why the marker is scoped to the **cycle** and the notice quotes nothing that moves inside
one — the generation counter advances on every reconciliation that lands, so a body keyed to it would leave every
re-measured candidate wearing a notice its own record no longer recognized, and the measurement belongs on the issue
thread, where each reading is announced anyway. What the notice *says* is decided by the side of publication and by
nothing else: a pull request nothing has pushed to is being adjudicated before anything is published, while the one
the work is already **on** was published long before the hold — so telling its author their change is held "before
anything is published" would describe a change that is not theirs, and what that notice names instead is the push the
adjudication stands in front of. Being reconstructible is a property of a *spelling*, and a hold outlives the binary
that wrote it, so the spelling before this one — marked by generation as well as cycle, and quoting the measurement —
is kept as something to **recognize** and never to write. All three are recognized and only ever one is written: a
body found in the older spelling is ours, and so is one found in the side of publication the record has since crossed,
which a developer resumed on guidance and re-measured past their own push is exactly how a cycle reaches. The same
edit that would have applied a fresh hold rewrites either in the current spelling, so every later comparison has one
answer to make. A hold a binary cannot reconstruct is a "do not merge" notice nothing can ever take back off, on a
pull request nothing can start an agent under. Leaving it alone is
not the same as being *held*, though, and the answer says so: an open pull request whose notice a human removed is a
change they can merge with nothing on it saying an adjudication is running, so the reconciliation reports it
**displaced** and no late decomposer is started under it. An answer already recorded may still be settled — settling
releases a hold that is already gone, and only a NEW run would leave a human free to merge under one. A write
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
everything a completed run leads to — publishing an accepted candidate, taking a snapshot, superseding the pull
request its work is on, activating children, even announcing a question — lands on an issue somebody may have closed
in between. So
after **every** completed late run, the adjudicator's and the developer revision's alike, the result is persisted and
then the owner is read again, once, before any of that happens (`late_owner.py`).

Every completion, not only the ones that decided something. A `question`, a timeout, an unusable reply, a candidate
the adjudicator moved, an outcome too large to record, and a developer reconciliation that could not be made are all
runs the issue paid for, and a closure during any of them strands the same generation and the same hold as a
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
size gate, the hold, and any spawn. The retry costs no agent: the run has already been paid for and its
result is already recorded, so the recorded answer is what settles the candidate once the read succeeds.

It is written **before** the read rather than derived from its failure, because a read that fails is not the only one
that does not come back: a process killed mid-read would leave nothing at all behind, on exactly the two routes above
that carry the next tick past the point a retry could hang off.

What the claim does *not* do is move the phase backwards, and neither does anything else above the transaction.
A boundary before the split — `measuring`, `holding_plan_pr`, `adjudicating`, `owner_check` — is never written over
`snapshotting`, `splitting`, or `superseding`, and the record refuses that move itself rather than each writer
remembering to: a transaction re-entered after a crash comes back through the *whole* coordinator, so the hold it
reconciles before anything spawns, the spawn that names its own boundary, and the claim each completion
writes would each erase it in turn. The window that matters most has nothing recorded at all — a child is created
before the write that records it — so the phase is the only thing left that says a loop was in flight, and a
cancellation observed there keeps exactly what it found. The guard reads a kept boundary back as the standing claim
it is, so a re-entered transaction pays for no second write of the claim it already made.

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
question, a content-drift hold, a stalled revision, or a spent spawn budget it is unbounded, since those parks *are*
what the issue is waiting on. So the sentence is written down beside the flag (`late_park_notice`) and dropped only by
the post that discharges it: a park whose sentence is still owed never counts as a repeat, is re-said at the top of
the next eligible tick — ahead of every gate a parked issue routes past — and, on the guard's own park, rides out on
whichever answer the owed read gets. A notice too long for the pinned comment is refused whole and loudly, since a
record that broke the write would take the park it explains down with it.

## What a verdict the read cleared earns

A **question** earns exactly one thing: the announcement. It is posted here rather than where the question was
recorded, and that is the whole reason it moved — the record has to go out before anything is said and the owner read
has to go out between them, so a question is not posted to a thread somebody closed while the agent was still
answering it.

A **split** is handed on rather than acted on. Creating the children, taking the snapshot they are cut from, and
superseding the pull request its work is on are one transaction; what the guard owes it is the guarantee it cannot
check for itself, that the outcome was re-checked against an owner read taken after the agent finished. Nothing is
created here. What that transaction then does is [the section below](#what-a-cleared-split-actually-does).

A **single** is reconciled as an EXEMPTION for the measured commit (`late_settlement.py`, which owns the order the
steps below run in and writes the exemption itself). The candidate is already committed in the developer's own
worktree, and the ordinary implementing publication is what pushes it and hands it to review — so what this step
owes is a durable record that this exact commit has been adjudicated, or the gate would measure the same candidate
past the same ceiling and adjudicate it again forever. `late_exempt_sha` names the measured commit and only it, which
is the whole invalidation rule: work committed on top of an accepted candidate is work nobody adjudicated, and it is
measured as the fresh candidate it is
([`../state-machine/labels-and-state.md#late-generation-state`][late-state]).

Beside that commit goes the **identity of what it contributes**: the generation's frozen base, the accepted
candidate, the canonical digest of the contribution between them, and the version of the scheme that digest was taken
under. It is derived from the frozen pair the decomposer inspected and from nothing else — never from the checkout's
head or a base read now, since the worktree is writable for the whole of an adjudication — and it goes down in the
same write as the exemption, because the retirement a few steps later takes that pair off the record. The exemption
says which COMMIT a human ruled on; the identity says which CHANGE they ruled on, which is the only question left
once the commit itself has been rebased, squashed, or made afresh. What lets a candidate publish unmeasured is
still the exact-SHA comparison and only that; what the identity licenses is the exemption MOVING onto the commit an
equivalent workflow rewrite produced, which `late_transfer.py` grants and records as its own pinned authorization
before the push it licenses — and which `late_rotation.py` then carries over on the write that receipts that push,
so the verdict never names a commit no remote has
([the size gate's own section](#the-size-gate-a-committed-candidate-passes)).
Where the reading could not be taken, or where any field of the record cannot be
vouched for, there is simply no transferable identity, the exact-SHA exemption stands untouched, and the rewrite is
measured as the fresh candidate it looks like. The record belongs to the
commit on the exemption field, so moving that field to another commit drops it: a later verdict that records the
commit alone writes nothing over those fields, and left there they would match the first commit by name the next
time one put it back.

Beside the exemption goes the **exact-commit reconciliation** of the pull request the issue records
(`late_reconcile.py`, with the hold release beside it and the head a published-side verdict is proved against one
owner over in `late_proof.py`), and it is the half the ordinary publication cannot do: that one searches for an OPEN
pull request on the branch, while `pr_number` by this point may name a plan PR a human merged, or the commit may
already be sitting on a pull request a crashed publication opened and never recorded. Neither is cosmetic.
`implementing` asks its recorded pull request first, and
a **merged** one that is no longer the plan ends the issue as `done` — with the adjudicated candidate never
published; and a commit already on a pull request nobody records is published a second time, since the reuse looks
for an open one and finds none. So the commit is what the pull request is found by, in any state
(`find_pr_for_commit`): one that carries it is recorded whatever state it is in, and when nothing carries it the
recorded number is kept only while it is still open — a settled one is dropped rather than handed on. A lookup, or a
recorded pull request, that could not be read parks (`late_pr_unreconciled`) rather than publishing on an answer
nobody gave.

Two things the reconciliation deliberately does not do. It creates **no snapshot** — a snapshot exists so children
can be cut from a candidate about to be superseded, and an accepted candidate supersedes nothing, so preserving a
copy of it would record an obligation with nothing on the other end. And it does not rewrite the held pull request:
the
description this generation replaced is restored over the hold text, and what happens to that pull request afterwards
is the ordinary reconciliation's — the publication that follows reuses it and rewrites its body when the push lands
on it, and leaves it alone when it does not. Only a body that IS this cycle's hold, verbatim, in either spelling
this orchestrator can reconstruct, is restored — so a description a human rewrote, or edited a sentence of, marker
and all, while the hold stood stays theirs, and a settled pull request still wearing an older binary's hold is still
put back. That settled case is the one release the retry above cannot have migrated first: a pull request nobody can
merge is left exactly as it is by the reconciliation, so what the release meets there is whichever spelling wrote it.

What a failed release may *stop* is narrower than what a failed hold stops, and for the reason the hold exists: the
danger is a change a human can still merge while it wears a notice saying not to, which is a property of an **open**
pull request. So only a reusable one parks the candidate (`late_plan_pr_hold_failed`), with the generation untouched,
which is what makes that retry free. One a human has already merged or closed is tidied where the edit lands and
stepped over where it does not — refusing to publish an adjudicated candidate over the description of a settled pull
request would be a permanent block bought for nothing, and the ordinary exact-commit reconciliation is what the
candidate goes on to.

The order is chosen so every window a crash can land in is one the next tick repairs. The hold is released first,
while nothing else has moved; the exemption and the identity beside it are written next, with the generation still
live behind them; only then is `workflow:implementing` handed back; and only after that is the generation retired
(`late_handback.py` owns that half) — behind the one comment naming the accepted commit and the measurement it was
judged on, posted immediately before the write that drops the generation,
so a crash between them costs at most a repeated comment. What that write keeps is the two external ledgers: an
obligation the remote is owed does not stop being owed because the adjudication that recorded it ended well. A
`decomposing` issue with no generation on it is one the INITIAL decomposer would pick up and re-decompose, and an
`implementing` issue with a live generation is one the relabel guard puts back and the next tick re-settles — so the
ordering is what keeps the first of those from ever existing.

## What a cleared split actually does

`late_transaction.py` is the order, and the order is the contract: every step is preceded by the durable fact that
lets the next tick tell "already done" from "never started", and every step is idempotent where that fact turns out
to be ambiguous. Four refusals come first, because no step below could repair any of them. A lineage already at
`MAX_LINEAGE_DEPTH` creates nothing (the bound is enforced where the children would be born as well as where the
reply was parsed). A recorded **ancestry that disagrees** with the generation's lineage creates nothing either, and
that is the same bound read from the other side: a child of an earlier split carries the lineage it was created
under, its own generation is minted from that record, and a generation naming a shallower depth or a different root
is one minted without it — which is exactly how a lineage would buy itself a generation past the cap. An
obligation ledger holding an entry this binary cannot type stops the whole transaction, since a split records a
snapshot and one consumer per child on exactly that ledger and merging into one written back verbatim would drop
whatever it did not understand. And a manifest whose declared scope carries one of this orchestrator's own receipt
markers is refused here rather than where the child body is built, because it is a fact about the manifest rather
than about one slice and because this is the last point at which refusing costs a new commit instead of a human —
the children step below says what that receipt would otherwise let a lookup adopt.

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

**The owner, again, before every step the remote keeps.** Past the snapshot the transaction re-reads the owner
before each child it creates and before each step of the publication behind them — because a ref is an object a later
pass can reclaim, and a child is a real issue somebody will work, which nothing here ever takes back. It asks
repeatedly rather than once because the steps are not one moment: a push and a fetch stand between the verdict and
the first child, a create, a record, and a seed stand between every child and the next, and publication is three
GitHub round-trips of its own — the announcement, the supersession, and the retirement that hands the parent to
`workflow:umbrella` and lets its children run. Reading once for all three would let a close observed during the
announcement or the supersession still put an agent on somebody's repository.

Who else could see a close is what makes asking necessary. A poll that observes one while a worker holds the issue
can hand it to nobody — the scheduler admits no second worker for an issue one is already running — so the reading
is latched process-wide (`workflow/engine/observations.py`) and swept a tick later.

The run in flight reads that latch too, and it is the first thing the owner read asks — ahead of GitHub, because it
holds the one reading GitHub cannot give back: a close and a reopen that both happened inside one of this run's own
steps leaves the issue reporting `open`, and only the poll ever saw otherwise. So a latched close ends the cycle
wherever the run stands.

Every step nothing takes back is asked past it, and each barrier covers a window of *remote work* the poll runs
beside. **Every child** including the first, because the write that forces the parent to be an umbrella stands ahead
of the first create — once more inside the create itself, since the orphan lookup before it walks the whole
repository on a resumed pass — once behind it, and once between the read of the child's own pinned comment and the
write that adds to it: the create is a request too, and so is that read, and what a close landing inside either
leaves is a real GitHub issue, recorded either way and written to never.
**Each publication step.** **Every relabel** the activation walk makes, since a close latched after the first child
was released must not release the second — asked on *both* sides of the publication licence beside it, since that
licence is a lookup and a close observed inside it would reach nothing else before the relabel landed — and the
transaction asks again behind the walk, because the walk holds children but does not own this issue's record.
**The spawn**, asked last of all, because a worktree probe, a retry-budget write and a hold to reconcile stand
between a tick's own gates and the one step that puts an agent on somebody's repository. **Both sides of a
developer revision**, which is the same step under another name, with the poisoned-session retry inside the shared
resume guarded alongside it. **Each step of the `single` publication**,
which is where the barriers protect the record rather than an effect: the last write drops the generation entirely,
and both the sweep and a receipt adopted from the thread read that generation to decide there is anything to end —
so past that write the answer is a *reinstatement* rather than a refusal, from the generation still in the call's
own memory. **Every obligation a reclamation settles**, between every two of the receipts a reclaimed ref owes its
children — each is a comment on somebody *else's* issue — between the fresh consumer proof and the ref delete it
authorizes — a ref that is gone while the record still reads live is a reclamation nothing afterwards can attribute
to the cancellation that earned it — and again between that delete and the receipts it
authorizes, since those receipts are the one cleanup effect that writes to somebody *else's* issue and a cancelled
cycle owes its children nothing — and once more inside each of those receipts, since proving a child untold is a
request of its own and the comment it authorizes stands behind it. And **the umbrella's own walk**, past its child
scan, behind the settlement its terminal waits on, and immediately before the write that records the resolution,
since everything after those acts — it reclaims a remote, hands the issue `done`, or releases a child, and `done`
takes the issue off every label the closed-owner sweep queries and closes it. What makes that label safe is the
write ahead of it: one pinned write that stamps the resolution and **retires the cycle** together, so a close
arriving past it finds nothing left to cancel. One landing *inside* it is answered behind it, from the generation
still in the call's own memory — a reinstatement rather than a refusal, exactly as the `single` publication's own
retirement and the size gate's drop of a small candidate both take one: the cycle goes back cancelled, no terminal is
written, and the owner keeps `workflow:umbrella`, where the ending is reached. Every one of those retirements reads
that answer off the *window* rather than asking the latch, and the window decides it as it closes, under the lock that
closes it — a barrier taken any earlier leaves an interval in which a poll can still latch a close and receipt it
against the cycle the window is advertising, and the worker would pass on having seen neither. That barrier belongs to
the process that made the write, so the write records which cycle it dropped: a process that dies before reaching it
leaves that correlation and the receipt on the thread, and the closed-owner sweep adopts the two together rather than
finishing a terminal over a close somebody already observed.

The same is true one layer up, of a close **no poll ever saw**: an issue open when the enumeration listed it and
closed by the time its pass refetches it carries a reading that exists only on the object the refetch returned. Both
paths that refetch take it there — the sequential loop and the worker — and hold it across the pass, so a mark that
could not be written leaves the reading for the next tick instead of losing it to a reopen.

The barriers past a claim-bearing read take the latch ALONE: a claim names `owner_check`, and writing it over the
boundary a tick actually reached is the rewind the record refuses.

A cancelled cycle is refused under every label it can be wearing, and `rejected` is written wherever the transition
graph declares the edge — plus `ready` and `blocked`, which the cycle's own decomposer writes as its ordinary
outcome and which neither the graph nor the closed sweep would ever bring a tick back to — never from the unlabeled
state, which is the restart handshake itself, and never past a `backlog` / `paused` that defers the external half of
the ending while the mark still goes down.

A latch is memory, so the poll that takes one also leaves a cycle-scoped receipt on the issue thread — a comment,
because the pinned comment is written whole and the worker holding the issue owns it. A post GitHub refuses is
retried by the next poll, since an observation with no durable half is one a restart takes away entirely. After a
restart the dispatcher's cancelled-cycle guard scans for that receipt once per owner per process, adopts it, and runs
the ending from the mark ([state-machine/delivery-stages.md](../state-machine/delivery-stages.md)).

The latch is also held past a cleanup pass that RETURNED without finishing the ending — a ref a live consumer keeps,
a delete the remote refused, a terminal GitHub declined — but only where nothing else would come back: an owner
still wearing one of the four swept labels is one the sweep reaches on its own cadence, and holding a reading over
it would cost a pass per tick for as long as the ending is owed. Two of those four are queried for this reason
alone: a decomposition outcome writes `ready` or `blocked` and can land after the close its owner was observed on,
so an ending left there has to be discoverable without the latch, which dies with the process. And because it does,
a sweep that leaves an obligation owed under a label outside all four puts a queried one back.

It is the *same* guard the handoff took, entered again, so the three answers are unchanged: a closed owner is marked
cancelled and the cycle ends where it stands, with everything already on the remote left on the ledger for the
cleanup path; an unreadable one parks with the read owed on the record, claimed under whatever boundary the
transaction reached rather than over it; and only an open one lets the next step run. A loop stopped mid-flight
leaves a partial split, which is a state the count and the register already describe — the ref is retained, because
a real child exists that the consumer ledger is short of.

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
about to create looks for that marker before opening anything. The issue is in the marker because a cycle identity
is minted per issue and repeats across them — two parents adjudicating their first candidate are both cycle 1 — and
the lookup is not scoped to one parent's children, so without it one parent would adopt, reseed, and activate
another's.

That lookup is a walk over the repository's issues in **every state and under no label**, which is the expensive
reading and the only correct one: in the window it exists for nobody has attributed the child, so a human is free to
close it as junk or move its label, and a search bounded to open issues on the label it was born with would miss
exactly those and open a second issue beside the one they had just acted on. It is taken only on a **resumed** pass
— one where an expected-children count already stood on the parent before this pass wrote its own — so a first
split, which has nothing to find, pays nothing for it. An enumeration nobody could take raises and parks, because
"could not ask" read as "there is no orphan" is what duplicates. And a candidate a human has since closed or moved
off the child label is *refused* rather than adopted: reopening or re-labelling it would undo a deliberate act on an
issue this orchestrator had not even attributed yet, so the transaction parks and lets them say which they meant.

The receipt has to be **unambiguous**, and that is a second rule, not the same one. The lookup matches a marker as a
substring of a body — all a body search can do — and the body carries the adjudication's own words above the marker
this transaction stamps in. An issue whose body holds two child receipts answers the search for either slice, so
adopting on the strength of the match alone records one issue as two children of the same split: named twice in the
parent's register, once in the consumer ledger, and seeded with whichever scope came last. So the manifest is refused
outright when any declared title or body carries one of this orchestrator's receipt markers — every one of them
starts with the same `<!--orchestrator-` prefix — and that refusal is taken *before* the snapshot is pushed, where
it costs a new commit rather than a human: a generation holding a snapshot may no longer be revised. A candidate the
lookup does return is checked for carrying exactly one child receipt, so an issue an older binary created or a human
edited is stranded for a human rather than adopted for a slice it may not belong to.

Each child is born knowing what it needs and nothing more: its declared scope in the words the adjudication used, the
current base branch, the ancestor snapshot ref and exact commit, and the lineage and cycle identity a later record is
correlated by — written to the child's own pinned state as the `late_ancestry_*` group
([`../state-machine/labels-and-state.md#late-generation-state`][late-state]) and read fail-closed like every other
late field. Its body says how the work may be reused: **cherry-pick a coherent commit**, or **copy selected paths**,
and never split hunks mechanically to make the change smaller. File and hunk boundaries do not express issue scope, so
a change partitioned along them is one nobody can build or review — the judgment about what belongs to a slice stays
with the developer who implements it. The seed is re-applied on a resume by reading the child's state and adding to
it, never by writing a fresh record: by the time a retry reaches a child that was already created, that child may be
implementing. The write that first *attributes* a child also takes back the park it may have collected in the
meantime — poll order is the repository's, not the transaction's, so an orphan can reach the stage machine before
anything records it and be parked as an unattributed `blocked` issue. Leaving that standing would activate a child
that then waits for a reply nobody owes it. A child that already records a parent keeps whatever park it has: that
one is its own.

**Only then the links and the supersession.** The parent says what it became and where its work went, exactly once,
and both halves of "once" are needed. The generation's own `late_links_announced` flag is the cheap gate and the one
that holds on the ordinary retry — it is scoped to this adjudication, unlike `decomposed_at`, which an **earlier**
decomposition of the same issue already wrote and which would therefore suppress the announcement entirely. The
thread is the gate that covers what a flag cannot: a comment that landed and a process that died before the write
are indistinguishable from the outside, so the marker this generation stamps into its own sentence is looked for
among the issue's comments before another is posted. That search is asked only when the flag is unset, so a resume
past the announcement costs nothing.

The pull request this cycle's work is on then gets superseded through the new `supersede_pr` helper: one marked notice
linking forward to the umbrella, every child, the snapshot ref, and the exact commit, and a close if it is still open.
Which pull request that is is decided by the side of publication the generation was entered on, and by the **entry**
rather than by the hold beside it — both can name the same pull request, since a generation entered past the first
push holds the one the work is already on. A generation entered BEFORE the first push has only the hold's record to go
on, and that names the plan PR this cycle marked. One entered PAST it names the implementation pull request the
candidate was measured on — and that one is the sharper of the two, because the tail behind this step deletes the
branch and hands the work to children: left unsuperseded it is an open change carrying work nobody will finish,
pointing at a branch that is gone. It is PROVED before it is closed, on the settlement's own reading and for the same
reason — the entry the gate froze names it and the head it was standing on, and neither can be re-derived — so a pull
request nothing could read, one a human merged or closed while the adjudication was open, and one somebody pushed to
are each a park with the children durable, the pull request left where its human put it, and a retry that settles the
same recorded verdict once the disagreement is reconciled. One already CLOSED over this adjudication's own receipt is
none of those: the retirement behind this step is not the last, so a tick that closed the pull request and died before
it comes back to a `closed` reading it cannot tell from a human's without the thread -- read as a settlement it would
park for good with the children blocked behind a supersession already made. What the receipt answers is the STATE and
only that: the head is proved on that path exactly as on the open one, because a close does not freeze the branch
behind it — somebody pushing between the crash and the retry leaves a commit the snapshot does not hold, and waved
through the retry would settle the split, activate the children, and reclaim that branch. A MERGED one stays a refusal
whatever the thread says, since a human who reopened and landed the work decided the opposite of what the supersession
claims; a reopened one reads `open`, so the same head proof runs and the close is made again with the receipt keeping
the notice from repeating. Every one of those refusals parks under a notice naming the disagreement rather than the
write-failure sentence, since a supersession this transaction already made is not one that could not be made.
Idempotent through the **thread** rather than through a receipt, since the comment and the record of it cannot be made
one operation. Both markers are scoped to the exact adjudication — the pull request outlives a cycle and the issue
thread outlives everything, so an unscoped one would read an earlier episode's receipt as this one's — and both are
honored only on a comment **this orchestrator authored**, since an HTML comment is invisible in the rendered thread
and anybody could otherwise post the marker to suppress the sentence it gates. A merged or closed pull request is told
and left alone; one that could not be read, or a release that failed on a still-open one, parks — and nothing is
activated while a pull request carrying the superseded work is still open. That last part is why the supersession runs
on **every** pass, including one whose ledger already reads `reconciled`: that entry records what an earlier pass did,
and a human who reopens the pull request between the write and the resume would otherwise have the resume skip
straight past, report settled, and let the children loose beside a change still carrying the superseded work.
Re-asking costs one fetch and one comment listing, and neither step repeats anything.

On the published road the pull request is asked about **between every step of the tail, on the same rule the owner
is** — in front of the close, in front of the retirement, in front of **every** child released, and immediately in
front of the branch delete. The owner is not the only thing a human moves mid-pass: every one of those steps is
licensed by the supersession being on that pull request, and a merge, a reopen, or a push takes the licence away. So
no step is run on evidence a step before it took — and "before it" includes a reading the step itself spent. The
child scan a release is decided on is a request per child and each relabel is another, so the ask lives inside the
activation walk rather than in front of it; the reclamation may spend a read-only probe deciding whether an ordered
snapshot ref is already gone, so its ask sits immediately before the delete rather than where the work list is
assembled. It is that road's alone: a generation entered before publication froze no head to prove a plan PR
against, and what that pull request carries is a design document rather than the superseded work.

The barrier in front of the close is why the close is made against a **second** reading. The receipt the first
carries costs a comment listing, and that listing is a round-trip standing between the state and head read beside it
and the write those two license. Asked again with no listing behind them, they are the last thing to reach GitHub
before the write — so a change a human merged, closed, or pushed to inside that window is left **untouched**: no
notice posted onto it, no close, and a park. Discovering it one step later would mean marking and closing a change
nobody adjudicated and only then refusing to finish. A pull request already closed over this adjudication's own
receipt writes nothing at all, since `supersede_pr` would post no second notice and close nothing already closed.

The receipt that second reading skipped is then **handed to the write** rather than looked up again. `supersede_pr`
searches the thread for its own marker before posting, and that search is one more request in exactly the interval
this barrier exists to empty: a human closing the change inside it would have the notice land on their settlement and
the helper report success. Nothing else can move that answer — the marker counts only on a comment of *ours*, and
this pass has posted none since it looked — so the answer travels with the call and the close is the first thing it
sends.

What a refusal costs depends on which side of the retirement it lands. **Before** it the pass parks, with the
disagreement named and the obligation written back as owed: the issue stays on `workflow:decomposing`, the children
stay `blocked`, the branch stays intact, and the next tick supersedes the same pull request again. **Past** it there
is no live adjudication left to park, so the last two barriers decline the step in front of them and leave it to the
retry that already owns it: the umbrella's own walk for the children, its terminal for the branch. That is why the
pre-retirement barrier is the very last thing before that write, with the branch resolved ahead of it rather than
between: the window it cannot close is the write, and a reopen inside it costs a label that lands rather than a child
that runs or a branch that goes.

Declining a step is only safe because those retries ask the same question themselves, and that is what the
retirement **keeps the publication group for**. It is the only thing left on the issue naming which pull request the
split closed and the head it was closed over, so the shared activation walk re-asks it in front of every relabel it
makes — under `workflow:umbrella` and under `workflow:blocked` alike, since the walk reads the parent's own record
rather than being told — `late_cleanup` re-asks it in front of every branch it deletes, and the settlement the
umbrella's terminal waits on asks it once more before `done` may be written at all. Dropped there, a reopened pull
request would have its work handed to children on the very next poll and the ref it points at reaped by the
terminal, with nothing in between having looked. It costs one lookup per release, one per delete, and one per
terminal decision, and a parent that never entered the size gate answers without a request at all.

Keeping the group has one cost, and it is paid a layer above: a whole publication group with no count beside it is
also the shape a tick that died between the freeze and the diff leaves, so the reconciliation the dispatcher runs
ahead of every handler asks the record's own settlement before it reads that shape — a `late_phase` past the ref the
transaction cuts, or a non-empty `late_split_children`. Without that question the group would name the stage the
gate was entered from while the issue wears `workflow:umbrella`, every poll would be held for a human as a reading
read off a stage the issue has left, and the walk this section is about would never run
([`../state-machine/delivery-stages.md#the-size-gate…`][size-gate]).

That last ask is the one no ledger could make, and it happens **twice**. A reclamation that **finished** owes
nothing, so a human who restores the branch and reopens the change afterwards leaves every entry settled and the
terminal free — and what it writes is `done`, a close, and the drop of the publication group, after which nothing
would ever ask again. The answer is deliberately not written back as an obligation: nothing *is* owed the remote,
and an entry claiming otherwise would send a later pass to delete a branch a human put back on purpose. The label
staying put is the whole of the response.

Once before anything is **said**, where refusing costs nothing, and once immediately in front of the **retirement
write**, which is the boundary itself. Between the two stands the resolution comment and the latch checks beside it,
each a request a reopen can land inside — and a refusal there costs a sentence that has already gone out. So that
sentence carries a marker and is gated on the **thread** as well as on the stamp: the stamp is what a resumed
terminal has past the write, and the thread is what covers the window the stamp cannot, so an umbrella held on a
reopened pull request does not repeat itself on every poll for as long as a human takes to settle it. A refusal at
that second barrier writes nothing at all, so the record the next tick reads is exactly what this one found.

Both halves of that receipt are scoped deliberately. It names the **cycle and generation**, because an operator
restarting a rejected cycle keeps the thread — a marker naming only the issue would silence the sentence the cycle
after it owes its humans. And it is stamped **only on an umbrella a post-publication split made**: nothing refuses
the others past their sentence, so the initial decomposer's umbrellas and plan-PR splits keep the stamp as their
sole gate, spend no comment listing per completion, and carry no receipt anything would read.

**Then the label, the retirement, and the activation, in that order.** The generation is retired in the same write
that hands the issue to `workflow:umbrella`: identity, both commits, both ledgers, and the publication group kept,
the measurement dropped,
and the recorded `pr_number` cleared. Dropping the measurement is what makes the label stick — a parent that has
become an umbrella has no candidate to measure, and a record still answering "oversized" is exactly what pins
`workflow:decomposing` and would have the relabel guard put the umbrella label back every tick. Activation runs after
that write for the reason the initial split's does: a crash between them must not leave a runnable child under a
parent still labelled `decomposing`, and a child this pass could not flip is picked up by the umbrella's own walk as
the retry.

It runs *through* that walk rather than through the initial split's one-shot flip, and the difference is the
supersession above it: that step can park for as long as a human takes to settle a pull request, so by the time
activation runs a child may have reached `rejected` or `done` on its own. A write that read nothing would put it
back to `ready`, and the transition guard only warns by default. So each child is read fresh and only the ones still
`blocked` with their recorded dependencies satisfied are moved; a read that failed leaves every child where it is,
since the umbrella takes the same reading on its next tick.

A child GitHub reports as closed is passed over there too, and that one is not specific to the late split: closing an
issue leaves its label untouched, so a child a human ended while it was still `blocked` goes on looking startable to
every walk that reads labels alone. The close is read the way GitHub spells it — `state`, the only spelling a real
issue carries — which is also how the reclamation counts a direct consumer that ended without a terminal label.

**Cleanup last, and never in the way.** The superseded branch is written to the ledger as `pending` in that same
retirement write and attempted *after* activation: an attempt that does not finish records `failed`, emits
`branch_cleanup_failed`, and holds no child back. Children waiting on a branch deletion would be work stalled on
tidiness.

"The branch" is every surface it exists on — the remote ref, the checkout holding it, and the local ref — and the
entry reads `reconciled` only once all three are provably gone. A remote delete that succeeded beside a worktree
that would not come down is not settled: what is left is a checkout on a superseded branch that the per-tick base
refresh treats as a pre-PR tree and goes on merging into. The two halves are attempted independently and the entry
is what both said, because a remote that refuses is a permission or ruleset problem only a human can clear — a local
teardown conditioned on it would leave that checkout accreting merges for as long as the refusal lasts, and the
local half needs nothing from the remote to succeed. The local half is *verified* rather than trusted, because
`git worktree remove` and `git branch -D` are best-effort helpers that report nothing — so the entry is decided by a
read taken afterwards, and that read fails closed. Taking the checkout down at all is safe here for one reason: the
snapshot was created and proved before any of this, so the commit it holds is no longer the only copy.

Only a branch this issue is actually published under is ever deleted. The target comes off a ledger a human can edit
and is spent on a destructive call, so it has to *be* one of the two names this spec gives this issue — the
slug-namespaced form and the legacy flat one — before the remote is touched. A namespace-and-tail reading is not
enough: `orchestrator/other-repository/issue-41` is inside `orchestrator/` and ends in this issue's tail while
belonging to another repository entirely, and two specs sharing one `target_root` is the case slug-namespacing
exists for. The number is taken from the issue being walked rather than from the record, so a hand-edited identity
cannot aim the delete either. Anything that does not match is recorded `failed` and left for a human, which is the
one answer that neither deletes somebody's branch nor quietly forgets the obligation.

What the obligation **does** block is the umbrella's own terminal completion, and the retry lives there rather than
in the transaction — an issue that has become an umbrella never reaches the transaction again, so nothing else would
bring a tick back to it. `late_cleanup.py` is asked at the one boundary where an unsettled obligation still matters:
every umbrella tick that finds every child resolved settles whatever is still owed, and the parent closes only once
nothing is. A refusal keeps the label, which *is* the retry, and leaves the parent visibly open instead of closed
over a remote nobody will ever reap.

That boundary is also the first at which the **snapshot** can go, and under the rule that owns it: a ref may be
deleted only once every recorded direct consumer has **ended**, and all-children-resolved is exactly when that
becomes true for the consumers this split created. Ended is read off the consumer's own closed state and explicitly
not off its label: all three dispositions that end a child close the issue and none of them survives a reopen, while
a label does — a child reopened while still wearing `done` is live again, and a reading taken off the label would
delete the only copy of the work it came back for. The issues come off the child scan the umbrella already took, so
proving it costs no request of its own, and a closed `done` covers a nested split too — a child that reached it has
published, so its own descendants are past needing the ancestor. Anything that cannot be proved keeps the ref: a
consumer missing from the scan, one whose read failed, or a consumer ledger this binary could not type. All of that
is about the consumers the ledger *names*, so the prior question is whether it names all of them, and the record's
own phase answers it. A child is created and then recorded in two writes — it must be, since a child on GitHub the
parent does not record is a child nothing would come back to — so while `splitting` stands the list may be short by
one that already exists, and its length decides nothing: a set of ended consumers says as little about the child it
has not reached as an empty one does. Nothing on the ref is reclaimed in that window. Either side of it the list is
whole — before the split nothing has been created, and past it the loop ran to the end — which is also what makes an
*empty* list a fact rather than a gap, since the ref is retained ahead of the first child.

The boundary an interrupted transaction stood at is therefore **kept**, because a phase is otherwise written only
forwards. Every completed run claims the owner read, and a transaction re-entered after a crash comes back through
one — so that claim never writes over `snapshotting`, `splitting`, or `superseding`, and a re-entered split carries
its claim under the boundary it interrupted. That matters most in the window with *nothing* recorded, which no
ledger can speak to: a child is created before the write that records it, so a loop that died between the two
leaves an empty list beside a real issue on GitHub, and the phase is all that says so.

Beside that, a pre-split phase is believed only as far as the record bears it out: those phases say "nothing has
been cut from this ref" only on a record that shows no split ever started — a consumer or a split child on the
ledgers, or the `expected_children_count` the transaction writes in the same durable step as `splitting`, ahead of
its first create. That count is what upgrades a pinned comment an *earlier* binary already rewound: the guard stops
new rewinds and nothing migrates records already in flight, so what has to answer for one of those is the evidence
no phase write ever touched. That same count is then asked of *every* boundary, ahead of the phase, because a record
the count proves finished is whole wherever it happens to be standing — and more than one boundary needs it.
`splitting` is two answers rather than one: the phase goes down before the first create *and again beside every child
recorded*, the last one included, so a crash between that final write and the announcement leaves a complete ledger
wearing a mid-loop boundary. `snapshotting` is the same question one retry later: a transaction resumed after a park
rewrites it over whatever boundary it had reached, so a finished split comes back wearing the one it started from.
Reading either as mid-flight retains the ref for good, since nothing revisits a cancelled owner to move the phase on.
So the count is compared against the positional register the loop appends to, and a register that reached it is a
loop that finished. A stale count from an ordinary decomposition of the same issue reads as unfinished, and is
meant to — being wrong that way keeps a ref and holds a terminal, where being wrong the other way deletes the only
copy of a child's work. Past the loop no corroboration is needed: `superseding` is reached only once every child is
created *and* recorded.
Deletion is idempotent because an absent ref is a success, so the crash between the push that removed it
and the write that would have recorded it costs one request on the retry — and it is named against the commit the
split preserved rather than against whatever a fresh read observes, so a ref somebody re-pointed is a mismatch left
for a human instead of the one blind write in the whole namespace, aimed at destruction.

The ref that gets deleted is re-derived, never taken on the ledger's word: it has to equal the name the namespace
mints for this issue, this cycle, and this generation. The transport's own checks are the namespace and the commit,
and neither is identity — every generation of every issue in a lineage was cut from the same candidate and so names
the same SHA, which means a hand-edited entry pointing at a sibling's ref would satisfy both and destroy the only
copy of exactly what that sibling was told to reuse. A target that is not this generation's own is recorded `failed`
and holds the terminal open, the same answer a foreign branch gets.

An opaque ledger is refused per ledger rather than as one flag. The two are preserved and written apart, and they
stop different things: an entry this binary cannot type on `late_resources` means no reclamation can be recorded at
all, while one on `late_consumers` means only that no snapshot's proof can be taken — the superseded branch, which
owes no consumer anything, is still deleted and still retried. Folding them together would leave a branch on the
remote because somebody hand-edited a list of issue numbers.

A branch is owed in **every** state but `reconciled`, not only in the two this binary writes. The ledger takes any
state the vocabulary defines from any writer, so an entry left `retained` — a reading that means something for a ref
and nothing for a branch, since no condition keeps one — would otherwise be retried by nothing and reported by
nothing, and the umbrella would close saying it owed nothing over a branch still on the remote.

A ref is owed on exactly the same reading, and for a reason the branch rule only hints at: there is no state under
which an object still on the remote is settled. A ref kept because a consumer could not be proved *ended* is one
this repository is holding, and an umbrella closed over it is an object nothing would ever come back for — the
parent is `done` by then, and no pass revisits a `done` issue. So `retained` holds the terminal exactly as `failed`
does, the label staying put *is* the retry, and the reason it is held is logged on every tick that holds, since a
hold attempts nothing and therefore writes and emits nothing.

"Ended" is the consumer's own issue state rather than its label. All three dispositions that end a child — reaching
`done`, being `rejected`, and a human closing it — close the issue, and none of them survives a reopen, while a
label does: a child reopened while still wearing `done` is live again, and a reading taken off the label would
delete the only copy of the work it came back for.

The delete itself is a small transaction, because the proof above is a reading of live issues and a reading is not
something a retry can reproduce. The entry is written `reclaiming` **before** the delete, and every recorded consumer
is read once more *past that write and immediately ahead of the delete* — the scan the pass qualified the ref on was
taken before the branch half ran and before anything was recorded, and each of those steps is a request a human can
reopen a consumer during. A consumer that came back inside that window keeps the ref, and what is left is the delete
request itself.

A later visit acts on that recorded decision for one thing only: finishing a delete the remote already took. Past
the consumer proof it asks about the ref in one read and qualifies only if the remote no longer has it. A ref still
on the remote is one a reopened child may still be cutting from, and no record of a past decision outranks the
reading in front of it. A delete the transport could not answer at all is caught and reported as the refusal it is,
so no attempt is spent without a typed `snapshot_delete_failed` behind it.

What follows a delete the remote accepted is the **receipt**, and it runs before the entry is recorded
`reconciled`. Every recorded consumer is told once that the snapshot has been reclaimed and that reuse now needs an
explicit new split cycle. The ref is never recreated. The receipt carries a hidden marker naming this owner, cycle,
and generation, and a consumer already holding one of ours is not told twice.

It is a COMMENT and nothing else, and that is the whole design. A pinned comment is written *whole* by whoever
writes it, so a handler of the child's own that read it before this pass and wrote it after would put the reclaimed
pointer back and take any park off, with nothing left to notice. A label is no proxy for "no writer" either: a
terminal finalize sets `done` / `rejected` **before** its last write, and closed `workflow:ready` /
`workflow:blocked` are swept by nothing, so a consumer a human left on one never becomes terminal at all. A comment
is appended rather than rewritten, cannot be lost to a concurrent writer, and reaches a consumer in every state a
consumer can be in. A child the pass could not reach, or whose thread it could not read or post to, leaves the entry
`reclaiming` rather than reconciling it, because reconciling is what stops anything coming back — and this owner is
the only thing that would.

What ACTS on it is the child's own guard, and it is the child's on purpose. It runs on the child's own **dispatch**,
ahead of every handler and ahead of the terminal no-op — which is the point, because a consumer that ended wears
`done` or `rejected`, reopening one leaves the label exactly where it was, and both are labels nothing below would
touch. Running there also means a relabel straight to another stage cannot route around it.

What it asks first is the **receipt**, and that answer is authoritative. The marker names the owner, cycle, and
generation its own ancestry records, and it is read only off a comment of ours — so neither a later reclamation's
receipt nor a third party pasting one can speak for this one. It outranks every reading of the ref because it
records what *happened*: a mirror this host never dropped, or a ref pushed again at the same commit, would both make
the world look untouched while the guarantee the child was given — that its candidate provably came from one
adjudication — is gone. It costs one walk of the child's own thread per tick, paid only by issues a split created.
A thread that could not be *read* is not a thread with no receipt on it: everything asked after this can look
untouched while the answer that outranks it sits unseen, so an unreadable thread holds the dispatch rather than
falling through to readings a receipt would have overruled.

Where no receipt landed — a crash, a thread it could not post to — the ref itself decides. This host's own mirror
first, so a child of a live split never reaches the wire; only a mirror that is already gone costs one `ls-remote`
for the exact ref and commit its ancestry records. What makes the mirror worth reading is the order a reclamation
runs in: it drops this host's copy *before* it touches the remote ref, and a copy that cannot be proved gone refuses
the reclamation rather than being logged past — so a mirror still standing says nothing has been reclaimed, instead
of saying so only when the best-effort half happened to succeed. Standing *at the recorded commit*, that is: the copy
lives in the object store every agent's worktree shares, so it is resolved and compared against the exact candidate
the ancestry names, and one pointed anywhere else is read as no copy at all.

That is a claim about the orchestrator that would do the reclaiming, so the pointer carries it rather than the
reader assuming it: the split stamps `late_ancestry_mirror_first` onto every ancestry it seeds, and a pointer
without one — written when the remote ref went first and the mirror came down best-effort behind it — skips the
shortcut and pays the ask. A mirror standing beside a ref that is already gone is exactly the world that ordering
was inverted to rule out, and exactly the world such a pointer may be living in.

The ask itself gives three answers and each is a different verdict. `absent` is the reclamation nobody told this
child about, and it parks. `mismatch` is the ref carrying somebody else's commit, which is not the candidate the
child was promised — the reclamation refuses one of those for a human, and so does this, under its own park reason
and its own sentence. `unreadable` is neither: an outage is evidence of nothing, so the dispatch is held for the
next tick with nothing written at all — parking every late-born child through a rate-limit window would be a
self-inflicted stop, and starting one against a ref nobody could vouch for is the failure the guard exists for.

One shape has no recorded ancestry to read at all, and it still has to stop. The transaction records a child on the
parent's ledger **before** it seeds that child's ancestry, because a child on GitHub the parent does not record is a
child nothing would ever come back to — so the window between the two is durable, and an ancestry write that failed
leaves an issue whose BODY tells it to reuse a snapshot and whose pinned comment says nothing. The reclamation
counts it as a consumer all the same and leaves its receipt on it all the same. So the body's own marker is what
says whose child it is — free, since the dispatcher already holds the issue, and absent on every issue no split
created.

That marker is also the one lineage claim here that comes out of a field the world can write, while everything it
competes with is authenticated — a pinned comment only the orchestrator writes, a receipt checked against its
author — so it is **corroborated rather than believed**. The owner's own generation is read fresh and has to name
the same cycle and generation and carry this issue's number among the consumers it cut from that ref. A claim it
does not vouch for is a claim about nothing and the guard steps aside: parking an issue on the strength of a
sentence somebody typed into its body is a denial of service, and an owner cannot have reclaimed a ref while its own
ledger may still be short one child. A record nobody could read, one whose consumer list this binary cannot type,
and one naming no candidate all leave the claim standing instead — it may be true and this tick cannot tell — so the
dispatch is held.

What a vouched claim yields is the whole pointer the failed seed never wrote: the ref the identity mints, and the
commit the owner recorded preserving. The ask is then the same one the recorded shape makes — is *this* candidate
still obtainable — and it has to happen, because the receipt is posted *after* the ref is deleted, so a thread with
no receipt on it is what that window looks like, and so is a thread nobody could read this tick. The park then
writes back the lineage the body claims — and only that, since the pointer came out of the owner's record rather
than out of anything this issue holds: the repair the failed seed owed, and what stops the question being asked
again.

A refusal drops the dangling pointer, parks the issue, and returns
before the label's handler is reached, so nothing runs against instructions naming a ref that is gone. Both writes
are taken on the issue's own dispatch, so there is no second writer to lose them to — which is what makes the park
durable where the owner's could not be. The one label it steps aside for is `workflow:decomposing`, beside the
adjudication guard that shares its read: an issue under adjudication is working from its own candidate rather than
an ancestor's snapshot. That is decided from the read rather than from the label, because the label alone cannot say
which of the two an issue is — a consumer closed while it was being decomposed comes back wearing it with no
generation of its own, and only a live generation on the record makes it this issue's own state.

A ref the remote *refused* to delete is a permission or ruleset problem an operator has to see, and the parent
staying open is how they see it.

Both of those retries are still reached once the umbrella has closed nothing, because a human who closes the owner
mid-cycle takes it out of every other pass. The narrow closed-owner sweep is what brings a tick back to it: on the
existing `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` cadence, cleanup-only, cap-exempt, never an activation or a spawn, and
reached even past the `backlog` / `paused` filter that parks everything else — dropping a closed owner there would
lose the close itself, so the route is taken and the control label defers only what the pass would DO.
What it does with the cycle that close ended is [below](#what-a-close-mid-cycle-ends-and-what-it-still-settles);
what it does with the ledger is exactly this, rule for rule (see
[`../state-machine/delivery-stages.md`](../state-machine/delivery-stages.md#closed-owner-cleanup-sweep-no-label-of-its-own)).

Two more things block it outright, and both are the same rule: nothing that cannot be *proved* settled lets a
terminal fire. An obligation ledger this orchestrator could not fully type blocks whatever the typed view says — the
entries it could not read are still obligations, and closing on the strength of a projection is the reading the
verbatim copy exists to prevent. So does a ledger holding anything at all on a record whose cycle identity is
damaged: there is nothing to correlate a reclamation to and no issue number to prove a branch belongs to this
generation, so the umbrella stays open and says so where an operator reads it. An issue that never entered the late
gate carries no ledger and answers without a write, which is every umbrella the initial decomposer made.

### What a close mid-cycle ends, and what it still settles

A human can close a late-split owner at any of the boundaries above, and every one of them leaves a different amount
of this orchestrator's work standing on somebody's repository. None of them leaves a workflow anybody wants resumed,
so the close ends the **cycle** rather than the tick, and `late_cancellation.py` is what the ending consists of. Two
passes reach it: the post-agent owner read above, for a close during a run, and the closed-owner cleanup sweep, for
a close at any boundary no agent was running at.

**The mark goes down before anything external happens**, and that is what makes everything after it safe to retry. It
says the cycle is over, so no gate below will spawn, adjudicate, relabel, or create another child; it carries the
moment the obligation was taken on; and it is irreversible within the cycle — a human who reopens the issue does not
get this cycle back, so a later visit re-marks the same cancellation and moves neither the stamp nor the boundary it
was taken at. What the reopen gets is the rest of the ending: the cycle settles what it already put on the
repository, the owner reaches `rejected`, and an operator removes that label to authorize a fresh attempt.

The `late_cancellation` record rides that same write, which is what bounds it to one per cycle rather than one per
sweep. The mark keeps the boundary as well as the moment, because `cancelling` is itself a phase and overwrites the
one it interrupted — and the interrupted one is what the retention rule falls back on when the record cannot prove
for itself that the split loop finished.

**Then the held pull request**, which is the one external thing a cancellation owns that the umbrella's terminal
never sees: every path that reaches an umbrella superseded the pull request its work was on along the way, so a
cancelled cycle is the only shape where one is still open under a "do not merge" notice with its original description
preserved on the issue.
Nobody is going to adjudicate it now, so the hold comes off, one marked notice says why, and the pull request is
closed — in that order, so a change that ends up closed is not also left wearing a hold nothing will ever take back.
A release that failed on a still-open pull request stops the close, since the preserved copy is the only record of
what the hold replaced. The notice is said at most once, proved from the pull request's own thread rather than from
a receipt, and the entry is recorded either way: `reconciled`, or `failed` with `pr_reconcile_failed` behind it.

It is re-asked on **every** visit, including one whose entry already reads `reconciled`, for exactly the reason the
ordinary supersession is: that entry records what an earlier visit did, and a human can reopen the pull request
behind it — an owner the sweep is still visiting for a branch it cannot delete would otherwise reach `rejected`, and
leave the sweep for good, beside a change that is open again under a cancelled cycle. Re-asking costs one fetch and
one comment listing and repeats nothing, while the write and both sinks stay behind a state that actually moved, so
a settled pull request adds no record per cadence.

**Everything else is the reclamation owner's, unchanged.** The superseded branch and the immutable ref are settled by
exactly the rules and in exactly the order the umbrella's terminal settles them in, and a cancellation buys no
shortcut through any of them — a consumer that is live again keeps the ref whether or not its owner is closed.

What a cancellation does change is which ledger the rule is read against. The count the split writes before its
first create is what tells a partial ledger from a whole one, and a cancelled loop can never reach it: the children
it did not make are ones nothing is going to make, so the ref would be held on a proof no pass could ever complete
and the terminal with it. So the loop that stops writes down that its register is **final** — which it may, because
every barrier that ends it is asked after the write that records the child in hand, and no further one will be
opened. The ref then goes by the ordinary rule, once every child the split actually cut has ended.

The one cancellation that seals nothing is a **resumed** walk stopped before it reached the first unrecorded index.
A create is a request and the write recording it is another, so a pass that died between them left a child on GitHub
with nothing naming it; the adoption lookup is what answers that, and until this walk has asked it the register
cannot be called complete. There the ref stays held on the count, exactly as before.

**One branch is this ending's own to take on**, and only one. The transaction settles the held pull request and
records
the branch that PR carried in two writes — the second is the retirement, and retiring ahead of a supersession that
might not land would let the children loose beside a change still carrying their work. A close in that window leaves
a cycle whose candidate is preserved, whose held PR is closed, and whose branch nothing on the record names, so
settling around it would retire the owner over a branch the remote keeps for good. A cancellation whose kept
cycle therefore resolves that branch and records it as owed — off the announcement's own *receipt* rather than off
the phase, because a park at the supersession is resumed from the top of the transaction, which rewrites the
earlier boundaries while stepping over the announcement it already made; a second failed attempt stands at
`splitting` with the receipt still set. Not before that receipt, since the snapshot is created *and proved* ahead
of the first child and the branch stops being the only copy there. Only where the record names no branch already,
in any state. And only once the held PR above is actually *settled* — that boundary is written *before* the
supersession is attempted, so it says the attempt was reached and nothing about whether it landed, and inferring
the branch while the pull request is still open would delete, out from under a change a human can still see, the
branch that change is built on. Nothing is lost by waiting: the pull request is re-asked every visit, and the one
that closes it takes the branch on.

**Every child that already exists is left entirely alone.** A cancellation mid-loop finds real GitHub issues carrying
real slices of somebody's work, and what happens to them next is a human's decision rather than this ending's: they
are not closed, not relabelled, not written to, and not commented on. The receipt a reclamation leaves is what a
*live* split owes the children it is still responsible for, and a cancelled cycle is responsible for none of them —
so even the visit that deletes the ref reaches no child. Each is still *read*, because proving every consumer ended
is what permits the delete at all, and that reading is the whole of what any of them costs.

Their *ledger* entries are discharged, which is a different thing from touching them. The ledger is not an inventory
of what exists but the list of what this orchestrator still owes somebody's repository, and a cancelled cycle owes a
child nothing: the entry's obligation was the receipt, and there is no receipt. Marking them reconciled is a local
write to the owner's own pinned comment, and it is what lets a settled cycle read as settled — left pending, they
would say the ending is unfinished forever, on an obligation no pass is ever going to discharge.

Nothing about the ref goes unsaid by that silence. The transport drops this host's copy of the ref *before* it
touches the remote and refuses the whole reclamation if that copy cannot be proved gone, so a child reopened
afterwards finds no mirror, asks the remote once, and is stopped and told by its own guard on its own dispatch —
which is where a receipt would only ever have been read.

**A reopen resumes nothing, and skips no part of the ending.** The mark is irreversible within the cycle, so a
human who reopens the issue does not get that cycle back, and both labels an adjudication can be wearing name a
handler that would act on the issue rather than settle it. Reopening fast enough does not undo it either: reaching
the closed-owner route at all is what says a close was *observed*, so an issue that pass finds open again — a human
who reopened it between the poll and the worker's refetch — is marked cancelled all the same, and stopped there.
Nothing external is done to an issue somebody has just reopened and no terminal is written; the mark is what hands
it to the guard below from the next tick. The dispatcher's own pinned-state guard catches that
window: it runs exactly the reconciliation above, reaches no handler, and writes the same terminal below. It *runs*
the cleanup rather than merely refusing because the closed-owner sweep visits closed issues only, so a refusal with
nothing behind it would freeze the issue until somebody closed it again. What it does not do is close the issue: a
human just reopened it, and the label is the whole of what this pass has standing to write.

Which labels that ending may be written from is the whole of what the label decides here — *whether* it is refused
is not, since a cancelled cycle is refused under every label including none at all. The two an adjudication runs
under are the two the terminal is declared out of. From the unlabeled state the record answers instead, because
three different issues wear that same nothing: an operator who took `rejected` off, a human who stripped a workflow
label mid-cleanup, and an ending whose terminal write GitHub refused. A cycle whose terminal is proved applied is
not handed it back, which would undo the one authorization a restart has; one carrying no such proof is owed the
write and gets it once its obligations settle.

**The terminal comes last, and only once nothing is owed** — branch, ref, and *every* unreconciled `plan_pr` entry on
the ledger, a recorded number with no preserved description beside it included. That last one is the entry no pass can
settle: the description the hold displaced is the only copy there was, so nothing may put it back or close over it,
and the terminal is held until a human repairs the record rather than closing a pull request this cycle cannot show it
ever marked. What the ending *does* discharge is the child receipts, which are recorded `pending` when each child is
created and which nothing has ever moved: `rejected` authorizes a restart, and a restart projects its fresh cycle only
over a ledger with nothing unreconciled on it, so leaving them would retire an owner whose restart then refuses for
good. Saying so changes nothing on the children themselves. That last reading is wider than what the pass acts on,
deliberately: acting takes the hold's own record, since releasing one means knowing which pull request this cycle
marked, while being owed takes the ledger, because an entry left under a number a later write cleared is still an
obligation and a retired owner is one nothing revisits. It is what a restart counts too, so retiring over one would
refuse the fresh cycle that terminal is meant to authorize. An owner that has settled all of them is handed
`rejected`, which is both the honest end of the cycle and what takes the issue out of the closed-owner sweep for good
— every label the sweep queries is one the owner keeps until this write lands, and this write is the only thing that
takes one off. So a cancellation that finishes
costs a bounded number of passes, and one that cannot — a remote that refuses a delete, a consumer that is live again,
a ledger a human has to settle — keeps the label, keeps being visited, and says on every visit what it is still
holding. An issue that never entered the late gate has no cycle to end and is not touched at all, which is every
umbrella the initial decomposer made.

### The restart that ending authorizes

`rejected` is not only how a cancelled cycle ends; it is the thing an operator **removes** to start a fresh one. That
removal is the whole handshake, and it authorizes rather than merely permits: taking a label off is a write GitHub
grants only to a repository's own people, and the record it is read against is a pinned comment only this
orchestrator writes. So the `ALLOWED_ISSUE_AUTHORS` allowlist — the guard on the one route a stranger reaches by
filing an issue — is deliberately not asked here, and an outsider's issue an operator has decided to restart is
restarted.

What the record has to prove is a *completed* cancellation: a cycle that exists, one a close already ended, one that
owes nothing, and one whose `rejected` was **proved** applied. That last one is the second half of the ending's
two-phase terminal record, and it is there because the gesture cannot be read off the issue's surface: a human who
strips a workflow label mid-cleanup, and an ending whose terminal write GitHub refused, both leave the same unlabeled
issue an operator's removal does. The decision to write the label goes down before the write; the proof goes down
only for a label that landed, so an attempt never reads as a terminal. The pass that makes the write takes that proof
from the write returning rather than by reading the issue back, since a client's cached labels outlive the write that
changes them and a closed owner gets no second visit. Where the proof is missing the ending writes the terminal it
still owes instead, and the handshake becomes available from there — which is also how a cancellation that ended
before this record existed is brought into it, since any visit that finds the issue still wearing `rejected` writes
the proof down.

Everything else stays inert, and inert means *undispatched*. A cancellation still owing the remote a branch, a ref,
a child receipt, or a held pull request belongs to the cleanup above until it does not, unlabeled or otherwise; a
closed issue is the sweep's whatever its record says; and an unlabeled issue that already carries a pinned comment
at all — a rejection from anywhere else in the workflow included — is left where it is rather than greeted a second
time, since a second pinned comment is invisible from the moment it is written while the finished workflow in the
first goes on deciding.

"Owes nothing" is the ending's own outstanding list *and* the domain's settled-ledger reading, because neither
contains the other. The pull request a generation names and cannot show it held is carried on no ledger and
reported only by the ending — restarting over one would delete the last thing on the issue pointing at a change this
orchestrator left marked and open — while a child receipt and an untypeable consumer ledger are counted only by the
domain, and a restart that reached its retirement over one of those would be refused there with its marker already
down.

The restart itself is a transaction over that same pinned comment, and each step is idempotent so a crash resumes at
the one still owed. The marker naming the cycle it intends, the cycle it succeeds, and the label it means to apply
goes down **first** — a tick that died between the write and the effects has to finish *that* cycle rather than mint
a second one and announce it twice. Then one notice on the thread, suppressed by a marker scoped to the cycle being
minted, and the target label, skipped where the issue already wears it and this orchestrator is what applied it —
a name somebody else applied is taken off and put back, since the restart's own application is what separates the
fresh cycle from its predecessor's terminal in the label history the ending reads. Only once both have reconciled is
the marker retired. Which label it is comes from the current `DECOMPOSE` setting when the marker is written and from
the record thereafter, so a restart begun under one setting and resumed under the other finishes what its own notice
announced.
A refusal from either effect keeps the marker standing and is said out loud on both sinks; `backlog` / `paused` defer
the whole of it, since the authorization sits on the issue's own surface and no tick can lose it.

What the fresh cycle inherits is what is true about the **issue** rather than about the attempt that ended: the
pinned comment's own identity, the bounded list of comment ids this orchestrator posted, the cumulative usage the
issue has already paid for, and the identity joining the new cycle to its predecessor. Every session, the pull
request and branch, the children and the dependency graph, the snapshot it was cut from, the parks, the drift
baseline, the counters, and the timestamps are gone, and the lineage depth and generation counter are back at zero —
a restarted issue is a fresh attempt with room to split, not a cancelled one wearing a new number. The full key list
is in [`../state-machine/labels-and-state.md`](../state-machine/labels-and-state.md#late-generation-state).

### What a human can still change once the transaction has started

Every transaction park is a moment the humans can speak into, and the coordinator settles what they said *before* it
replays the recorded verdict — so a split that stopped halfway can meet a fresh edit or a fresh instruction on the
next tick. Two rules keep that from undoing work that already exists.

A generation whose split has **already acted outside this process** may not be revised into a new one, and two
effects put it past that point.

*Children*, because a second manifest over the top of real GitHub issues strands every one of them: nothing polls a
child the parent stops recording, they carry an ancestry naming the adjudication that made them, and they are the
consumers the snapshot is retained for.

*A recorded snapshot obligation*, even with no child yet. The ref is named for the generation but the **commit under
it** is the candidate that generation froze, and the reclamation proves a ref is ours to delete by comparing the two
— so a revision that moves `candidate_sha` leaves the entry pointing at a ref that no longer matches it, the
reclamation reads a mismatch and refuses, and the umbrella's terminal stays open over a ref nothing can ever settle.
The entry is refused in every state it can hold, because none of them proves the ref is absent: `pending` is a push
that may have landed, and `failed` is a create that may have landed beside a verification that did not. An opaque
ledger is refused with them, since an entry this binary could not type may be exactly that obligation.

Either way the issue is handed back naming what stands — their comment stands, whatever the split created stands,
and the recorded verdict stands — and which of those to keep is a decision about things that already exist.

A generation that **does** advance carries none of the previous one's split receipts. `late_split_children` and
`late_links_announced` are cleared with the counter, because both are one-shot and positional: a register carried
forward would have the new manifest adopt an old child by index, and a link receipt carried forward would swallow the
announcement the new split owes. What is *not* cleared is either external ledger — a ref the remote holds is owed
whatever this generation decides next.

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

**A spent spawn budget outranks even drift.** An adjudication is charged to the issue's shared daily spawn budget,
and a budget with nothing left in it parks the issue as `retry_cap` before any of the reading below happens. What
that park is waiting on is a human deciding to spend more of the issue's day on this candidate, which an edited body
is not — so the tick ends at the park, the fingerprints are not even read, and the frozen commit, the late session,
the recorded generation, and the hold all stay where they are. The one reply that lifts it is a trusted
`/orchestrator continue`, and what it buys is one adjudication; the notice explaining the park has to have reached
the thread before any comment on it is read as that answer, since saying it is what moves the response boundary.
The budget itself is in [`../state-machine/labels-and-state.md#the-retry-budget`][retry-budget].

**Drift outranks every answer.** An edit to the title, the body, or a comment already counted into the baseline
changes what the candidate is supposed to BE, and an answer that arrived in the same window was written about the scope
as it stood before — applying it would adjudicate a reply against requirements it never saw. So the tick that first
sees drift parks (`late_content_drift`) and consumes nothing: the frozen commit, the late session, the recorded
generation, and any hold are all left exactly as they were, because none of them is wrong, only unadjudicable
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
[retry-budget]: ../state-machine/labels-and-state.md#the-retry-budget
[agent-run-circuit]: ../state-machine/labels-and-state.md#the-agent-run-circuit
[size-gate]: ../state-machine/delivery-stages.md#the-size-gate-on-a-published-pull-request-every-push-onto-an-open-pr
