# Workflow labels, per-tick flow, and pinned state

This page is the authoritative spelling of the state machine's public contract: the label set and how a wire label is
spelled apart from the stage under it, the migration off the pre-namespace spellings, what one tick reads and writes
per issue, and every pinned-state key a handler depends on. Live GitHub issues carry these strings, so a rename here
is a migration rather than a refactor.

The handlers that act on them are in [`delivery-stages.md`](delivery-stages.md) and
[`conversation-stages.md`](conversation-stages.md); the compact label-lifecycle reference is in
[`lifecycle.md`](lifecycle.md). For the summaries these sections are reached from, see
[`../state-machine.md`](../state-machine.md).

## Workflow labels

An issue should have at most one workflow label at a time. Non-workflow labels such as `bug` or `enhancement` are
preserved; the orchestrator only swaps labels from its own workflow set. Label names are part of the public contract
because live GitHub issues carry them.

A state's label and the stage under it are spelled apart throughout this file. `workflow:<tag>` is the **wire label**:
the literal string on the GitHub issue, which is what a label write puts there, the transition guard checks, the
pollable-issue queries ask for, and the per-tick dispatcher partitions on. A bare `<tag>` is the **stage**: the handler
that runs while an issue carries that label, the subpackage under `orchestrator/workflow/stages/` holding it, and the
identifier analytics rows, audit event payloads, and agent-session attribution carry. The labels that were never
namespaced — `in_review`, `question`, `discussion`, and the `done` / `rejected` terminals — read the same either way.

The prefix is a collision guard, not the membership test — being a `WorkflowLabel` member is. A `workflow:`-prefixed
name that is not one resolves to no state at all: the `workflow:dependencies` / `workflow:github_actions` /
`workflow:python:uv` service labels Dependabot stamps on its update PRs
([configuration/operations.md](../configuration/operations.md#continuous-integration)) share the prefix and nothing in
the tree reads them, so they route nowhere and a label write leaves them in place exactly as it leaves `bug` or
`enhancement`. Applying one as a workflow label is what the typo guard below rejects.

Three non-workflow **control labels** modify behavior without occupying the workflow slot:

- `backlog` makes the orchestrator skip the issue: the per-tick dispatcher filters it out before the family/fanout split
  (so a parked, workflow-label-less issue cannot fold into the cap-counted family bucket and starve other work under
  `parallel_limit=1`), and each stage handler also skips it before the workflow label is read. Removing it hands control
  back to the state machine on the next tick.
- `paused` is the same hard skip as `backlog` at every point (dispatch, scheduler routing, `_process_issue`, and base
  sync), differing only in intent: `backlog` is a "not yet" hold on a fresh issue, `paused` freezes an already
  in-flight one without discarding its state. Removing it resumes processing on the next tick. Because those skip
  points read the issue's labels at tick start, every stage that runs an agent additionally re-checks a freshly
  fetched issue right after the run returns (`_paused_during_agent_run`, alongside each stage's `interrupted`
  short-circuit — both live on the `workflow/engine/guards.py` owner the stage leaves import directly): the dev-agent
  stages that resume committed work (`implementing`, `in_review`, `fixing`, `resolving_conflict`, the `validating`
  drift / awaiting-human / reviewer-change dev resumes, and the `documenting` initial and follow-up docs passes) and
  the stages whose agent is not a developer (the decomposer run in `decomposing`, fresh spawn and awaiting-human
  resume; the reviewer run in `validating`; and the question and discussion runs, opening round and awaiting-human
  resume alike) all consult it. A `paused` applied
  mid-run stops before a PR opens, the label flips, a HITL park or ACK comment posts, a docs push lands, usage
  counters fold, child issues are created, watermarks advance, or pinned state advances, so a dev stage's committed
  work stays on the branch and republishes through the normal recovered-worktree / stranded-fix path once the label is
  removed, while the read-only decomposer / reviewer / question runs simply re-run from durable state on the next
  tick. The `discussion` run is read-only only until the humans confirm the design: a confirmed round commits
  `plans/issue-<number>.md`, so a pause can leave that commit on the branch with no disposition, exactly as a crash
  can. It is not re-run — the pre-spawn write (the round anchor, `discussion_round_open`, and `discussion_base_sha`)
  is durable whatever the pause withholds, and the next tick reads the commit back through it: a valid plan is judged
  and published as the withheld round would have, and anything else parks on what the branch actually carries. Only a
  round that committed nothing re-runs against the same replies, since the watermark it staged is one of the
  mutations the pause leaves unpersisted. Because `paused` is a plain control label, removing it is the entire resume
  protocol — the next poll picks the
  issue back up from durable state; there is no un-pause command. This is distinct from `/orchestrator continue`
  ([`_handle_fixing`](delivery-stages.md#_handle_fixing-label-workflowfixing)'s `_handle_continue_command`, plus
  the shared implementing / documenting handling), which retries
  only specific `awaiting_human` session-failure parked *retry* flows — and, on a `decomposing` or `implementing`
  issue parked under [the retry budget](#the-retry-budget), renews that budget for one more spawn: pausing is never a
  `park_reason`, so a continue command is not an un-pause and does not clear `paused`. It is unrelated to un-pausing,
  but not exempt from it — the hard skip fires in `_process_issue` before any handler, so a continue comment posted
  on a paused issue is deferred with everything else until the label is removed.
- `workflow:community_contribution` is applied by the per-tick open-PR sweep (on the `workflow/engine/tick.py` owner,
  which drives it before per-issue dispatch) when `ALLOWED_ISSUE_AUTHORS` is configured: any open PR whose author is
  not in the allowlist is labeled and `HITL_HANDLE` is @-mentioned once per PR. Bot-authored PRs
  (Dependabot, Renovate, CI bots) are skipped via GitHub's `user.type == "Bot"` flag — they open PRs structurally and
  are not community contributions. The orchestrator does not otherwise drive these PRs. With `ALLOWED_ISSUE_AUTHORS`
  empty (the default), the sweep is a no-op. The label is the sweep's own dedup marker rather than an operator
  control, which is why it is namespaced where `backlog` / `paused` are not, and why the sweep asks for both its
  spellings: a PR the bootstrap rename could not reach is already labeled, and re-labeling it would repeat the ping.

### Typed states and the transition guard

The label vocabulary is defined once in [`orchestrator/workflow/state.py`](../../orchestrator/workflow/state.py), which
every caller inside the tree imports directly — `orchestrator.workflow` re-exports the same objects for callers
outside it: `WorkflowLabel` (a `StrEnum`) is the single source of truth for workflow states, and `ControlLabel` holds
the modifiers above. Because `StrEnum` members *are* their wire strings, a member is the GitHub label verbatim — the
enum just gives the names one authoritative definition. The labels the orchestrator writes itself are namespaced
`workflow:<tag>` so a repository's own labels cannot collide with them; `in_review`, `question`, `discussion`, `done`,
`rejected`, and the `backlog` / `paused` controls keep their bare spelling because a human applies or reads those
directly. The automatic `workflow:community_contribution` control is namespaced with the rest of what the
orchestrator applies. The
namespace stops at the GitHub boundary, and `stage_name` on the same owner is what strips a wire label back to the
stage tag every sink below that boundary records. A repository whose labels predate the namespace still carries the
bare spellings; how it moves off them is [below](#legacy-labels-and-the-migration-off-them).

Two guards run at `GitHubClient.set_workflow_label` (the single label-write chokepoint; `create_child_issue` bypasses
`set_workflow_label` and shares only the typo guard for its direct write, coercing each child label through
`coerce_workflow_label` — the same strictness):

- **Typo guard (always strict).** A label name not in `WorkflowLabel` raises immediately, so a typo cannot be applied as
  a literal label that the next tick would treat as unlabeled-pickup. `create_child_issue` coerces each birth label the
  same way, so split children are born with only a valid workflow label and any control label is rejected.
- **Transition guard (`WORKFLOW_TRANSITION_GUARD` = `off` / `warn` / `enforce`, default `warn`).** An illegal
  `current → new` relabel is checked against `ALLOWED_TRANSITIONS`. `warn` logs the rejected edge through the
  `orchestrator.state_machine` logger and proceeds; `enforce` raises `IllegalTransition`; `off` disables the check. A
  same-label re-set is always allowed. That logger name is spelled out literally in the owner, so an operator log
  filter selects on it regardless of which module the guard lives in. One write asks to skip it (`guarded=False`), and
  only one: the late size gate putting a label back where a human moved it from. The graph describes the moves this
  orchestrator makes, so it has no edge for repairing one it never made — a guarded `workflow:validating →
  workflow:decomposing` restoration would raise under `enforce` and strand the generation under the wrong label for as
  long as the operator kept the guard on, which is the opposite of what the guard is for. See
  [`../workflow/roles.md`](../workflow/roles.md#what-the-humans-can-still-change-while-a-candidate-is-frozen).

`ALLOWED_TRANSITIONS` is a forward spine (e.g. `workflow:implementing → workflow:validating → workflow:documenting`)
plus interrupt / detour edges declared per-target. It is keyed by `WorkflowLabel` members, so a pre-namespace label
resolves to its member before the guard sees it and is checked against the same edges. Operator relabels via the
GitHub UI bypass both guards, so the guard never fights a human.

Fourteen of those edges belong to the late size gate and are declared ahead of the handlers that write them.
`workflow:implementing → workflow:decomposing` is the route a clean committed candidate measured past the threshold
takes instead of publishing — adjudication runs under the existing decomposing label rather than a state of its own.
`workflow:validating`, `workflow:documenting`, `in_review`, `workflow:fixing`, and `workflow:resolving_conflict` each
own the same edge, and they own it because the gate stands in front of every push onto a pull request the remote
already carries: a commit joining a branch a pull request is open on is measured for what that pull request would COME
TO, and one past the ceiling is held off it and adjudicated from whichever of the five states that push was reached
under. The pre-PR states own no such edge — nothing there has a publication to be measured against.
The same five own the edge BACK — `workflow:decomposing → workflow:validating` / `workflow:documenting` /
`in_review` / `workflow:fixing` / `workflow:resolving_conflict` — because a settled `single` verdict returns the
issue to the stage it was taken out of rather than to `workflow:implementing`: that stage is the only owner of the
completion the candidate still owes, and the record names which one it was
([below](#late-generation-state)). Both directions are declared from one set, so the way in and the way back cannot
drift apart.
The existing `workflow:decomposing → workflow:implementing` edge beside it is the way back a verdict on work nothing
had published takes, carrying the exemption naming the adjudicated commit, so the ordinary publication reconciles
that exact commit the way it does for any other change.
`workflow:decomposing → rejected` and `workflow:umbrella → rejected` are the one terminal a late generation whose
owner was closed mid-adjudication reaches, once its external cleanup is reconciled, under whichever of the two labels
it had reached; they are also the only way a pre-PR state reaches a terminal at all. `done → rejected` is the last of
the fourteen and the only edge out of a terminal this orchestrator declares, and it is there for an owner a human
moved onto the terminal over a cycle that still has an ending to reach. The umbrella's own terminal needs none of it:
the write that records the resolution **retires the cycle with it**, one write before the label, so a close arriving
past that write finds nothing left to cancel and no `done` issue is ever left carrying a late cycle. A close observed
*inside* that write is answered behind it — the generation is still in the pass's own memory, so it goes back
cancelled and the owner keeps `workflow:umbrella`, where the closed-owner sweep reaches the ending. Nothing else may
leave a terminal — the edge set is asserted whole, so the exception cannot grow a second. A restart after such a
cancellation needs no edge of its own: the operator authorizes it by *removing* `rejected`, so the label a restart
applies is written from the unlabeled entry and `rejected` keeps its empty edge set — a rejected issue left labeled
stays inert. The pinned state those transitions move an issue through is [below](#late-generation-state).

- _(none)_ — Open issue not yet picked up by the orchestrator.
- `workflow:decomposing` — The decomposer is deciding whether the issue is single-context or should become child
  issues.
- `workflow:ready` — The issue is decomposed and has no unresolved blockers.
- `workflow:blocked` — The issue is waiting on child issues or dependency edges.
- `workflow:umbrella` — Parent issue with no implementation of its own; closes to `done` when all children resolve.
- `workflow:implementing` — The dev agent is producing commits in a per-issue worktree. A clean result advances to
  `workflow:validating`.
- `workflow:documenting` — The single docs pass on the existing PR worktree, reached only via the final-docs handoff
  in `_handle_validating`'s approval branch (after verify + squash). Advances to `in_review` after a pushed docs
  commit OR an explicit `DOCS: NO_CHANGE` verdict.
- `workflow:validating` — The reviewer agent is checking the diff; on `VERDICT: APPROVED` the local verify gate runs
  `VERIFY_COMMANDS` before the squash + `workflow:documenting` handoff. `CHANGES_REQUESTED` relabels to
  `workflow:fixing` before the dev spawn.
- `in_review` — A PR is open and ready for human review. The orchestrator never merges from here — humans drive the
  merge. A mergeable PR whose current head completed the reviewer-approved final-docs handoff (or carries a real GitHub
  APPROVED review), with no standing human CHANGES_REQUESTED on that head, earns a one-shot HITL ping per head SHA.
- `workflow:fixing` — The dev fix-loop is active. Entered on unread in-review feedback OR a `CHANGES_REQUESTED`
  verdict. A successful fix bounces directly back to `workflow:validating` so the reviewer re-approves.
- `workflow:resolving_conflict` — The orchestrator is resolving a rebase conflict on a PR branch against
  `<remote>/<base>`. Reached only when the per-tick base-sync rebase actually leaves conflicted files, or via an
  operator relabel.
- `question` — Operator-applied read-only Q&A label: the decomposer agent answers in the per-issue worktree and waits
  on a human reply or close. No PR is opened.
- `discussion` — Operator-applied architecture discussion: the decomposer agent researches the repository, explores the
  design as a tree, and comes back with a numbered frontier of currently-answerable questions plus its own recommended
  answers, then parks awaiting human. Answering by number resumes the same session, which recomputes the frontier
  around what those answers settled and parks again, for as many rounds as the humans reply. Once a human confirms the
  shared understanding, the same session commits `plans/issue-<number>.md` and the stage publishes that one file as a
  plan PR, keeping the label and opening no further round while the humans read it. Nothing is implemented here and
  nothing routes an issue in. What takes it out is the humans deciding, in one of three places. Their verdict on that
  plan PR is drained by the stage itself: merged is `done`, closed unmerged is `rejected`, and either finalizes the
  issue and reaps the worktree and the branches. Closing the ISSUE before a plan PR exists is `rejected` the same way,
  with no teardown. Otherwise a human relabel takes it out — to `done` or `rejected` by hand, the two edges
  `ALLOWED_TRANSITIONS` grants the state, or through the GitHub UI to `workflow:implementing` to have the plan built,
  which arrives as an operator relabel and is screened by the read-only guard rather than travelling a graph edge.
- `done` — Terminal success; PR merged, umbrella resolved, or a `question` issue closed.
- `rejected` — Terminal rejection; PR or issue closed without merge.

### Legacy labels and the migration off them

A repository whose labels predate the namespace carries the bare spellings on live issues, so moving it over is one
write plus the reads that cover what that write could not reach.

The write is the label bootstrap, which `runtime.startup.connect_clients` runs once per configured repo at process
start — so such a repository is migrated at the next start, not mid-tick. `ensure_workflow_labels` walks both
vocabularies and provisions each label the repository is missing. Only a namespaced label has a pre-namespace
spelling to migrate off, so only those reach all three answers:

- **The namespaced label already exists** → nothing happens, and a bare label still defined on the repository beside
  it stays defined. The bootstrap neither renames nor deletes it: at the repository level a leftover of its own and a
  name the repository picked for itself are the same thing. Issues still carrying that bare label come off it one
  relabel at a time under the rules below, not on a second bootstrap pass.
- **Only the pre-namespace spelling exists** → it is renamed in place rather than duplicated, which carries every
  issue holding it across in a single edit — including the closed ones and the `backlog` / `paused` parked ones no
  label write of the orchestrator's would otherwise reach.
- **Neither exists** → the namespaced label is created fresh.

The seven labels that were never namespaced — `in_review`, `question`, `discussion`, `done`, `rejected`, and the
`backlog` / `paused` controls — have no second spelling to migrate off, so the bootstrap only ever skips one that
already exists or creates it bare. Which vocabulary a spec came from decides nothing here: the rename is driven by the
label's own spelling, which is why it covers `workflow:community_contribution` alongside the states.

A PAT without `Issues: Read and write` can neither rename nor create: the refusal is logged and the rest of the
bootstrap is abandoned, leaving that repository on its old vocabulary until the permission is granted and the process
restarts. That, the skip case above, and a human re-adding a retired label by hand are what the reads below exist
for.

Three reads take either spelling, so none of them depends on the rename having run:

- **Routing.** `github.labels.workflow_label` reads an issue's labels through `issue_workflow_label`, which resolves a
  bare tag back to its `WorkflowLabel`, so an issue still carrying the old label reaches its handler and the next
  label write rewrites it to the namespaced spelling. (`label_for_name` is the same lookup on the write side, where
  `coerce_workflow_label` accepts either spelling for a label about to be applied.)
- **The community sweep.** It asks for both spellings of `workflow:community_contribution` and rewrites neither: the
  label it finds is proof the PR's one HITL ping already went out, and re-labeling would repeat it.
- **The closed-issue sweep.** Each sweep label is queried under its pre-namespace spelling too, because a closed issue
  is the one case no other pass revisits — see [Pollable issues and finalization](#pollable-issues-and-finalization)
  for the request cost that carries.

An issue can therefore carry both spellings at once, and the namespaced one always wins — `issue_workflow_label`
scans for it across every label before it will settle for a bare tag, so the order GitHub happens to return them in
cannot change the answer. The write side mirrors that read. `replaced_label_names` takes off the namespaced labels
always; a bare tag joins them only when it names a state coming off anyway — because the namespaced spelling of that
same state sits beside it, or because the issue has no namespaced label at all and the bare one *is* its
pre-migration state. So a bare `blocked` or `ready` the repository uses for its own triage, on an issue whose state
is already namespaced, is read past and left in place: that protection is the point of the namespace, and it would be
worth nothing if a relabel deleted the label anyway. The one case the two spellings cannot be told apart is a bare tag
on an issue with no namespaced label — there it is taken as the pre-migration state, which is what lets the issue
keep routing.

## Per-tick flow (`workflow.tick`)

Each tick fans out across every configured repo (`config.default_repo_specs()` returns one `RepoSpec` per `REPOS` line)
and dispatches per-issue handlers through a long-lived `IssueScheduler` capped by `MAX_PARALLEL_ISSUES_GLOBAL` /
`MAX_PARALLEL_ISSUES_PER_REPO`. One repo's pass is owned by `workflow/engine/tick.py`, which `workflow.tick` is the
entry point into; the multi-repo dispatch, the scheduler lifecycle, and the fixed order that pass runs its four steps
in are in
[`architecture.md#per-tick-flow-workflowtick`](../architecture.md#per-tick-flow-workflowtick). What follows is what
each step reads and writes per issue.

The dispatch loop classifies each pollable issue by workflow label before submitting it:

- **Family-aware labels** (`workflow:decomposing`, `workflow:blocked`, `workflow:umbrella`, unlabeled pickup) read and
  write cross-issue state (parent ↔ child). They are folded into one bucket per repo that drains sequentially on a
  single worker thread, so parent / child handlers cannot race. A bucket whose every label is in
  `_CAP_EXEMPT_FAMILY_LABELS` (`workflow:blocked` or `workflow:umbrella` — pure label / dep-graph walks) runs on a
  dedicated executor and does not consume a `MAX_PARALLEL_ISSUES_*` slot, so a blocked parent waiting on children
  cannot deadlock those children. A **closed** issue on a cleanup-swept label is not in this bucket at all: its
  handler is the cleanup sweep rather than the stage its label names, so it fans out with its own exemption.
- **Fan-out labels** (`workflow:ready`, `workflow:implementing`, `workflow:documenting`, `workflow:validating`,
  `in_review`, `workflow:fixing`, `workflow:resolving_conflict`, and the operator-applied `question` and
  `discussion`) only touch their own state and worktree. They run concurrently up to the per-repo and global caps. A
  **closed** fan-out issue (a merged-PR, closed-`question`, or closed-`discussion` issue still carrying its sweep
  label, surfaced by the closed-issue sweep) is submitted `cap_exempt=True`: its handler only runs a terminal
  finalization (flip to `done` / `rejected` + branch cleanup) — or, on a closed `discussion` whose plan PR is still
  open, one PR poll and nothing at all, since that issue is held for the humans' verdict rather than finalized — with
  no agent spawn, so it must not be starved behind active agent work — otherwise under `parallel_limit=1` a merged-PR
  issue sits closed-but-labeled for many ticks while a sibling reviewer or docs agent holds the only slot.

The duplicate-active gate keys on `(repo_slug, issue_number)`: an in-flight handler that straddles polling passes is
reported active to the next poll's submit, which is rejected as `duplicate_active`. The pre-tick base-refresh skips any
active issue's worktree.

Only issue numbers cross the thread boundary — each scheduler worker mints a fresh `GitHubClient` via
`gh._for_worker_thread()` and re-fetches its Issue against that client.

### Base refresh

Before any issue is dispatched the tick runs `_refresh_base_and_worktrees(gh, spec)`: a single
`git fetch <spec.remote_name> <spec.base_branch>` in `spec.target_root`, then per-issue dispatch on each existing
worktree under `<WORKTREES_DIR>/<owner>__<name>/issue-*`. The remote name defaults to `origin` and is overridable per
`REPOS` row. Per-stage `_ensure_*_worktree` helpers only fetch on (re)creation, so without this refresh long-lived
worktrees would stay anchored to whatever `<remote>/<base>` looked like when first added.

Two paths depending on whether a PR exists:

- **Pre-PR worktrees** get a clean-tree `git rebase <remote>/<base>` directly — no remote to push, so the local branch
  stays linear without publishing a rewrite.
- **PR-having worktrees** in `workflow:validating` / `workflow:documenting` / `in_review` / `workflow:fixing` go
  through `_sync_pr_worktree_to_base`. A clean rebase pushes (force-with-lease pinned to the pre-rebase SHA so a
  foreign update rejects rather than being clobbered), resets `review_round`, posts a PR notice, and relabels to
  `workflow:validating` so the reviewer re-runs against the rewritten head. Only when the rebase actually leaves
  conflicted files does the helper relabel to `workflow:resolving_conflict`.

The `question` and `discussion` labels skip both paths unconditionally (`_issue_skips_base_sync`) — the question
handler tears down its own worktree, the discussion stage keeps its checkout across every round exit, and merging
base into either would accrete commits on a branch no developer owns or rewrite the state an unsafe park left for an
operator to read. On the discussion side it is also what protects the publication: the plan is pushed at the SHA the
stage's own check read, so a rebase between that reading and the push — or after it, onto the tip the plan PR is open
against — would move the branch off the commit anything vouches for.

The skip outlives the label. An unconsumed `question_*` / `discussion_*` park is honored whatever the issue is
labeled now, because an operator's relabel to `workflow:implementing` takes the label off a full tick before the
read-only guard rules on the branch, and a rebase in that gap would move the tip off the SHA the guard measures and
convict a branch nobody touched. So are the five records that freeze a branch on their own
([`git/base_sync/frozen.py`](../../orchestrator/git/base_sync/frozen.py)): the two a discussion tick writes BEFORE the
thing they describe (`discussion_round_open` and `discussion_publishing_sha`, which a tick that died mid-round leaves
standing with no park at all, and with the commit it died holding on the branch), the `read_only_baseline_sha` the
guard writes in place of a park it clears, which stands until the dev run commits, and the two commits the late size
gate is deciding about — `late_candidate_sha`, the pair it froze, and `late_approved_sha`, the commit an approval has
still to publish, each dropped by the step that spends it. The `late_collapse_*` group joins them on stricter terms:
a squash mid-rewrite is proved by the TREE the commit on the branch carries, which a rebase replaces with one
carrying the base advance too, so any member of the group being on the comment at all — `null` included, since that
is what the squash's own reader refuses to resume — holds the branch until whatever finishes or undoes the collapse
drops it. Two PARKS freeze the branch as well, and by the park
rather than by a record, because neither can leave one. A standing `late_measurement_failed` is the first: the
sharpest of those refusals is taken before any commit could be named, so there is nothing on the pinned comment to
freeze by — and rebased under it, the exact-pair retry has lost its commit and the refusal that substitutes nothing
for a pair nobody froze is standing on the base. A standing `agent_timeout` is the second, and its watermark names a
commit that does not exist yet: `pre_implement_sha` is the tip the killed run STARTED at, so every reading of it is a
comparison against what the checkout has become since. On the commonest shape of that park — a run killed before its
first commit — the branch carries nothing of its own, so a base that advances fast-forwards the checkout straight
onto the new tip, and the next tick's silent recovery reads a head that moved with no developer having written a
line. Both parks end the way every park does, by being answered — with one exception on the size gate's side: a
`late_measurement_failed` standing over a split that has already become children is retired by the reconciliation
itself, since nothing about that record is a human's to answer, and the branch it was freezing goes back into base
sync with it.

One approval is the exception, and it is this refresh's own work rather than a stage's. An auto rebase pins its
recovery anchor before git runs and enters the size gate on that same head, and the gate records the rebased commit
as one still owed a push before the push goes out — so a process that dies in between leaves the anchor and the
approval together. Read as a freeze, the approval takes the branch out of the very recovery the anchor exists for:
the reconciliation ahead of the next handler would land the push while nothing ever cleared the anchor, reset
`review_round`, or routed the reviewer at the rewritten head. So an approval whose `late_approved_lease` IS the
pinned `pending_auto_base_rebase_push_sha` does not freeze the branch, and the refresh finishes the route it
started. Nothing else writes either field while an anchor is outstanding, and a group too damaged to show its lease
fails the test and keeps the freeze. The reading group is never set aside: a generation the gate froze and did not
answer is a question no push settles.

The reconciliation stands down for the other half of that same pairing, and it has to: the refresh reads the pull
request before it will run a recovery, so a `get_pr` that fails leaves the anchor pinned with the debt beside it
and the recovery deferred to a later tick. Reached then, this owner would pay that debt as an ordinary approval —
publishing the replay and settling it — while the finish that clears the anchor, resets `review_round`, and routes
the reviewer at the rewritten head never happens, and the stage would run over a branch the refresh rewrote with
the round the reviewer spent before the rewrite. So an anchor still on the comment stops the tick outright: nothing
is published, nothing is written, and nothing is parked for a read that will answer on the next one. That holds
whether or not there is anything else to see — the windows leaving no debt and no count are the same refusal with
less on the comment, and answering "nothing owed" there lets the handler run behind the same unfinished recovery.
It is the fail-closed half of the same rule — the interrupted rebase owns what it recorded until its own recovery
finishes it or parks.

Unless the refresh is standing down for the very record the reconciliation is holding, and there deferring is a
DEADLOCK rather than a courtesy. The freeze above sets one thing aside for this anchor and one only, the approval
leased to it; every other record it reads holds the branch still. So a pair the gate froze and never counted stops
the refresh on every tick — and a reconciliation waiting for that recovery waits for a tick that cannot come, while
the same stalemate repeats. The question is therefore asked of the freeze itself rather than re-derived: a comment
carrying nothing that holds the branch is one the recovery can reach and the reconciliation defers to it, and a
comment that holds the branch is the reconciliation's own to answer.

A pull request that is no longer OPEN ends the pairing the other way. Nothing can be pushed onto a merged or closed
one, so the debt is unpayable and the permission beside it is a claim about a push that will never happen — and
left standing, the debt is exactly what this reconciliation would try to pay, parking the issue on a publication it
cannot even enter while the stage that would finalize the merge to `done` never runs. So the refresh retires the
whole handoff in the write that drops the anchor: the attempt, the approval whatever it is leased to (nothing else
writes one while an anchor is outstanding), and an `authorized` permission made over the head the anchor names. A
`published` one is kept, because it describes a transfer that already happened and an exemption the merge carried
with it.

`late_exempt_sha` and `implementing_published_sha` freeze the branch too, but on conditions rather than on their
presence: neither is ended by a write — the exemption is never cleared at all and the publication record is
overwritten rather than spent — so read by presence they would take a branch out of the refresh for the rest of its
issue's life. The refresh asks two things instead. The checkout: the head still IS the commit the record names, or
there is nothing there left to protect. And the label: the stage that has to act on that commit — `implementing` for
both, plus `decomposing` for an exemption a relabel has not carried out of it yet — still has the issue. Past the
handoff neither holds anything, and that is the point: a pushed branch is kept in step with base by the PR-aware
sync, which is the only route that can move it without stranding the SHA a reviewer is looking at. That rebase is a
REWRITE of whatever the branch was standing on, so where it was standing on an exempt commit the refresh hands the
size gate the same evidence a squash does — the pair the adjudication recorded, the pair the replay produced, and
the pull request and pre-rebase anchor the push is made against — and the transfer above may carry the verdict onto
the replay. A base advance that CHANGED what the branch adds to it fingerprints differently, so the permit refuses
and the ordinary cumulative gate measures the replay like any other candidate.

A checkout standing exactly where the attempt anchored it takes the shortcut back to the ordinary rebase flow
only where the attempt left nothing else behind — no record of a replay and no permission this build reads as
outstanding or cannot vouch for. Anything else is an attempt that got a long way and was UNDONE: a reset whose park
write was lost, or a hand at the checkout. There the reset is re-run onto the commit the branch is already on, so
the debt for a commit no branch has and the permission that will never be spent on it go with it on the rollback's
own terms, and the issue parks for a human to say what undid it. Dropping the anchor instead would throw away the
only thing that brings a recovery back and hand the branch to a fresh rebase with the transfer state still
standing.

That rebase is six durable moments in a row — the anchor, the rewrite, the permission, the push, the receipt, and
the route — so the crash recovery behind `pending_auto_base_rebase_push_sha` classifies TWO things rather than one.
Where the remote stands says which effect the dead tick reached, and it is read as an exact SHA: still on the pinned
anchor and the push never went out, on the rewritten commit and it did, anywhere else and somebody moved the branch
out of band. The ahead/behind counts answer only that third case. A rebase REPLAYS the branch, so the commit the
pull request still carries is an object no local history contains afterwards — git reports the branch as behind its
own publication as well as ahead of it — and a recovery reading those counts first would park the canonical
pre-push case as a remote somebody else moved. Remote-old is therefore proved by BOTH pinned heads together: the
remote standing exactly on the anchor the `--force-with-lease` is pinned to, and the checkout standing exactly on
the replay `pending_auto_base_rebase_rewrite_sha` records. The anchor alone would let a worktree somebody rebuilt,
an operator's reset, or a branch pointed at other work be force-pushed over the candidate under a lease every one
of them satisfies. Remote-new is proved by the same record: refs that agree prove only that they agree, and
somebody who moved the branch and the remote together leaves exactly that shape, so a landing the attempt cannot
show it made parks rather than finishing the route.

One window has no id to be proved by at all, and it is the narrowest: `git rebase` returned and the write naming
what it produced did not. What stands on the comment there is the anchor and the TERMS the attempt was entered
under, and what stands in the checkout is a replay nothing names. The head is offered to the permit instead of
asserted — the transfer evidence is assembled over the dead tick's own publication, and the permit re-fingerprints
what the checkout contributes against the pair the adjudication recorded before it licenses anything. A replay of
the accepted change proves out; a worktree somebody rebuilt does not, and neither does a partly-applied rebase. The
push behind that road is permitted or it does not happen: a refusal parks rather than falling through to the
cumulative reading, because a count says how big a change is and never whose it is. Where no verdict on the issue
can be re-fingerprinted at all, the branch is reset onto the anchor and the issue parks with the replay left in
`git reflog` — the rebase costs one tick to make again, and publishing an unprovable head costs the pull request.

The pull request and the stage are reconciled ahead of every road, because finishing one is never silent: the
notice goes to the pull request this tick holds, the audit event is filed under the stage this tick reads, and the
anchor that is the only thing bringing the tick back is dropped. An attempt recorded against a publication the
issue no longer records would have all three attributed to work it was never made for, so it parks with HEAD and
the whole record left exactly where they are — which pull request the branch belongs to is a question about the
issue's record rather than about the commit, and throwing the replay away would answer neither.

A relabel out of the refresh-driven set entirely — `workflow:implementing`, `workflow:decomposing`, anything this
sync does not drive — leaves no road at all, since nothing here fetches, compares, or publishes under a label it
does not own. What is left is a clear or a park, and the local HEAD is read (no fetch, no request) to decide which.
An anchor over a checkout still standing ON it is dropped: git never moved the branch, so the anchor is only a
promise to come back that no tick under the new label is coming for. An attempt that got further is not. A recorded
replay, or a permission granted for a push nobody made, is state the clear cannot honour — the checkout may be
standing on a rewrite the pull request has never seen, a human's verdict is licensed onto a commit no push carried,
and the debt beside it says a publication is still owed. So is a checkout git has ALREADY moved with only the terms
pinned, which is the window between `git rebase` returning and the write that names what it produced: the terms
cannot tell it from an attempt that never started, and a head this host cannot read is no evidence of either.
Dropped, those records come apart from one another and the issue reads exactly like one with nothing in flight, so
a decomposition tick is free to put a second agent on a change a human already ruled on — while the rewrite stays
on the branch with nothing naming it. So each of them parks instead, with nothing reset and nothing cleared — the
hand that moved the label may have moved the checkout too — and an operator putting the label back lets the
ordinary recovery finish the attempt on its own terms.

That park is taken ONCE. Keeping the record is what brings this route back, and the route is reached from the label
check ahead of every gate, so every poll under the wrong label arrives at the same comment again. Repeated, it
fills the thread with one sentence and — the part that actually breaks the recovery — each park ratchets
`last_action_comment_id` forward, so the operator's reply ends up behind the orchestrator's own newest comment and
the retry scan never sees it. A park this route already left standing is therefore left completely alone: nothing
posted, nothing written, nothing ratcheted. It is recognized by `awaiting_human` and `auto_base_rebase_failed`
standing over an anchor that is still pinned, which only this park leaves — every other road ending on that reason
resets the branch and clears the attempt first.

One remote-old shape is not a pre-push recovery at all, and it is read off the record too. Where a receipt names
the checkout's commit as pushed from this anchor — or a transfer settled, which rides the same write — the pull
request HAD this replay, so a pull request standing on the anchor now was rolled back out of band. The head it was
rolled back to is the very head a reissued push would lease itself against, so the lease would be satisfied and
the rollback overwritten. That parks as the externally moved remote it is: HEAD back onto the anchor, the record
dropped with it, and a human asked which of the two heads the branch is supposed to be on.

What the pinned comment carries says how far the transfer's own writes got, and there are five answers — with the
exemption and the identity under it asked before any permission standing beside them is believed, since a
permission is a claim about moving one verdict and a group damaged after the grant went down leaves it reading back
whole over a verdict nothing can name.

No exemption in flight is the ordinary interrupted rebase. A rewrite with no `late_rewrite_*` group behind it is a
grant the crash came before, so the recovery re-derives the same evidence the dead tick would have assembled —
over the pull request and stage that tick recorded — and the permit rules on it. Without
that, the replay of a change a human already ruled on is measured past the same ceiling and routed back into
adjudication with a pull request open over the work. A group still at `authorized` for the head in hand, with the debt
written beside it agreeing and every term of it bound to the attempt this recovery is finishing — the pull request
and the stage that attempt recorded, the anchor its push was leased against, and the pair and digest the semantic
identity names — IS the evidence, re-asked in full — the grant is one write of two records for one
commit, and a permission standing beside a debt for another commit or another lease is a comment something took
apart. Read as outstanding it would be re-asked, and a permit that grants re-writes BOTH, so the missing half would
be reconstructed from the very claim nobody could check.

One already `published` means the receipt landed and only the notice, the audit event, the cleared anchor, and the
reviewer's route are outstanding — and it is cross-bound the same way, since fields that are each well-shaped on
their own still describe some other attempt when they disagree with the one in hand.

And a group this build cannot read whole, or one naming some other commit, is refused before either road that
publishes anything: left to the ordinary cumulative
gate the replay is measured past the same ceiling and routed into a second adjudication on the strength of a record
nothing checked, so the branch goes back onto the anchor and the issue parks as `auto_base_rebase_failed` until a
human repairs the comment.

The exemption itself is read the same way, by PRESENCE rather than by truth. The fail-closed readers answer "no
exemption" and "no identity" for a group something damaged exactly as they do for a comment that never carried one
— rightly, since the gate's only move is to measure — and a recovery that took that for "no verdict in flight"
would finish a route with the verdict still on the commit it was given for. So a comment claiming an exemption it
cannot show whole, or an identity group short of a member, is that same refusal. The LEGACY shape is not: an
exemption with no identity beside it is complete for what it says, costs a later tick the transfer rather than the
verdict, and goes on exempting the commit it names.

The permission still outstanding over a remote that already carries the rewrite is the one that owes a WRITE, and
the recovery takes it rather than leaving it for the stage it is about to relabel to: the permit is scoped to the
stage the rewrite was entered from, so a settlement one tick later is refused on the stage alone. It is taken
through the same gated publication every other push in this domain goes through, which over a pull request already
standing on the commit is the leased no-op that proves it — nothing sent, no agent, no measurement, no second
adjudication comment, and the receipt, the paid debt, and the rotation riding one durable write under
`transfer_proof=already_published`.

The permit is asked for that settlement BEFORE the gate, and a refusal is a refusal rather than a fall-through.
The gate's own answer to a declining permit is the ordinary cumulative reading, which is right for a rebase
deciding whether to publish and wrong here twice over: a count under the ceiling would report the call as a landed
publication and let the route finish with the permission outstanding and the verdict still on the commit a human
ruled on, and a count over it would route an adjudicated change into a second adjudication with the pull request
already carrying the work. There is nothing on this road to measure — the remote has the commit — so a relabelled
issue, a repointed pull request, an owner nobody could re-read, a lease this host cannot peel, or two
contributions that no longer fingerprint alike each park instead. The gate is told the same thing on the way in, so
a permit that stops holding between the two asks is refused there rather than measured. The reissued push a
remote-old recovery makes is decided the same way wherever it holds a transfer at all — an outstanding permission,
or evidence re-derived for a grant the crash came before — and a refusal there resets the branch onto the anchor
rather than measuring a change a human already ruled on. That exclusivity outranks the `DECOMPOSE` switch as well.
Off, the switch takes new work straight past the gate unread, which is the whole of what it is for — and a recovery
holding a transfer is not new work: kept out, it would push with nothing vouching for the move, finish its route
with the exemption still on the commit a human ruled on, and leave the permission standing outstanding for ever. So
a permit-only call is inside the gate whatever the switch says, and publishes on the permit or not at all.

A dirty checkout holds every one of those roads, not only the one that still owes a receipt: finishing hands the
issue to the reviewer, and a reviewer sent to a checkout with uncommitted files reads work the pull request does
not have as though it were under review. The rotation is read back afterwards for the
same reason: the terms are re-asked inside the gate, and a push that landed without the verdict moving with it is
a route this recovery may not finish.

Every other landed rewrite is asked whether the comment ACCOUNTS for it before the route is finished, because
finishing clears the anchor and the anchor is the only thing that brings this recovery back. An issue carrying no
verdict always is, which is the ordinary interrupted rebase and has nothing a missing record could strand. A transfer
that settled, and a replay the ordinary cumulative gate published, are accounted for by the receipt their write
left — read WHOLE, as `implementing_published_sha` together with the `implementing_published_lease` it was pushed
from, held against this recovery's own anchor. A receipt is never cleared, so the commit alone goes on naming
something this stage pushed rounds ago and vouches for any pull request somebody rewound onto it; the head is what
dates it to THIS attempt. The debt beside it is asked too — the two go down together, so an approval still standing
over a receipted commit is a write that did not land whole. Anything else parks with HEAD and the anchor left exactly as
they are: a `late_rewrite_*` group nobody can read, a receipt nobody wrote, a debt nothing paid, a checkout
carrying uncommitted changes the settlement may not be fingerprinted beside, and a leased no-op the remote refused.
Nothing is reset on any of them — the checkout is standing on the commit the pull request carries, so a reset would
take it off work the remote has — and the next tick classifies the remote afresh once a human has answered.

Refresh-only failure modes — push rejected (`auto_base_rebase_push_failed`), rebase failed without conflicted files
(`auto_base_rebase_failed`), dirty-after-clean-rebase (`auto_base_rebase_dirty`) — reset HEAD back to the pre-rebase
SHA and park awaiting human with a durable `park_reason`. Recovery is refresh-only and gated on a fresh human
issue-thread comment past `last_action_comment_id`; the actual `awaiting_human` / `park_reason` clear is deferred to the
same pinned-state write that publishes real progress, so an early-return path cannot silently drop the retry intent.
Every PR-stage handler short-circuits at its `awaiting_human` gate when `park_reason in _AUTO_REBASE_PARK_REASONS` so
the refresh owns the operator's retry comment.

Before rebasing, the flow fetches `gh.get_pr(pr_number)` and skips when `pr_state != "open"`: a just-merged PR advances
`<remote>/<base>`, so the stale worktree is naturally behind base; without this gate the refresh would push and relabel
a PR the next handler would finalize. A `gh.get_pr` failure is treated as "leave alone".

### Pollable issues and finalization

`gh.list_pollable_issues()` yields all open non-PR issues plus closed non-PR issues still labeled with one of the
eight recovery sweep labels — `workflow:implementing`, `workflow:documenting`, `workflow:validating`, `in_review`,
`workflow:fixing`, `workflow:resolving_conflict`, `question`, `discussion` — or one of the four cleanup ones,
`workflow:decomposing`, `workflow:umbrella`, `workflow:ready`, and `workflow:blocked`. Each of the seven that HAS a
pre-namespace spelling is queried under
that too, because a closed issue is the one case no other pass revisits: on a repository whose labels the
bootstrap could not rename (see
[Legacy labels and the migration off them](#legacy-labels-and-the-migration-off-them)), the bare label is
all that is left to find it by. Both queries feed one seen-number set, so an issue carrying both spellings is yielded
once. The closed-issue sweep makes external manual merges and operator closes finalize cleanly:
- Closed `in_review` / `workflow:fixing` / `workflow:resolving_conflict` — a human-merged PR with a `Resolves #N`
  footer auto-closes the issue before the orchestrator can flip the label.
- Closed `workflow:implementing` / `workflow:documenting` / `workflow:validating` — the same external-merge race when
  the human merges before reaching `in_review`. Each handler's entry-time `_finalize_if_pr_merged` flips to `done`
  instead of stranding the issue.
- Closed `question` — a human closing the issue is the terminal signal
  [`_handle_question`](conversation-stages.md#_handle_question-label-question) consumes to finalize to
  `done`.
- Closed `discussion` — two different endings, and the label is swept for a longer window than the rest. With no plan
  PR published, the close is the whole signal and
  [`_handle_discussion`](conversation-stages.md#_handle_discussion-label-discussion) finalizes to `rejected`,
  which is what takes
  the issue back out of the sweep. WITH one, the close says nothing about the design: the stage holds its terminal
  and keeps the `discussion` label precisely so this sweep goes on yielding the issue until the plan PR itself
  merges (`done`) or closes unmerged (`rejected`). Nothing else revisits a closed issue, so a terminal flip while
  that PR is open would strand the worktree and the branches the plan lives on.

`workflow:ready` and `workflow:blocked` are swept closed for **cleanup only**, and for one reason: a decomposition
outcome writes one of them, and a run spawned before its owner was observed closed lands *after* that observation. So
a close that was latched and receipted while the owner was still `workflow:decomposing` can end up on an issue closed
under one of these — with the ending unmarked, the ref its children were cut from still held, and the latch that
would route it living only in the process that took it. A restart before any cleanup pass would lose that ending for
good, so the query is what makes it discoverable without one. Only their CLOSED issues are asked about: an open
`workflow:ready` issue is polled and dispatched exactly as ever, and is not refetched the way an open owner on one of
the two adjudication labels is. What it costs is one pinned read per closed issue on either, on the sweep cadence.

`workflow:decomposing` and `workflow:umbrella` are swept closed for the same pass, and all four are the one case
where the label does not choose the handler -- and the one case the `backlog` / `paused` filter does not get to drop,
since discarding a closed owner there would discard the close itself and leave a live generation to spawn against
after a reopen and an unpause. The control label defers what the pass would DO; the mark still lands. A split records
what it owes the remote on the closing issue's own generation ledger — the branch its superseded candidate was
committed on, and the immutable snapshot ref its children were cut from — and an issue a human closes mid-cycle is one
nothing else would ever bring a tick back to. So the dispatcher reads *closed* first and routes both to the cleanup
sweep ([`delivery-stages.md`](delivery-stages.md#closed-owner-cleanup-sweep-no-label-of-its-own)) rather than to
`_handle_decomposing` or `_handle_umbrella`, which would spawn the decomposer or activate children on an issue whose
close was a decision to stop. The pass ends the cycle: it marks the cancellation, closes any pull request that
cycle was holding, re-reads every recorded snapshot consumer, settles what can be settled, and moves the issue to
`rejected` once nothing is owed. Being routed there at all is what says a close was *observed*, so an issue the pass
finds open again — reopened between the poll and the worker's refetch — is marked cancelled and stopped there, with
the ending left to the dispatcher's own guard from the next tick. It does nothing else — no agent, no activation, and
no child of the split touched. `rejected` is the only label it ever writes, and it is what takes the issue out of this
sweep for good. It rides the same sweep walk, the same cadence, the same label cache, and the same absent-label
throttle as the recovery labels above. It is partitioned as **fan-out** rather than into the family bucket, and
submitted `cap_exempt=True` on its own: the bucket's exemption is all-or-nothing, so one open `workflow:decomposing`
issue sharing the tick would make a closed owner cap-counted and, under a saturated cap, skipped — which would stop
the repository reclaiming refs for as long as its decomposer stayed busy.

- Closed `workflow:decomposing` / `workflow:umbrella` / `workflow:ready` / `workflow:blocked` — a snapshot owner a
  human closed mid-cycle, or one whose own decomposition outcome landed after that close. Its generation
  ledger may still hold a superseded branch, an immutable snapshot ref, and a pull request under a hold, and no
  other pass revisits a closed issue. It is the one sweep entry that resumes no workflow: what it ends is the late
  cycle, and it reaches a terminal only once that cycle owes the remote nothing (see above). An issue on any of the
  four with no late cycle at all costs the pass its one pinned read and nothing else — it is not written to, not
  relabelled, and not commented on.

The closed-issue sweep issues one closed-issue query per sweep label the repository actually carries, per repo, every
tick — a fixed request cost that drives GitHub primary-rate-limit exhaustion on multi-repo hosts. The four cleanup
labels ride that same walk rather than a second pass of their own, so what they add is four label lookups and four
closed-issue queries on the ticks the sweep already runs, and nothing at all in between. A pre-namespace
spelling the rename already retired costs only its `GET …/labels/<name>` miss, and even that is thrown away for
twenty sweeps before being asked again rather than re-requested every pass. The spellings one sweep confirms absent
are reported together, in a single repo-qualified INFO line naming them, so a migrated multi-repo host does not open
with a burst of near-identical lines that reads like broken configuration; a sweep whose legacy lookups all came from
the throttle confirms nothing and logs nothing, so that line recurs when the window expires rather than every pass. A
missing namespaced label, or a lookup that failed any other way — a 403 is no answer about whether the label exists —
stays a per-label warning.
`CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` (default `1`) batches the whole sweep to once every N ticks; the open-issue poll is
unaffected, so the only effect of `N>1` is that an externally-merged/closed issue can take up to `N-1` extra ticks to
finalize. See [configuration.md#github-rate-limits](../configuration.md#github-rate-limits).

`done` is queried by neither sweep, and nothing needs it to be: an umbrella's terminal makes the retirement durable
one write *before* the label, so no `done` issue is ever left carrying a late cycle. A crash before that label lands
leaves the owner on `workflow:umbrella` with the resolution recorded, which the closed-owner sweep finishes.

`done` and `rejected` are terminal no-ops. Every handler receives the active `RepoSpec`, so `git worktree add`,
`git fetch <spec.remote_name> <spec.base_branch>`, push-token resolution, and PR-base selection all flow from the spec.

### Pinned state

Per-issue durable state lives in a single **pinned comment** on the issue (`<!--orchestrator-state {...json...}-->`).
The schema is defined by `read_pinned_state` / `write_pinned_state` (see `github.pinned_state.PINNED_STATE_MARKER` /
`PINNED_STATE_RE`). `read_pinned_state` trusts a comment as state only when it is authored by the account backing the
orchestrator's token AND its whole body is the marker, so neither a third party's forged marker nor an ordinary
bot-authored comment that embeds the marker in prose can preempt state (see
[pinned-state authentication](../security.md#pinned-state-authentication)).

A comment that passes both checks is the state comment whatever its payload turns out to be. One that will not parse,
or that parses into anything but a JSON object — `[]`, `7`, `null`, from a truncated write or a hand edit — is refused
rather than handed on: the state reads back empty and flagged unparsed, so a reader deciding on the absence of a
recorded branch or pull request can tell it from an issue that pinned nothing (both carry `{}`), and the comment id is
kept, so the next write replaces the corruption in place instead of leaving a second pinned comment beside it.

The keys that matter for the state machine fall into a few groups:

- **Agent identity.** `dev_agent` + `dev_session_id` (locked dev session — see
  [in-flight session lock](../workflow/command-specs.md#in-flight-session-lock)),
  `review_agent` (traceability only; reviewer is fresh per round), `decomposer_agent` + `decomposer_session_id`
  (parents), `question_agent` + `question_session_id` (`question` stage), `discussion_agent` +
  `discussion_session_id` (`discussion` stage), `late_agent` + `late_session_id` (the late adjudication of an
  oversized committed candidate — see [the late run](#the-late-run) below). The last four pairs are separate pins on
  purpose: each seeds from `DECOMPOSE_AGENT` on its own first spawn and is then locked independently of the others on
  the same issue, so a flip of `DECOMPOSE_AGENT` between two rounds can neither move a conversation onto a backend
  that never ran it nor hand that backend a session id it never issued. The three conversation pairs also *resume*
  their own session id on a human reply — with the decomposer pair excepted on an issue stopped by its spawn budget,
  where no reply resumes anything and the renewal that lifts the park retires the session it would have replayed
  ([The retry budget](#the-retry-budget)) — and the late pair resumes on exactly one: a substantive trusted answer to
  the categorized question the adjudicator asked, which is a reply to the agent that asked it. Every other late run is a
  fresh conversation against the frozen candidate — see [the late run](#the-late-run) for the two conditions a resume
  takes.
- **Decomposition.** `children`, `dep_graph` (`{child_idx_str: [child_idx, ...]}` — GitHub has no first-class blocks
  relation), `decomposed_at`, `pickup_comment_id`.
- **A debt with no record behind it.** `late_approved_sha` + `late_approved_lease` outlive the generation that
  granted them, because the write that approves a candidate retires that generation before the push. Where the lease
  is set the approval was taken over a pull request the remote already carries, and the dispatcher pays it ahead of
  every handler rather than leaving a stage to run over a publication the commit never joined — but only from a
  checkout still standing on the commit, and never while the issue is under `workflow:decomposing`, where the
  settlement owns the push. A checkout that is absent, unreadable, or standing elsewhere parks instead. The pair is
  dropped by whatever settles the commit: the push that lands it, an approval superseded, a hold that routes it to
  the adjudication, or a reset that sends the branch back off it — the auto rebase's after a refused push, and the
  squash's own rollback after one.
- **A held pair's continuation.** `late_spends` records what the tick that froze a pair owed if its hold went
  through — the reviewer round a fix spends, the bookmarks a consumed batch clears, the head a finished docs pass
  produced, the outcome a resolution earned — as `[[field, value], ...]`. It is one of `LATE_STATE_KEYS`, so it lives
  and dies with the generation it is about, and each member is bounded by the FIELD it names rather than by what a
  comment can carry: a round is a real non-negative count, a bookmark is only ever cleared, a settled head is a whole
  object id or none, an outcome is one bounded single-line name. A counter that came back as text would pass any
  looser check and fail at the `int(...)` the round cap is counted with, on a tick nobody is watching. One exception
  to living and dying with the generation: the write that APPROVES a small candidate retires that
  generation before the push it licenses runs, and puts these back inside the same write. Otherwise a push that misses
  leaves an approval the next tick can pay and nothing that says what paying it closes — the caller parked, and the
  stage it returns to short-circuits on that park. The reconciliation ahead of the next handler restores them for the
  same reason it restores an interrupted reading's: a tick with no run behind it could re-derive none of it. Whatever
  finally settles the approval drops them with it, so no later cycle inherits a round it was never owed — and a
  landed push settles it in the write that carries the receipt, so the fields go down with the publication rather
  than in a write behind it that a crash can take.
  Read back as ONE group, and bounded on both ends: the key has to name a field this workflow's routes actually
  close (`late_split/spends.py` spells that vocabulary as literals so the domain does not import the four stage
  packages that own the keys, and a guard test proves the two agree), and the value has to be one the pinned comment
  can carry. A single member that fails either refuses the whole group and `late_claims` parks on the raw key still
  being there — half-applied is worse than none, since the caller restoring the hold cannot tell which half it got:
  the round advances, the bookmark it was spent for stays pending, the record is discarded as paid, and the next
  re-entry reruns a developer over feedback that was already answered. On the allowed road the
  retirement drops this key in the same write that grants the approval, so the recovery reads it BEFORE its own push
  and the fields ride the receipt that push writes — one write, so no window exists in which the publication is
  recorded and the round it spent is not.
- **Conflict rounds.** `conflict_settled_outcome` + `conflict_settled_sha` name a resolution the size gate held —
  which of the four content updates it was (`agent_resolved` / `base_rebased_clean` / `recovered_push` /
  `drift_resolved`) and the head it produced. Written inside the
  routed hold's own write ahead of the relabel, and read back by the resumed `resolving_conflict` tick, which could
  not re-derive either: the settlement publishes the commit, so the branch it comes back to already carries its base
  and would be flipped as `base_up_to_date` — the one exit that resolves nothing and stamps no
  `last_conflict_resolved_at`. Dropped by whichever pushed-round tail finally pays the round.
  - **The replay a rebase made.** `conflict_replay_from_sha`, `conflict_replay_from_base_sha`,
  `conflict_replay_to_sha`, and `conflict_replay_pr_number` are what a `workflow:resolving_conflict` rebase records
  ABOUT ITSELF, because the tick that runs a replay is not always the tick that publishes one. The head it is about
  to replace, the fork point that head's contribution is read over, and the pull request it is being made against go
  down before the rebase runs — the first two because the rebase destroys them, the third because `pr_number` is a
  field a later tick can find pointing somewhere else and a rewrite is evidence about ONE publication. The commit it
  produced is stamped on once there is one, before the size gate is entered. Written only for a branch standing on
  the commit `late_exempt_sha` names, because nowhere else could a transfer ever be granted and a record there would
  be a request spent to protect nothing. Two readers, both on the tick that finds the replayed commit unpushed after
  a crash. The divergence guard asks it FIRST, because a replay moves the branch off the head it replayed and so
  comes back ahead of the publication and behind it -- the shape a stale checkout carrying somebody else's commit
  also has, which this stage parks. A record naming that exact head, that exact commit and that pull request is what
  tells the two apart, and it leases the force-push to the pre-rebase head. The recovered push
  (`conflicts/divergence._push_recovered_commits`) reads it again for the evidence it hands the gate: no probe of
  the branch tells a replay from a resolution an agent wrote or the unpushed fix commits the `fixing` drift reroute
  sends over — on base as readily as behind it — so the record is the only thing that can. It is acted on only where
  it is about the publication and the commit in hand: the pull request it names has to be the one the issue still
  records, the head it names has to be the head that push is leased against, and the commit it names has to be the
  one the checkout is standing on. Read live, that first one is the gap: a repointed `pr_number` would let this
  branch's replay be offered as a rewrite of some other open pull request, and one standing on the same head would
  satisfy every check the permit makes. The stamped commit is also what makes a stale group inert rather than
  dangerous, which is why clearing it is tidiness: the no-op flip and whichever tail publishes the replay drop it on
  writes they were already making, and never for a request of its own.
- **PR / branch.** `branch`, `pr_number`, `review_round`, `conflict_round`. The first two are also what a published
  discussion plan records, beside `discussion_plan_path` — the path of the Markdown file that PR carries. The stage
  reads the plan path and `pr_number` together as its "already published" gate, since an issue relabeled into
  `discussion` from a PR stage arrives carrying somebody else's `pr_number`. `discussion_publishing_sha` is the
  in-flight half of that record: the tip a publication was pushing, written before the push and retired by the write
  that records the PR, and the only thing that makes a plan-shaped commit on a parked issue's branch one this stage
  may finish rather than one it merely found there.
- **Drift baseline.** `user_content_hash` — SHA-256 over title + body + non-orchestrator comments; updated whenever
  the orchestrator reacts to a human edit.
- **HITL park.** `awaiting_human`, `last_action_comment_id`, `park_reason`. `_park_awaiting_human` (on the same
  `workflow/engine/guards.py` owner as the two run refusals) sets
  `awaiting_human=True` and clears `park_reason` to `None`; a handler that needs the reason to survive into the next
  tick explicitly re-sets it after the park call. `last_action_comment_id` doubles as the record that a mention was
  posted: a transient park that later self-recovers reads it back to decide whether it owes the thread a follow-up
  (see [`delivery-stages.md`](delivery-stages.md), **Recovery follow-up**). Park reasons that route via
  `_park_auto_rebase_failure` (`auto_base_rebase_failed` / `auto_base_rebase_dirty` /
  `auto_base_rebase_push_failed`) are owned by the per-tick
  base-sync flow — every PR-stage handler short-circuits when `park_reason in _AUTO_REBASE_PARK_REASONS`. The
  exhausted retry budget re-sets `retry_cap` for the same kind of reason — a park nothing can recognize is one the
  next tick re-decides from scratch (see [The retry budget](#the-retry-budget)), and the spent lifetime agent-run
  ledger re-sets `agent_run_limit` for the same reason again, since the dispatcher's hold over it reads that flag and
  nothing else (see the **agent-run-limit park** bullet below). A failed squash-on-approval re-sets `squash_failed`,
  and what reads it back is the recovery that took it: that route retries on every tick and stays silent while its
  own reason stands, so the reason is what tells a notice already on the thread from one to post afresh — and a park
  worded by the size gate behind it, which says its own piece on every reading it cannot take, is held for a human
  rather than re-entered. The late
  size gate re-sets its own reasons for the same kind of reason: `late_measurement_failed`,
  `late_candidate_moved`, `late_evidence_missing`, `late_plan_pr_hold_failed`,
  `late_generation_incomplete`, `late_worktree_missing`, `late_worktree_mutated`, `late_adjudicator_timeout`,
  `late_manifest_invalid`, `late_result_unrecordable`, `late_owner_unreadable`, `late_pr_unreconciled`,
  `late_snapshot_failed`, `late_children_failed`, `late_supersession_failed`, `late_content_drift`,
  `late_revision_dirty`, `late_revision_unmeasured`, `late_revision_unanswered`, and `late_question` — see
  [the late run](#the-late-run) for which of them the next attempt retires. A late tick can also hand the issue back
  under the shared `retry_cap`, which is not one of its own: the adjudication is charged to the same per-issue spawn
  budget every other agent run is, and a refusal there means the issue's day of tokens is spent rather than anything
  about its candidate. It is staged through the same late owner all the same, so the generation and the hold ride
  its write, and it is answered where every other `retry_cap` park is. `late_measurement_failed` is the only one
  of them taken outside `workflow:decomposing`, because it is the gate's own and the gate runs before any
  adjudication exists — under `workflow:implementing` where the push would open the pull request, and under any of
  the five states that push onto one the remote already carries. It is answered wherever it was taken, one step
  ahead of the generic continue
  classifier, since a content-free `/orchestrator continue` on it means "take the reading again" rather than the
  guidance a park needing a real answer would be refused for. It is also the one of them a tick can retire with no
  answer at all, and under a label it is never taken on: a park standing over a record whose split has already
  become children is the
  reconciliation's own false positive — what a settled split keeps the publication group for is the releases and the
  branch delete its umbrella still owes, not a reading anybody is waiting on — so the guard clears it under
  `workflow:umbrella` and lets that handler run (see
  [`delivery-stages.md`](delivery-stages.md#the-size-gate-on-a-published-pull-request-every-push-onto-an-open-pr)).
  `late_candidate_moved` is the second taken outside the
  adjudication, and it is the publication's own: the checkout is not the one the gate approved, so
  nothing is pushed and the issue is not handed on. It reaches the same five states for the same reason — every
  gated push proves its checkout again on the far side of the effect, not just the one that opens the pull
  request. Two readings answer for "the checkout", because the head answers
  only half of what it means. A head somewhere else is one. A tree carrying work no push would publish — or one
  `git status` could not report on, which is not a clean tree but a reading that never happened — is the other, and
  it is the half that can be true with the head never having moved, so every proof about the commit passes over it.
  Both are asked before the push and again once the pull request is open. It has a reason of its own because the
  remedy is neither a retry nor a re-measurement — every stage past the handoff works from that checkout and none of
  them measures again, so what it asks for is the worktree back on the approved commit and carrying nothing else,
  and a worktree deliberately left on the descendant is measured as the fresh candidate it is on the next run. The
  same reason covers the pre-spawn refusal one step earlier: an approved commit this host
  cannot show at all — the checkout was rebuilt from the base or the plan pull request on a replacement machine — is
  the same ask with nothing to compare against, so neither the recovered-worktree shortcut nor a fresh developer run
  is allowed to proceed past it. It is also the one park answered by something other than a comment: the approved
  commit is recorded as `late_approved_sha`, every tick asks the checkout one local `rev-parse` against it and one
  `git status` around it, and a checkout put back — on that commit, with nothing loose beside it — publishes on the
  next poll with nothing re-run and no agent spawned. Both questions are asked, or the recovery would republish into
  the very refusal the park was taken on and post a fresh notice every poll for a checkout that has not changed.
  That record is what makes
  the answer possible at all — the generation is retired ahead of the effects it licenses, so once the approval lands
  nothing else on the issue still names the commit — and the read is silent, so an operator who leaves the checkout
  where it is is not told the same thing once a tick. `late_evidence_missing` is the adjudication's counterpart, taken
  under `workflow:decomposing` before the hold or any spawn: the checkout is there and one of the two recorded
  commits is not, so the agent would be shown a `git diff <base>...<candidate>` that cannot resolve and its verdict
  would be an answer about nothing. It asks for the worktree at the recorded commit, never another run.
  `late_owner_unreadable` is the one of them that recovers on its own: it is a GitHub read that failed after the
  agent had already answered, so the retry re-reads rather than re-running anything, and the tick that finds the
  issue readable again posts the same one-time follow-up a transient `validating` park does — before the write
  that clears the park, so a crash between them loses the write and not the sentence. What drives that retry is
  `late_owner_check_pending` on the generation rather than the park itself, which is also why this reason is taken
  only when the issue is not already parked on something a human has to answer. On an issue that already is, the
  notice that other park staged is still said when the reason it stands on is one no later attempt supersedes —
  the four revision and drift parks — since nothing else ever would, and an `awaiting_human` with no sentence
  behind it stands for as long as the read keeps failing.
- **Undelivered park notice.** `late_park_notice` is the `{reason, message}` a late park has recorded and not yet
  said. The flag is durable before the comment is posted — a comment GitHub refuses must not take a finished run's
  result with it — so without this field a refused post leaves an `awaiting_human` nothing can tell from one whose
  comment landed, and every later tick reads the flag, takes the human as told, and says nothing. It is written beside
  the flag on the same write and dropped by the post that discharges it, so a park whose sentence is still owed is
  never counted as a repeat and is re-said at the top of the next eligible tick. It is matched against the standing
  `park_reason` (a notice for a park something has replaced or answered is dropped rather than said), left to the
  fresh attempt for the reasons that attempt supersedes, dropped when the cycle is cancelled, and refused whole —
  loudly — when it would not fit the pinned comment, the same budget a recorded outcome is refused past. Not one of
  `LATE_STATE_KEYS`: a park outlives the generation that took it. It carries the shared spawn budget's `retry_cap`
  sentence too when a late adjudication is what ran out — that park is taken by this mode's owner, so it is this field
  rather than `retry_cap_notice` that holds what it owes, and the entry replay under `workflow:decomposing` finds
  nothing to say for it because the redelivery below is what says it. The converse is the one an old record leaves: an
  issue parked on `retry_cap_notice` under this label — by the shared parking form, before it entered the size gate or
  before this owner existed — is said by that entry replay instead, so the late spent-budget hold treats an obligation
  on **either** field as a park the thread has not been told about. Owned by
  [`late_notice`](../../orchestrator/workflow/stages/decomposition/late_notice.py). It is a claim about the thread, so
  the thread settles a disagreement with it. The post and the write recording it cannot be one operation, so a write
  that failed after a post that landed leaves the field claiming a sentence is owed to an issue that already has it —
  and the first thing a tick does is look for that sentence among the comments above `last_action_comment_id` (the
  mark a park's own mention ratchets, and only on a write that landed, which is what scopes the search to this
  episode). One found there discharges the obligation and repairs the watermark to it. Without that step the
  redelivery would repeat a comment, and — worse — the owner guard would read the standing obligation as proof nobody
  was told and clear its park without the recovery follow-up it promises. A read that could not be TAKEN answers here
  exactly as an empty one does, which is the opposite of the shared field's reading and is this mode's own choice: the
  sentence is said again, costing one repeated comment rather than risking a park that stands unexplained for as long
  as the read keeps failing. What that buys is the property every late park hangs on — the notice reaches the thread
  before anything on that thread is read as an answer to it.
- **In-review watermarks.** `pr_last_comment_id` (issue thread + PR conversation, shared IssueComment id space),
  `pr_last_review_comment_id` (inline PR review comments), `pr_last_review_summary_id` (PR review summary bodies). Only
  non-empty `CHANGES_REQUESTED` or `COMMENTED` review IDs ever advance the summary watermark; `APPROVED`, `DISMISSED`,
  `PENDING`, and empty-body reviews are filtered before the bump.
- **Final-docs handoff.** `docs_checked_sha` + `docs_verdict` (`updated` / `no_change`) set by `_handle_documenting`'s
  success exits, and the verdict an earlier pass left is dropped as the next one begins — every entry shape re-anchors
  `docs_checked_sha` to the head it is about, so a stale verdict beside it would say a pass has finished for a head one
  is only starting on, which the in_review merge gate reads as a head this orchestrator has documented and pings. The
  success exits announce first, then persist, then relabel: the notice's id has to ride a write or nothing
  records it, and `in_review` repairs nothing it is handed — its merge gate pings only for a head `docs_checked_sha`
  names with a `docs_verdict` beside it, so a relabel taken ahead of the write would strand the issue there on the
  head the pass began on. `docs_settled_sha` is the head a docs pass produced, written inside the size gate's own
  routed write ahead of that gate's relabel to `workflow:decomposing`: the pass is finished and only the `in_review`
  handoff is still owed, so the tick a settled verdict hands the label back to finishes from the receipt rather than
  reading a branch in sync with its remote as an issue no docs pass has run for. The gate writes it whichever way the
  answer went, so it covers the other tick that can leave a handoff owed — a push the gate ALLOWED, which landed,
  whose process died before that write could record the pass. Read back only over a checkout standing ON the head it
  names — in sync is what a replacement host rebuilt at a moved pull request reads as too. Dropped by every terminal
  docs success in the same write that records it, including a republication that carries the held commit to the
  remote itself — held past that write to cover the relabel behind it, the receipt outlives the handoff whenever the
  write that would drop it does not land, and a later `validating` approval at the same head consumes it and skips
  the docs pass it just bought. So the relabel window keeps no receipt, and it does not need one: what it leaves is
  the record a same-head approval leaves, and the next tick runs the pass rather than handing off on evidence that
  could belong to either.
  `ready_ping_sha` records the head the in_review handler already posted a `:bell:` HITL ping for.
  `docs_drift_unwind_pending` is set while `_handle_documenting`'s drift block is reconciling and cleared only on the
  relabel back to `workflow:validating`.
- **Fix routing.** `pending_fix_at` + per-namespace `pending_fix_issue_max_id` / `pending_fix_review_max_id` /
  `pending_fix_review_summary_max_id` recorded by the `in_review → fixing` route, plus the full
  `pending_fix_issue_ids` / `pending_fix_review_ids` / `pending_fix_review_summary_ids` batch lists. They are hints, not
  watermarks — the in_review watermarks are deliberately left behind so the `fixing` rescan can re-discover the
  triggering comments, and the id lists let `_reconstruct_pending_fix_batch` rebuild the exact triggering batch after
  the watermarks advance past it (falling back conservatively to the max ids for issues parked before the lists were
  recorded). The `validating → fixing` route instead records a single `pending_fix_reviewer_comment_id` — the id of the
  PR conversation comment carrying the reviewer's CHANGES_REQUESTED feedback — and does NOT set `pending_fix_at` (that
  key is the route discriminator that drives the review-round reset). `_reconstruct_pending_fix_batch` re-fetches that
  exact comment by id (outside `filter_trusted`, since it is the orchestrator's own reviewer output the author allowlist
  would otherwise drop) as the validating-route replay anchor. The rebuilt batch is what the `/orchestrator continue`
  operator command replays when retrying a session-failure park (see
  [`_handle_fixing`](delivery-stages.md#_handle_fixing-label-workflowfixing)); the anchor is cleared on a
  pushed fix and inside `_clear_pending_fix_bookmarks`.
- **Crash-recovery anchors.** `discussion_round_branch` + `discussion_round_sha` — the branch a discussion round
  opened on and the SHA it was at, written BEFORE the spawn and surviving every exit the stage takes; a published plan
  moves the pair onto the tip it pushed (that commit is what the stage now vouches for) and only a
  successful relabel out (`_clear_stale_read_only_park`) drops it. It answers two questions, and which one is
  being asked is settled by whether the discussion stage has the issue parked:
  on an unparked issue it means a round ended with no disposition (withheld by a mid-run `paused`, or cut short) and
  comparing it to the branch says whether that round committed; on a parked one it says everything the branch carries
  AT that SHA predates this stage — which is what `relabel_evidence.py` reads to let a discussion held on an
  inherited PR branch relabel to implementing. A park that *did* find a commit keeps the pair for that second reading:
  it is the tip the park tells the operator to reset back to, and the one the guard then certifies, so dropping it
  would strand a PR-backed issue whose only other remedies (reset to base, delete the branch) destroy the PR. The
  branch is recorded beside the SHA because an issue pinned to a legacy `orchestrator/issue-N` ref opens its round
  there, and answering for the slug-namespaced ref instead would report an unchanged tip while the commit sat
  elsewhere.
  `read_only_baseline_sha` — what that anchor becomes when the relabel clears. `_clear_stale_read_only_park` hands the
  certified tip to the implementing stage rather than dropping it, because the fresh-spawn path reads any branch ahead
  of base as a previous dev run whose publication was interrupted (`_has_new_commits`) — and the branch a discussion
  was held on may legitimately be ahead of base already. Without the handover the first implementing tick would skip
  the implementer and republish the inherited commits as the work the discussion just agreed to. It is the anchor
  except where the same handoff moved the branch onto a plan PR's live head, in which case it is that head: what this
  key has to name is where the branch REALLY sits, not which commit the record started from.
  `spawn._recovered_work_present` spends it: while HEAD still sits on that SHA the commits are inherited and the dev
  runs, and once the dev commits, HEAD moves off it and the key is dropped. That is a comparison, so it is spent only
  on a HEAD that was READ — `_head_sha` reports its own failure as `""`, which differs from the certified tip exactly
  as a commit does, and a baseline retired on that reading would skip the implementer and republish the design's
  predecessor as the work the discussion just agreed to. An unread head leaves the key standing and the dev runs, the
  same as a head still on it. `publication._advance_to_validating`
  spends it too, since an issue leaving for `validating` has published and would otherwise carry the key — and
  everything the key holds — out of this stage with it.
  Standing beside `discussion_plan_sha`, it is also the record that says a handoff was ACCEPTED and nothing here has
  published since, which is a state a crash can leave an issue in for polls at a time: the write lands before the
  developer runs and an interruption drops everything staged after it. While it stands,
  `plan_handoff._reconcile_open_plan_handoff` takes the guard's own reading again on every tick — the same plan
  PR read, the same re-anchor onto what it carries, the same two records written — because the humans still have the
  design on an open pull request and can move its head. Left unwatched, an amendment made in that window reads as this
  stage having pushed: merged, the issue closes as `done` with no developer having run, and unmerged the developer is
  spawned on the checkout the handoff left and its ordinary push takes the amendment back out. A merge alone matters
  with nothing amended, since the freeze below would otherwise start the developer behind a base the plan has just
  landed in. What ENDS the reconcile is the branch and not a record, because a push reaches git before it reaches the
  issue: a tip past the baseline is a developer's work, and a tip that could not be read is no answer at all and holds
  the tick.
  `read_only_anchor_sha` — the head that reconcile is moving the branch onto, written before the move and retired by
  the write that records where it landed. The move has the same window the handoff itself does, one level down: the
  ref is put on the reviewers' head before anything says it was, and the branch a crash in between leaves is a tip
  past the baseline — which the reading above would call a developer's commit, handing their amendment to the
  recovered-work shortcut to push with no agent having run. A marker still standing says the branch is where this
  stage was putting it, so the move is simply made again; nothing is spawned between the two writes, so no developer
  can have committed under one. `publication._advance_to_validating` clears it beside the baseline.
  `disposition._run_left_commits` reads the
  same floor at the other end of the tick, so a dev that answers with a question instead of committing parks on it
  rather than having the inherited commits published as its work. Both the cleared park and this key are written
  BEFORE the spawn, because a mid-run pause or a shutdown interruption returns without writing pinned state at all:
  staged, the acceptance would be lost and the next tick would read the park and anchor back and convict the
  developer's own commit. An unspent baseline also holds the branch out of the base refresh (`_issue_skips_base_sync`
  again), since a rebase would move HEAD off the certified SHA while the inherited commits it names are still there
  and the next spawn would read them as an interrupted dev run. `_publish_committed_work` retires it — there is
  committed work to publish either way at that point — so the freeze ends with the stage that needed it rather than
  following the issue through review.
  `pending_auto_base_rebase_push_sha` — set to the pre-rebase local HEAD immediately BEFORE
  `_rebase_base_into_worktree`; cleared on every exit. A non-empty value on entry means a previous tick rebased and died
  before the post-push write, and `_recover_pending_auto_base_rebase` keys off it to either no-op, push the recovered
  head, or park as `auto_base_rebase_push_failed`. It is also what tells the approval that interrupted attempt wrote
  from a stage's, so the refresh is not frozen out of finishing its own route (see [Base refresh](#base-refresh)).
  `pending_auto_base_rebase_rewrite_pr` + `pending_auto_base_rebase_rewrite_stage` — the TERMS of the same attempt,
  written in the anchor's own statement, before `git rebase` is allowed to touch the branch. They are what the
  permit's publication checks are asked against on the tick after a crash: read off the issue then they would
  compare today with today, and a relabel or a repoint made while the process was down would pass as the dead tick's
  own. They are also what the recovery reconciles against the issue's before it finishes any road, since finishing
  posts a notice, files an event, and drops the anchor.
  `pending_auto_base_rebase_rewrite_sha` — what that rebase produced, written on the statement after git hands it
  back, before the head is read for anything else and before the size gate is entered. It is the only thing that can
  say the divergent checkout a recovery finds is that attempt's own work: a rebase REPLAYS the branch, so a worktree
  somebody rebuilt, an operator's reset, and a branch pointed at other work all present the same shape and all
  satisfy the same lease.
  The group is read whole or not at all and typed against the same shapes every other late field is — an
  abbreviation or a value that is not a whole git object id is no head — and dropped by the same write that drops
  the anchor. Absent, IN FLIGHT, and DAMAGED are three answers rather than one. A comment carrying none of the three
  is an attempt from before this record existed, and only there do the ahead/behind counts get to answer. A comment
  carrying the terms and no head is the window between git returning and that second write: nothing names the
  commit in the checkout, so the recovery offers it to the permit to be proved by what it CONTRIBUTES against the
  pair the adjudication recorded — a replay of the accepted change proves out, and a checkout nothing here made
  does not. Where the issue carries no such pair — a legacy exemption, a base the remote would not name, an issue
  that never earned a verdict — there is nothing to prove it by, and the branch is reset onto the anchor and parked
  rather than force-pushed on a count. And a comment carrying a head that does not vouch for the checkout — a term
  taken out, a head that is not a commit, terms missing under a head that is there, or a whole record naming some
  other commit — resets onto the anchor and parks too, because read as either window it resembles, a strictly-ahead
  checkout would be measured and force-pushed on the strength of a claim nothing could check.
  `pending_auto_base_rebase_announced_sha` is the last member and covers the last window a finish has: everything a
  finish announces goes out before the relabel and the write that clears this record goes out after it, so the head
  it has already said it published is recorded in between, while the anchor still stands. A tick lost there comes
  back to a comment that says the announcement was made and owes only the relabel it may not have reached and the
  write it never made — rather than putting a second `base_rebased` on the stream, under the stage the relabel
  moved to, for one publication that happened once. The relabel is part of what it owes: dropping the record
  without it would strand the issue on the stage the rebase ran from with nothing left to correct it. The window
  the mark cannot close is the one before it — the notice and the event are out and nothing says so — and it
  resolves toward saying them again rather than losing them, since a record a reader can see twice beats one nobody
  can reconstruct. It is read by PRESENCE, like every other checkpoint here: the key standing at all says a finish
  announced THIS attempt's replay, so a value naming any other head — or naming no commit — is a mark something
  took apart, and the tick parks rather than reading it as "nothing was announced" and saying the notice and the
  event a second time for one publication. What the mark forgives is one LABEL rather than the fact of a relabel:
  `workflow:validating` is the only stage this route ever writes, so a mark found beside `workflow:fixing`,
  `workflow:documenting`, or `in_review` is somebody's move over an unfinished attempt and parks as the foreign
  publication it is. And the finish it resumes is held to the base lag like every other one: a base that advanced
  again while the process was down leaves the announced head behind it, so the record goes down, the label stays
  where it is, and the tick falls through to the rebase that brings the branch forward — whose own finish makes the
  route.
  The whole group is dropped by the one write that ends an attempt, so no road can leave a member behind. Where
  the anchor is found under a label the base refresh does not drive there is no fetch and no comparison to be
  had, and the road is a clear or a park: a checkout still standing on the anchor strands nothing and the record
  is dropped, while a recorded replay, a permission still owed a push, a checkout `git rebase` has already moved,
  and a head this host cannot read each park with every record intact — the last two because a clear there leaves
  the rewrite on the branch with nothing naming it and hands on an issue no reader can tell from one with nothing
  in flight.
- **Counters / timestamps.** `retry_window_start` + `retry_count` (24h fresh-spawn budget shared between implementing
  and decomposing, with `retry_cap_stage`, `retry_cap_continued`, and the sentence the park owes the thread beside
  them once it runs out — `retry_cap_notice`, or `late_park_notice` where a late adjudication is what ran out, since
  that park is taken by the late owner and rides its write; see [The retry budget](#the-retry-budget)),
  `silent_park_count` (dev-session silent-park counter), `dev_resume_count` (per-dev-session resume budget; once it
  reaches `DEV_SESSION_MAX_RESUMES` the session is retired and respawned fresh from durable state, reset to 0 on every
  fresh spawn), `merged_at` / `closed_without_merge_at` terminal stamps, and the per-round stamps `last_question_at` /
  `last_discussion_at` the two operator-applied conversation stages set on every run they settle.
- **Usage meter.** `issue_agent_runs` + `issue_total_tokens` + `issue_total_cost_usd` + `issue_cost_sources` are
  per-issue cumulative counters folded in by `_accumulate_issue_usage` at each developer (implementing), reviewer
  (validating), decomposer (decomposing), question, and discussion run site from the `UsageMetrics` that
  `_run_agent_tracked` parses. `issue_total_tokens` sums input +
  output + cache-read + cache-write (codex `cached_tokens` is excluded — it is already part of `input_tokens`, so
  summing it would double-count); `issue_total_cost_usd` sums each run's `cost_usd` (`None` costs from `no-usage` /
  `unknown-price` runs add nothing); `issue_cost_sources` is the sorted distinct `cost_source` set a terminal verdict
  reads to mark `(est.)` (any `estimated`) or unpriced `unknown` (any `unknown-price`). The increment rides the
  handler's existing single `write_pinned_state`, so an `interrupted` run that returns without writing never accrues.
  The decomposer / question / discussion stages additionally skip the fold for `interrupted` runs, so even
  their dirty/commits inspection park (which does write pinned state) records no counter.
- **Agent-run ledger.** `agent_run_allowance` + `agent_runs_used` + `agent_run_reservation`, owned by
  [`orchestrator/workflow/engine/run_ledger.py`](../../orchestrator/workflow/engine/run_ledger.py) and read back as
  one typed `AgentRunLedger` snapshot (the configured ceiling, the allowance in force, the count spent, the launch
  outstanding, and what is left of the allowance). `agent_run_allowance` is the ceiling this issue is held to; absent
  — which is every issue nobody decided anything special about — `MAX_AGENT_RUNS_PER_ISSUE` governs and is read live,
  and a recorded `0` says unlimited exactly as a configured `0` does. `agent_runs_used` is monotonic: it is charged
  upward and never decremented, zeroed, or rolled over, it goes on counting while the ceiling is off (an unlimited
  setting stops nothing turning runs away, not the runs), and a missing one starts from the `issue_agent_runs` meter
  above rather than from zero — the two count the same unit, so the larger of the pair is what a read answers.
  `agent_run_reservation` is the launch currently holding a charge, as one of two stable phases: `reserved` (charged,
  not yet spawned) and `started` (the spawn happened), with `agent_run_fingerprint` naming which launch that is — the
  digest of the request's own identity (role, stage, backend, spec, resumed session, review round, retry count), never
  of its prompt, which is rebuilt every tick and would make one launch look like a new one every poll. The charge is
  taken before the spawn, so a run that crashed, timed out, or was killed mid-flight is still spent; settling a
  reservation drops the phase and the fingerprint together, never the charge. This owner decides nothing and posts
  nothing — the reading is taken and acted on at the tracked spawn boundary
  ([The agent-run circuit](#the-agent-run-circuit)), and the one writer of `agent_run_allowance` is the operator
  command below.
- **The agent-run-limit park.** `awaiting_human` + `park_reason="agent_run_limit"` + `agent_run_limit_notice`, owned
  by [`orchestrator/workflow/engine/run_limit.py`](../../orchestrator/workflow/engine/run_limit.py), which is handed
  the ledger reading rather than taking one — so the park quotes the numbers the refusal was made on rather than
  whatever the setting has become since. It is the human-intervention state a spent lifetime ledger leaves, and
  unlike the retry cap beside it there is no window under it to elapse: a lifetime total is spent once and no clock
  returns it, so the park IS the ending rather than a pause in it. The durable half goes down before a word of it is
  said, for the reason the retry cap's does — a notice on a thread no pinned state backs is one nothing would ever
  reconcile. `agent_run_limit_notice` is a record rather than a bare sentence: the message, the `allowance` in force,
  and the runs `spent` against it. The reason under this park never varies, so the pair of counts is the only thing
  that can tell one exhaustion from another — a recorded sentence about the reading the ledger still shows is kept
  **verbatim** (the thread is searched for exactly that text, so rewording it would find nothing and say it twice),
  and one about any other reading is replaced, since it quotes an allowance or a spend the issue has moved off.
  Before it is said again the thread is read for it, and only a comment **this orchestrator wrote** above the
  `last_action_comment_id` watermark counts as the receipt (`github.comments.authored_by_us`, the same author check
  every park-notice reconciliation gates on); a thread that could not be READ is its own answer and the tick says
  nothing, since the sentence it may already carry is the one about to go out. The park is held by the DISPATCHER
  (`_run_limit_holds_the_tick`), one step behind the pair that run — an authorized restart and a cancelled cycle's
  own cleanup — and ahead of everything else, because every road below it is a stage's and each answers
  `awaiting_human` with the park it was written against: a resume on the next trusted reply, a hold waiting on
  guidance, a classifier that refuses a command carrying none. None of those buys back a run. The hold replays the
  sentence the park still owes before it returns, since nothing below it runs to say one, and it is the one guard
  there that steps aside for a CLOSED issue — what a close reaches below is a terminal that ends the issue rather
  than a road that spends anything on it, and refusing it would leave the issue permanently mid-ending. A later tick
  that meets the same explained park says nothing and records `standing`. Both fields are additive and default safe:
  an issue recorded before them, or hand-edited into a shape neither fits, reads back as unparked and owing nothing
  rather than as a tick that raises.
- **The one command that lifts it.** `/orchestrator add-agent-runs N`, owned by
  [`orchestrator/workflow/engine/run_grant.py`](../../orchestrator/workflow/engine/run_grant.py) and asked by the
  same dispatcher hold, because the ledger is spent by every role at every stage and no one handler is where a human
  would say it. It is read only while THIS park stands on an OPEN issue (a command on any other park, or on a running
  issue, is a ceiling nobody was held to; a closed one is let past to its terminal before the read is taken), only
  past `last_action_comment_id` from an author `ALLOWED_ISSUE_AUTHORS` trusts, only
  once the park's own sentence has been said (the delivery moves the response boundary, so a command read before it
  would be bought and then consumed by the notice explaining the park), and only as an exact positive whole number no
  larger than `MAX_RUNS_PER_COMMAND` (50) — leading zeros are dropped first, so `007` is seven and a digit string too
  long to be inside the bound is turned away *before* `int()` sees it, since the interpreter refuses to convert one
  past its own limit and a request that raised would be neither granted nor refused. The last command in the unread
  batch is the request. A valid one writes
  `agent_run_allowance` = `used + N` — an absolute ceiling rather than an increment, so a tick that dies between the
  receipt and the write buys the same runs again rather than a second `N` on top of them — clears `awaiting_human`
  and `park_reason` and drops any `agent_run_limit_notice` record beside them, ratchets the watermark past both the
  command and its own acknowledgement, records `granted`, and returns the tick to the stage handler its label names.
  That watermark is derived from what the tick actually read — the last comment of the batch the command came out of,
  walked forward only over comments this orchestrator wrote (by recorded `orchestrator_comment_ids`, else by
  `_ORCH_COMMENT_MARKER` + author) and stopped by the first that is not. A comment posted between the batch read and
  the receipt is therefore left above the mark for the next tick, since a watermark is how every stage decides what
  is unread and a comment swept under it is lost rather than delayed.
  Every other request leaves `agent_runs_used` and `agent_run_allowance` exactly as it found them, keeps the park,
  and posts one receipt carrying `<!--orchestrator-add-agent-runs-refused:issue=N:comment=M-->`. Both answers are
  marked that way — the acknowledgement carries `<!--orchestrator-add-agent-runs-granted:issue=N:comment=M-->`; each
  is scoped to the comment that asked and checked for on the thread (author included) before it is written again,
  so a write that failed after a post that landed is recognized rather than answered twice. An untrusted request is
  answered with nothing at all: a reply is a comment somebody else's word paid for, and consuming the thread for one
  would spend the watermark a trusted operator's command is read against. A bare command is also kept out of the
  `user_content_hash` (`run_grant._is_bare_command`, one of the filters in
  [the drift hash](delivery-stages.md#user-content-drift-detection)), since the tick that answers it is the tick the
  stage handler runs on. Nothing here returns a spent run.
- **Terminal usage verdict.** `_format_issue_usage_verdict` renders those counters into one visible receipt line
  (`:receipt: this issue: N agent runs · T tokens · $X.XX`, `(est.)` appended when any `estimated` contributed,
  `unknown` in place of the figure when an `unknown-price` run leaves the total incomplete). It returns nothing when
  no run was counted, so a terminal with an empty meter posts no receipt. Every terminal surface renders it before its
  single `write_pinned_state`: the PR merged / rejected finalizers (`_finalize_if_pr_merged`,
  `_drain_review_pr_terminals` — all three arcs, including the open-PR/manually-closed-issue rejection — and
  `_finalize_if_issue_closed`, all on the `workflow/engine/terminals.py` owner the stage leaves import
  directly) post it as a standalone `_post_issue_usage_verdict` comment, the `umbrella`
  all-children-done branch appends it to its close comment, the closed-`question` terminal posts it when
  question-stage counters accrued, and the `discussion` stage's plan-PR terminal posts it on each of its three
  endings — the merged plan, the plan closed unmerged, and a close with no plan PR at all — since that owner
  composes those same three arcs directly rather than through either entry point. Reusing `_post_issue_comment`
  keeps the receipt's comment id tracked in
  `orchestrator_comment_ids`. This is a read-only verdict — no budget breaker or control behavior gates on it.
- **Late generation.** The additive `late_*` group an oversized committed candidate is adjudicated under — cycle and
  generation identity, root / current issue and lineage depth, the declared scope, the frozen candidate and base SHAs,
  the measurement and the readings that did not produce one, the reconciliation phase, the local content fingerprints,
  the held pull request, how the gate was entered and what it was entered from, the external-resource ledgers, the
  owner read a finished run still owes, the cancellation marker, and the pending-restart marker. The one commit an
  accepted candidate publishes under sits beside that group rather than in it, since clearing the generation is
  exactly what it has to survive. Every key, and what an absent one means, is in
  [Late generation state](#late-generation-state) below.

The legacy `codex_session_id` key (written before `dev_agent` existed) is still honored on read by `_read_dev_session`:
it round-trips to `spec="codex"` with no args so an older orchestrator's pin keeps running on codex.

### The retry budget

Fresh agent spawns are charged to one per-issue budget — implementing and decomposing share it, because both spend
the same issue's day of tokens — and it is decided on
[`orchestrator/workflow/engine/retry_budget.py`](../../orchestrator/workflow/engine/retry_budget.py), which every
stage's gate reads and none of them re-implements. The gate answers a decision and posts nothing: what a refusal
implies durably is staged into the caller's own pinned state and rides the write that caller was going to make
anyway, so a tick that dies between the two leaves the budget as it found it. What a caller then DOES with a refusal
is written once beside the gate (`_charge_or_park`) for the stages whose park carries nothing of their own — the
initial decomposer's and the implementer's fresh spawns — and by the stage itself where the park has to carry state
the budget never sees, which is the late adjudication and its live generation.

- **The accounting.** `retry_window_start` + `retry_count`, against the bound `MAX_RETRIES_PER_DAY` sets. The window
  opens at the first counted attempt and reopens once 24h have elapsed. Only fresh spawns count: a resume on a human
  reply and a recovered worktree's push are an unblock signal and carry-over work. An unbounded budget (`0`) counts
  nothing and drops the pair as it passes, so turning the budget off is not a pause on a window — turning it back on
  opens a fresh one rather than refusing out of a count nobody could spend while there was no budget to spend it
  against.
- **The park.** `awaiting_human` + `park_reason="retry_cap"` + `retry_cap_stage` (the stage whose spawn ran out; the
  budget is shared, so the flag alone cannot say what the human is being asked about). The gate asks that park before
  it asks anything else, and while it stands nothing gets past it that is not a human — not the clock reaching the
  end of the window, and not an operator widening `MAX_RETRIES_PER_DAY` or turning it off, which is a setting change
  rather than an answer to the notice. What ends it depends on which stage the parked issue is in. Where a stage
  routes the park to a resume, a reply on the thread takes the flag down as its own side effect. The two stages that
  spend this budget hold it instead, each ahead of every road of its own that would walk past a park — the drift
  reset, the kill switch and the resume on `workflow:decomposing`; the parked-continue classifier, the drift check
  and the resume on `workflow:implementing`, the last of which would take the flag down on any reply at all and start
  a session nothing charges. On both, nothing but the renewal below lifts it, and the issue keeps untouched
  everything the tick never reached: the manifest, the children, the locked session and its spec, the `pr_number`,
  the frozen candidate, and a late generation's whole record
  ([decomposing](delivery-stages.md#_handle_decomposing-label-workflowdecomposing),
  [implementing](delivery-stages.md#_handle_implementing-label-workflowimplementing)). The late adjudication that
  runs under `workflow:decomposing` holds it a third time, on its own road and for its own reasons — the tick that
  meets one there never proves the frozen pair, never re-marks the pull request the candidate stands under, and
  never reads the thread as an answer to a question about the requirements
  ([the late run](#the-late-run)).
- **The notice.** `retry_cap_notice` — the sentence the park still owes the thread, written **before** a word of it is
  said and dropped only by a post that landed or by the park ending. The order is the point: a notice on a thread that
  no pinned state backs is one nothing would ever reconcile, and the window under it would roll over a day later with
  the issue running again beneath a comment saying it had stopped. What the park routes the tick past is what makes
  the record load-bearing, so the sentence is replayed at stage entry (`_replay_owed_notice`, called by
  `_handle_implementing` and `_handle_decomposing` ahead of every gate) until the thread carries it — and a stage that
  answers a command on this park waits for that sentence before it reads the thread at all, since a delivery moves the
  response boundary past everything written under the old one and words that predate the question are no answer to it.
  Spelled without the mention the delivery prefixes, and kept verbatim for as long as the park stands: the thread is
  searched for exactly that text, so a later refusal under another stage or a retuned cap may not reword it. Before it
  is said again the thread is read for it, so a comment that landed under a pinned write that then failed is recorded
  as said rather than repeated — and only a comment **this orchestrator wrote** counts as that receipt, since the
  sentence is plain text anybody on a public thread can copy (the comment-side receipt rule in
  [`security.md`](../security.md#the-snapshot-ref-namespace); the author is read through
  `github.comments.authored_by_us`, the author check the marker lookup and both park-notice reconciliations gate on). A
  thread that could not be READ is its own answer, distinct from one read and found empty: the notice stays owed and
  the tick says nothing, because a request that failed inside the window where the sentence is already posted would
  otherwise produce exactly the duplicate this protocol exists to stop.

  A park the LATE adjudication takes carries the same sentence in `late_park_notice` instead — that mode already owns
  a notice field, because everything one of its parks leaves standing is a generation's record that has to ride the
  same write ([late generation state](#late-generation-state)). The persist-before-post order, the verbatim match,
  and the authorship rule are the same there; a sentence carrying no marker is one anybody could otherwise paste back
  to mark a park explained that nobody ever explained. The unreadable-thread reading is the one thing that is **not**
  the same, and deliberately: that field answers a failed read the way it answers an empty one, so the sentence is
  said again. It costs one repeated comment where this owner would have stayed silent, and it buys the property the
  late mode needs more — the notice always reaches the thread, which is what moves the response boundary before any
  command on that thread is read as an answer. Its own reasoning is under [late generation
  state](#late-generation-state).

  Which field a `retry_cap` park under `workflow:decomposing` carries therefore depends on which owner took it, and
  the hold that keeps that park reads **both**. A park the shared parking form took — on an issue that had not
  entered the size gate, or before the late owner existed — is said by the stage-entry replay, and that replay is the
  one step that can stand down leaving the sentence unsaid. The late hold runs immediately after it, so treating its
  own field as the whole question would find the park explained, take a second read that may succeed where the first
  failed, and buy an adjudication with words written before the human was ever asked.
- **The renewal.** One explicit step on this owner, and the only thing that renews a budget while its park stands. It
  routes no comment itself: a caller establishes that the park stands and that the words it is answering are a trusted
  `/orchestrator continue`, and then calls it. The three `retry_cap` owners are the callers that do
  (`_park_owns_the_tick` under `decomposition/`, `decomposition/late_retry_cap.py`, and `implementing/`, each taking
  the command with whatever else its comment carries and consuming the batch it read); under a stage that has no such
  caller a park is lifted by the reply that resumes the session instead. What the step grants is a single attempt: it
  reopens the window at that moment and clears the park with its stage and notice, and the caller retires the locked
  session in the same write, keeping the agent spec — a fresh spawn is what was bought, and the spawn pins an id of
  its own only when the run hands one back, so an id left standing is one the next reply would resume. The late
  adjudication is the one caller that retires nothing there, and for the same reason rather than a different one: its
  pre-spawn record already drops `late_session_id` for every run that is not continuing a question a human has
  answered, so the attempt a command buys opens a fresh conversation before the agent starts. On
  `workflow:implementing` the gate asks that again for every spawn a grant pays for, since a grant outlives the tick
  that took it and the budget is shared: a restart, or a continuation bought on a `decomposing` park, reaches the
  spawn with a session no `implementing` park ever saw. A whole fresh day would let one reply spend the cap over again
  with nobody watching. The attempt is written down as itself — `retry_cap_continued`, a count of granted spawns
  nobody has spent — rather than as a counter to compare against the setting when the spawn is finally asked for,
  which would make it worth nothing once an operator turns `MAX_RETRIES_PER_DAY` off and several attempts once they
  widen it. An issue carrying that count is answered from it and from nothing else: no window is renewed under it and
  no cap is read, and a grant with nothing left refuses like any other exhausted budget, so the next attempt is a
  human's word again. Until it is spent, the stage's other roads to an agent stand down for the gate — on
  `workflow:implementing`, the body-edit resume — since a resume passes no gate and would run the attempt while
  leaving it on the issue to be bought again. The count is dropped where the rest of the budget is — the publication
  that moves the issue on (`_reset_implementing_counters`) — and refunded with the counters by the one write that goes
  out before an agent starts: the late adjudication's pre-spawn record (`_ACCOUNTING_FIELDS`), so a run the close
  latch, a pause, or a shutdown declines leaves the attempt there to be taken again. The two initial-spawn `retry_cap`
  owners need no refund for that case: the grant is written before the spawn and the spend rides the tick's own later
  write, which a mid-run `paused` or a shutdown never makes.
- **The audit.** One `retry_cap` event per step, `phase` distinguishing them — see
  [`observability/event-streams.md`](../observability/event-streams.md#audit-event-log-event_log_path).

`retry_cap_stage`, `retry_cap_notice`, and `retry_cap_continued` are additive and default safe: an issue recorded
before they existed, or hand-edited into a shape none of them fits, reads back as no park stage, nothing owed, and no
spawn handed out — never as a tick that raises. The grant is the strictest of the three, since it is the one field
that hands out a spawn, and it is the one whose ABSENCE is the safe reading rather than its content. Absent — never
continued, or cleared back to null by the publication reset — the issue is answered by the configured budget, as
every issue that never hit the cap is. Present, it governs: a number is read into the range a continuation writes (a
bigger one buys the same single attempt, a negative buys nothing), and a value that is not a number at all proves no
attempt and hands out none, which parks the issue and asks a human rather than falling through to a whole window's
worth of spawns off the strength of something somebody typed.

### The agent-run circuit

The lifetime ledger is charged at the one place every role reaches an agent through:
[`orchestrator/workflow/engine/run_circuit.py`](../../orchestrator/workflow/engine/run_circuit.py), asked immediately
around the sole low-level `run_agent` call inside `_run_agent_tracked`. A gate written into each spawning handler
would be a gate the next handler is added without; there is exactly one call that starts an agent process, so a
charge around it is one every role, stage, and cycle pays. Every launch names an `AgentRunBudget` — the issue a
charge is written on, and the caller's own `PinnedState` — and the parameter is **required**, so a spawn road that
omitted it would not compile rather than quietly spend runs nothing counts. All eight spawn sites (the decomposer's
fresh and resumed runs, the late adjudicator, the question and discussion rounds, the developer's fresh spawn and
its resume, and the reviewer) pass one, and
[`tests/workflow/engine/test_spent_ledger_spawns.py`](../../tests/workflow/engine/test_spent_ledger_spawns.py)
drives the real handlers against a spent ledger so an unwired road is caught as a spawn that happened.

- **Two durable writes, both before a process exists.** `reserved` (charged, nothing invoked) then `started` (the
  invocation is what happens next). A charge taken behind the spawn is one a crash, a timeout, or a shutdown kill
  collects for free. A tick that dies between the two writes leaves `reserved`, and the launch it was taken for
  reuses that charge; a tick that dies anywhere after `started` leaves a run nobody can prove did not happen, so the
  next launch charges a new attempt. Nothing settles a charge by giving it back. A spawn spends one pinned read and
  one or two pinned writes on this — two when it charges, one when it takes over a pending `reserved` — which is
  work proportional to agent runs rather than to ticks, so it does not move the idle per-tick floor in
  [`configuration.md`](../configuration.md#github-rate-limits).
- **The launch is matched, not just the phase.** A standing `reserved` is reused only when `agent_run_fingerprint`
  names this same request; a charge some other road recorded is one this launch never paid for.
- **Durable state is re-read, and only the circuit's own fields come back.** The charge is written onto a fresh
  `read_pinned_state`, and exactly the keys that write changed are merged into the caller's object. Everything else
  the caller is holding is mid-tick — a reviewer spec staged ahead of the spawn, a moved reply watermark, a charged
  retry slot, a session id about to be replaced — and belongs to the handler's own single write at the end of the
  run. The merge is what keeps that write from putting the charge back the way its read found it.
- **Nothing is invoked unless the charge landed.** An unreadable or unparsable pinned comment, a refused write, and a
  spent allowance all return an `AgentResult` with `interrupted=True` **and `invoked=False`**, and emit no
  `agent_spawn` / `agent_exit`: there is no run to bookend. Handlers already read the first through
  `_ignore_if_interrupted` and return without writing durable state. Only the spent allowance is a decision about
  the issue, so only it takes the park above — on the freshly read state, whose durable write the park owner makes
  itself — and the dispatcher's hold stops the issue on the next tick.
- **`invoked=False` is not the same claim as `interrupted`, and four roads need the difference.** The initial
  decomposer, the late adjudicator, `question`, and `discussion` inspect the checkout *before* they ask about
  interruption, deliberately: a run the shutdown sweep killed can have written before it died, and a contaminated
  tree is an operator's to see whether or not the run counted. A launch that never started wrote nothing, so a tree
  dirty from an earlier run — or a `HEAD` that will not resolve — is not its doing, and a `decomposer_dirty` /
  `late_worktree_mutated` / `question_dirty` / `discussion_unreadable_worktree` park in its name would **replace the
  durable `agent_run_limit` park the refusal had just taken** with a reason about a process that never existed. Each
  of those roads therefore asks `guards._ignore_if_never_invoked` first, ahead of every reading it would otherwise
  classify the run by.
- **What is charged is a process, not a tick.** A developer resume that lands on a transcript the backend has lost
  buys a second spawn in the same tick — a fresh one, in the same worktree — and pays for it, because it is a second
  run. A run the shutdown sweep killed and a run an operator paused mid-flight are charged too: both cost the same
  compute as one that finished, and only the disposition is thrown away.
  [`tests/workflow/engine/test_charged_launches.py`](../../tests/workflow/engine/test_charged_launches.py) drives
  every developer road — the fresh implementation, the resumes `implementing`, `fixing`, `documenting`, `in_review`
  and `resolving_conflict` make, and the poisoned-session retry behind them — plus the fresh reviewer round, and
  reads the spend back off each issue's own pinned comment on the far side of the handler's write.
- **The caps that were already there still refuse first.** The 24h retry budget, `MAX_REVIEW_ROUNDS`, and
  `MAX_CONFLICT_ROUNDS` each park ahead of the spawn, so a tick they turn away reaches no boundary and spends
  nothing; `DEV_SESSION_MAX_RESUMES` refuses the resume rather than the tick, retiring the transcript before the
  charge so what is paid for is one fresh spawn. The lifetime ledger is the backstop under them, not a replacement
  for any of them, and a cap that fired only after the charge would spend a run on work nothing ran.
  [`tests/workflow/engine/test_capped_launches.py`](../../tests/workflow/engine/test_capped_launches.py) drives each
  one against an issue whose ledger has room to spare.
- **The three roles that talk pay the same meter.** The decomposer's fresh spawn and its awaiting-human resume, the
  question stage's opening round and its resume, and the discussion stage's opening round and its resume are six
  roads to an agent, and a road can reach the boundary naming an issue that is not the one it is spending.
  [`tests/workflow/engine/test_charged_conversations.py`](../../tests/workflow/engine/test_charged_conversations.py)
  drives all six through their real handlers and reads the spend back off each issue's own pinned comment — on a run
  that finished, one the shutdown sweep killed, and one an operator paused mid-flight. The late adjudicator is a
  seventh road and not a dispatched handler, so its case sits beside it in
  [`tests/workflow/stages/decomposition/test_late_charged_run.py`](../../tests/workflow/stages/decomposition/test_late_charged_run.py).
- **The charge carries nothing else out of the tick with it.** Each resumed round marks the batch of replies it quotes
  as read BEFORE it spawns and leaves that watermark unpersisted on purpose: a round that never reports is replayed,
  and it has to be replayed against the same replies rather than against an answer already recorded as read. The late
  coordinator holds the retry slot it charged out of its own pre-spawn write for the same reason. Both sit inside the
  window the two charge writes land in, so the merge that carries back only the circuit's own fields is what keeps
  them unpublished — the paused-round cases in the two modules above are what hold it.
- **Each durable step is reported to both observability sinks.** `reserved`, `started`, the park a spent allowance
  takes, and the wider ceiling an operator command buys are one `agent_run_budget` family written by
  [`orchestrator/workflow/engine/run_budget.py`](../../orchestrator/workflow/engine/run_budget.py) — always *after*
  the write that makes the step durable, so a refused write records nothing and a launch reusing a standing
  `reserved` records only the start it paid for. Every record carries the whole ledger reading it was taken on, and
  the two charge phases carry a `reservation_id` pairing the bounded head of the same fingerprint the phase is
  matched by with the `used` count that charge moved — the fingerprint alone repeats across charges of one launch
  shape, and the count is what makes the id name a charge — so the tick that charged a run and the tick that spawned
  on it join. The contract is in
  [`../observability/event-streams.md`](../observability/event-streams.md#agent-run-budget-records-both-sinks).
- **What the number actually buys is measured end to end.** Every owner above answers for its own step, and none of
  them answers the question the setting is written in: how many agent processes one issue can start before something
  stops it.
  [`tests/workflow/engine/test_lifetime_journeys.py`](../../tests/workflow/engine/test_lifetime_journeys.py) walks a
  real issue through the loops that have no natural end — a fix answered by a review answered by a fix, a review round
  reset by a recovered conflict, a review round reset by a base synchronization, a session retired and replaced every
  round, and an issue moved back to `workflow:decomposing` and out again — running it once per tick under a small
  allowance and summing what each tick spawned. The two reset loops are entered on a round the reviewer has already
  spent and the tick itself is what puts it back, so what is asserted is a reset that happened rather than one a
  fixture staged; the base refresh in particular rebases, force-pushes, hands the issue back to `workflow:validating`
  and resets the round while starting no process at all, and the ledger is exactly where it found it. Every other cap
  is pinned wide for the length of a walk, because each of them is a setting the environment can carry and any of them
  left live could be the thing that ended the walk instead. The runner stops at exactly the configured total on every
  journey — under the issue's own allowance and under `MAX_AGENT_RUNS_PER_ISSUE` for an issue carrying none — the
  park's sentence is said once however many ticks reach it afterwards, a trusted `/orchestrator add-agent-runs N` buys
  exactly N further starts and no more, and a request past `MAX_RUNS_PER_COMMAND` buys none. The total is read back off
  the issue's own pinned comment through a client rebuilt from nothing else, which is what a restarted process would
  have.
  [`tests/workflow/engine/test_lifetime_compatibility.py`](../../tests/workflow/engine/test_lifetime_compatibility.py)
  holds the three things the ceiling may not change: an issue that predates it starts from `issue_agent_runs` rather
  than from zero, a stage cap that is spent at the same time still parks its own way with the lifetime count
  untouched, and a closed issue's terminal still drains — posting the receipt for what the whole issue spent and
  leaving both meters and the park exactly as it found them. The adjudicator's own sequence is
  [`tests/workflow/stages/decomposition/test_late_lifetime.py`](../../tests/workflow/stages/decomposition/test_late_lifetime.py),
  which freezes one fresh candidate after another over the same issue and shows the ledger — rather than the day's
  spawn budget, pinned wider than the sequence is long — ending it, plus the restart that projects a new cycle and no
  new runs; the exact-SHA exemption policy is asked of an issue with runs to spare and of one with none in
  [`tests/workflow/stages/implementing/test_late_gate.py`](../../tests/workflow/stages/implementing/test_late_gate.py),
  since a spent lifetime is neither a way past the size gate nor a reason to re-adjudicate an accepted commit.
- **There is no second road to a process.** The gate is worth what the number of places a run can start makes it
  worth, so the shape is also read off the source rather than only driven:
  [`tests/repository/test_agent_spawn_boundary.py`](../../tests/repository/test_agent_spawn_boundary.py) holds the
  whole chain — `run_subprocess` named only by the two backends, `run_claude` / `run_codex` named only by the runner
  that dispatches between them, and `run_agent` named only by the agents facade that republishes it and the tracked
  boundary that calls it — and holds that call to `_run_agent_tracked` itself, with the circuit asked on a line above
  it. A reference counts rather than a call, because a spawn bound into a variable is invoked where its name is no
  longer written.

### Late generation state

The `late_*` keys are the late size gate's own group, and they are **additive**: an issue that never entered the gate
carries none of them and reads back as an absent generation, so no migration reaches a live issue and a handler that
reads and writes late state on every issue leaves a legacy pinned comment exactly as it found it. The keys are spelled
once, on [`orchestrator/workflow/late_split/keys.py`](../../orchestrator/workflow/late_split/keys.py) —
`LATE_STATE_KEYS` is the whole of what one GENERATION owns inside the pinned comment, `read_late_generation` /
`write_late_generation` on the [`state`](../../orchestrator/workflow/late_split/state.py) owner beside it are the
round trip through them, and `clear_late_generation` is defined as dropping exactly
that list and nothing else. The late keys that deliberately sit outside it all do for the same reason — each is
written so the generation CAN be cleared and would be worthless if the clear took it. `late_exempt_sha` and the
semantic identity beside it, both described below, live on the
[`exemption`](../../orchestrator/workflow/late_split/exemption.py) owner; the `late_rewrite_*` authorization that
says a rewrite was allowed to carry one of those exemptions over lives on the
[`rewrites`](../../orchestrator/workflow/late_split/rewrites.py) owner, and outlives the generation for the same
reason the exemption does — the grant is written a whole publication before the receipt that would spend it;
`late_retired_cycle_id` and the two-phase terminal record beside it live on the
[`endings`](../../orchestrator/workflow/late_split/endings.py) owner. The typed record the
group round-trips through is `LateGeneration` on the `models` owner beside
it. A write with no `late_cycle_id` records only what the issue still owes — the two external ledgers, if either
holds anything — and drops the rest rather than keeping a half-record no audit line or child lineage could be
correlated to. Every field is read defensively: a hand-edited or older value that cannot be typed reads back as
absent rather than raising on a tick that has committed work to reconcile. Which reader a field goes through
is the field's own contract rather than its Python type — an identity has to be positive, a measurement non-negative,
a depth inside the lineage, a flag literally `true`, a source stage one of this workflow's own labels, a measurement
failure one of the steps `git/measurement/` names, a restart target one of the two labels a restart may apply, and a
rewrite kind or phase one of the bounded vocabularies the `rewrites` owner publishes — with that owner's source
stage narrower still, since only the five stages that push onto a pull request the remote already carries can be
the stage a rewrite was entered from.
The hex fields are read at their exact lengths: a frozen commit is a whole git object id (40 or 64), because nothing
here ever records an abbreviation, and a local fingerprint is a whole SHA-256 digest (64), because a truncated one is
not a hash anything could be compared against. Only a real integer counts as a number at all: a bool, a float, and
a numeric string are each a value nothing wrote. So a `late_threshold` of `-1` beside a `late_additions` of `0` does
not make an unmeasured candidate report as oversized, a `"false"` string does not arm a cancellation or a pending
restart, and prose in a `late_candidate_sha` never becomes live state — and what a read refuses, the next write drops
rather than preserving.

- **Identity.** Minted by the size gate, at the moment it freezes a candidate: the cycle is this issue's own while
  one is live and the number after `late_retired_cycle_id` otherwise, and the generation counter advances with every
  candidate frozen inside a cycle — which is what keeps a verdict recorded against an earlier commit from reading as
  an answer to this one. The lineage beside them comes off `late_ancestry_*` wherever a split wrote one, so the bound
  applies at the depth the issue was really born at.
  `late_cycle_id` and `late_generation` are monotonic and never reused, so a record naming cycle 2
  always names the same attempt; `late_root_issue` and `late_current_issue` place the issue in its lineage; and
  `late_lineage_depth` is 0 at the root and bounded by `MAX_LINEAGE_DEPTH` (3, a safety invariant no configuration
  reads). A depth at or past the bound — including one an edit put there — reads as "may not split", so the deepest
  child a split can create must resolve as one change or ask a human. A depth that cannot be read *at all* is the
  same answer rather than the root's 0: it reads back as unknown, an unknown depth may not split, and the write
  leaves it unknown, so a damaged field on a lineage already at the bound cannot buy it another generation and the
  next pass cannot normalize the gap away. The only thing that puts a depth back to 0 is a restart, whose fresh cycle
  is a root again.
- **Frozen evidence.** `late_scope` is the declared scope this generation owns; `late_candidate_sha` and
  `late_base_sha` are the exact commits a reconciliation may act on (a recorded SHA is the evidence, never the current
  HEAD or base); `late_threshold` and `late_additions` are the measurement, which trips strictly above the threshold,
  so a candidate exactly at the configured value is accepted. The threshold is `MAX_ADDED_LINES`
  ([`configuration.md`](../configuration.md#cadence-and-budgets)) as it stood when the generation was recorded, and
  the additions are what `git/measurement/` counted between those two commits, so a retuned setting cannot re-judge a
  generation already under adjudication. `late_phase` names the reconciliation boundary reached —
  `measuring`, `holding_plan_pr`, `adjudicating`, `owner_check`, `snapshotting`, `splitting`, `superseding`,
  `cleaning_up`, `cancelling`, `restarting` — so a tick that crashed mid-step reconciles that step rather than
  starting a new one. It is a boundary marker rather than a resume token: the owner-read claim every retry passes
  through rewrites it to `owner_check`, so a step that has to know whether it already ran keys on its own durable
  fact instead — the ledger entry for the snapshot and the branch, the recorded `children` for the children, and
  `decomposed_at` for the one comment a split owes its parent.
- **Measurement retries.** `late_measurement_miss_count` and `late_measurement_failure` are the record of a reading
  that did *not* happen: how many consecutive readings this CANDIDATE has lost — across generations, since a base the
  remote would not name records none and the next reading of that same commit freezes afresh under a new one — and the
  `MeasurementFailure` step a NOTICE about it named (`base_unreadable`, `base_absent`, `candidate_unreadable`,
  `candidate_absent`, `diff_unpinnable`, `diff_failed`, `diff_unreadable` — one of the two vocabularies
  [`orchestrator/git/measurement/models.py`](../../orchestrator/git/measurement/models.py) owns). The second is
  written by the roads that TELL somebody and by no other, so it reads as "the sentence on this thread names this
  step" rather than "the last reading stopped here": recorded by a quiet miss instead, it would be a notice nobody
  made, and the miss that finally spends the bound would find its own step already there and hand the issue over
  without a word. The `measurement_failure` on the emitted record is a different field answering the other question,
  and the two are deliberately not kept in step: every `late_failure` this gate writes — the quiet misses included —
  names the step THAT reading stopped at. A stream is read by somebody counting causes, so it reports every reading;
  the pinned field is read by the guard deciding whether to speak, so it reports what was said
  ([`../observability/event-streams.md`](../observability/event-streams.md#late-split-records-both-sinks)).
  They are durable because nothing else remembers a miss — every tick is a fresh process, so a gate counting in memory
  would either re-read a permanently broken pair forever or spend a human on the first reading a fetch happened to
  interrupt — and the ceiling a bounded retry is held to is `_MEASUREMENT_MISSES_BEFORE_PARK` (3),
  spelled on
  [`orchestrator/workflow/stages/implementing/state.py`](../../orchestrator/workflow/stages/implementing/state.py)
  beside the silent-park bound. Only the two steps that name the TRANSPORT are counted against it — `base_unreadable`
  and `base_absent`, a remote that would not answer for the base branch and a fetch that did not bring the object back
  — because those are the ones that clear themselves. Misses 1 through 3 write the pair and the incremented count,
  emit the typed `late_failure`, log at WARNING and stop, with no `awaiting_human`, no `park_reason`, no comment and no
  step on the PINNED record — the emitted one names it either way — so the next tick re-enters the same pair by itself
  on both roads and spawns nothing; the fourth takes the `late_measurement_failed` park, records the step it is about to
  name in the same write as that count, and mentions a human once. What that mention says is the member and a line
  written for the operator holding the issue — which of a remote, a token, a throttled request, a checkout, or a planted
  attribute file they are looking at, and, for the remote read, the fetch and the two diff steps, the
  `orchestrator.git_plumbing` channel their invocation is logged under — with whatever the failing step wrote for itself
  carried up beside it, already scrubbed of the credential by the transport that ran it. Every other member still parks
  on its first miss, since re-reading a candidate this host does not hold or a diff nothing can pin buys the same
  answer, and so does a record nobody may act on at all: the pair is proved usable before its transport is retried —
  everything a reuse needs except the base itself, which is the one field the failure being retried leaves absent. The
  retry WRITES the record back, and the
  mint behind it keeps the record's cycle, scope and spent readings while re-stamping the issue number, the ceiling
  and the boundary from the process running now — so unproved it would adopt a reading taken against another issue
  under this one's identity, or re-judge a generation that lost its ceiling against whatever `MAX_ADDED_LINES` has
  been retuned to since, either of them ready to publish here the moment the base came back. Past a measurement park
  of ANY cause the pair goes on being re-read once a poll on ONE of the two roads: the post-publication
  reconciliation, which runs ahead of every handler on the five stages that publish onto a pull request the remote
  already carries and asks nobody first. A park taken before publication is re-read by nothing at all — it owns every
  tick until a trusted bare `/orchestrator continue`, which clears the latch and the reason ahead of the gate and so
  buys one more counted attempt, whose own miss is answering a human and is said out loud. Each reading retaken with
  nobody asked is announced at most once per thing there is to say: one that stops at the step
  `late_measurement_failure` already names repeats a sentence the human cannot answer any faster, so the tick is held
  silently and no further miss is counted. That is the whole reason the field is written by the roads that ANNOUNCE —
  without it a candidate this host cannot peel, or a diff nothing here can pin, would mention the same people once a
  poll for as long as it took them to fix it. A reading that stops at a *different* member is not a repeat: it is a
  different next move, and nothing else would ever say so, so it is announced once and takes that field's place in the
  write the notice rides out on, which makes the reading after it a repeat rather than a second announcement; no miss
  is counted for it either. Silent to the THREAD and to nothing else: the typed `late_failure` still reaches both
  sinks on every one of those readings, each naming the step that reading stopped at, since the stream is the only
  place they exist at all and a run nobody can break down by cause reads like a pair nobody is looking at; and a base
  id the remote finally names is written down even then, because it is the exact object every retry after it asks
  for. And the silence is scoped twice over: to a park a human is still WAITING behind — the latch rather than the
  reason beside it, since a resume consumes the one and leaves the other standing — and to the pair that park was
  taken over. A
  fresh candidate, which is what guidance answered with, retires it and starts its own bound rather than having its
  first miss swallowed by one; so does a reason whose latch a resume already spent. That retirement rides the durable
  write that records the fresh candidate, because the two are read back as one: nothing on the comment says which
  commit a park was taken over, so a crash between that write and the verdict would leave a park over one commit
  beside a record naming another, and every later reading of that pair would be held silently against a bound it never
  reaches. A base that IS reached puts the count — and only the count — back to zero in the write that records the
  pair: reaching the base is not the last step a reading can stop at, and the member beside the count says what the
  thread was TOLD, so dropping it there would announce the diff failure behind it afresh on every poll. The member
  goes where every one of those steps is behind it — the verdict a reading that HAPPENED settles, which clears it and
  the count together in the write it settles on, before that record becomes the one an oversized candidate is
  adjudicated from — and it goes with the record itself wherever a small candidate retires one. Until then a notice
  naming another step is what replaces it. The park is retired by that same verdict rather than by the gate's own
  door, since entering the gate is not answering the question the park was taken for and a retirement there would
  durably unpark an issue whose next reading can miss again, as
  [`delivery-stages.md`](delivery-stages.md#_handle_implementing-label-workflowimplementing) describes. The count is
  read as a non-negative whole number and the failure as one of that vocabulary's own members, so a hand-edited count,
  a bool, or a `LateFailure` spelling in the failure field reads back as no miss recorded rather than as one a retry
  would count. Both are scoped to the pair frozen beside them and go with `clear_late_generation`: a candidate that
  moved is fresh work whose reading nobody has lost yet, so its misses start at zero rather than inheriting a count
  taken over commits nobody measures any more — while a base the remote would not name records no base at all, so the
  same commit frozen afresh under the next generation keeps the count it has already spent and the bound stays
  reachable. Absence is the same answer — no misses and no failure is what every pinned comment written before the
  pair says, so the write leaves both off rather than spelling that state a second way.
- **Local fingerprints.** `late_title_body_hash`, and `late_comment_hash` beside the `late_comment_watermark_id` it
  covers from, are what tell a scope edit apart from a trusted answer arriving after the late baseline. They are
  local by design: the global `user_content_hash` above keeps its single baseline and its meaning unchanged, so
  nothing here moves a baseline the re-decompose and dev-resume routes read. Who counts is that hash's own trust
  filter, asked through the same predicate — the pinned-state comment, the orchestrator's marker and its recorded
  ids, third-party bots, and every author outside `ALLOWED_ISSUE_AUTHORS` are dropped before anything is digested, so
  nothing an outsider posts shifts a fingerprint, becomes guidance, or moves the watermark. A comment with no usable
  id is dropped beside them, because the watermark is the only thing that ever consumes one. The three fields move
  together or not at all: advancing the watermark without the digest would leave a prefix nothing had hashed, and
  advancing the digest alone would let the comments it covers arrive as fresh guidance a second time. The watermark
  only ever rises, so a deleted comment cannot lower it and replay conversation an agent already answered, and the
  digest is taken over the counted prefix rather than trusted to the watermark — a comment rewritten in place moves
  no id at all, and reading that as drift is what keeps an edit with no new comment behind it from being lost. Both
  fingerprints absent is a generation whose baseline has still to be taken, which is why "no baseline" is a separate
  answer from "the requirements moved": an absent digest equals nothing, and reading that as an edit would park the
  first tick of every adjudication. Every path that ACTS on a reply moves the shared `last_action_comment_id` with
  the local watermark, because two readers walk the same thread: a question answered, a candidate certified, a
  stalled revision re-read, or a developer resumed are all comments this mode has spent, and leaving the shared one
  behind would hand them to the later validating → in_review handoff as fresh PR feedback — routing the pull request
  to `fixing`, or resuming the developer on input it already handled. It moves to the highest *trusted* comment
  folded in, so an untrusted one sitting above it stays unconsumed exactly as it does on every other resume, and it
  is a one-way ratchet. What counts as a *reply* is a third reading again, taken against the higher of
  `late_comment_watermark_id` and the shared `last_action_comment_id` above — which every announced park advances past
  the notice it posted, making it the response boundary a park needs. A comment written before a park is not an answer
  to it, so a park that fires while somebody is mid-sentence is not resolved on the next tick by the sentence they had
  already sent. What each comparison earns is in
  [`../workflow/roles.md`](../workflow/roles.md#what-a-late-adjudication-is-asked-and-what-it-may-answer).
- **Held PR.** `late_plan_pr_number`, `late_plan_pr_head`, and `late_plan_pr_body` — the pull request whose body a
  cycle-marked hold replaced, the head it was standing on when that happened, and the body it replaced, kept so the
  original can be restored. The `plan_pr` spelling is what live pinned comments carry and stays for that reason; what
  the group NAMES is whichever pull request the cycle holds — the plan one a design discussion left standing where the
  generation was entered before publication, and the implementation one the work is already on where it was entered
  past it. Which of the two it names can CHANGE inside a cycle: a generation whose publication entry names a pull
  request the hold does not is one the adjudication has moved off — guidance resumed the developer, their push landed,
  and the re-measurement entered the gate on the pull request the work is now on. The old hold is released, the slot
  freed, and the new one taken, in that order and never both at once, since there is one identity and one preserved
  body to hold them in; a release that could not be made on a reusable pull request parks the tick instead of
  spawning. A `pr_number` somebody re-aimed is deliberately not that signal — it names whichever pull request the
  issue currently records rather than the change under adjudication. All three go down in one write before the pull
  request is edited, so the only thing a crash can lose is the edit — and a head this orchestrator cannot name refuses
  the hold outright rather than being recorded absent, since the write drops an empty one and what it would leave is
  an identity and a body with no head between them, on a change already wearing the notice. What restores the body is
  the release the `single` reconciliation runs: the identity says which pull request this cycle marked, and the body
  has to BE that hold verbatim before it is written over, so a description a human rewrote — or edited a sentence of,
  leaving the hidden marker in place — is left as theirs. The hold text is keyed to the cycle and quotes nothing that
  moves inside one precisely so that comparison is possible: the generation counter advances on every reconciliation
  that lands, and a body keyed to it could never be reconstructed after a re-measurement. What the notice SAYS differs
  by side of publication, because the sentence has to be true of the change its author is looking at: one nothing has
  pushed to is adjudicated *before anything is published*, while the one the work is already on was published long ago
  and its notice names the push the adjudication stands in front of instead. Both spellings are recognized and only
  ever one written, so a record that crosses publication mid-cycle has the notice already standing rewritten rather
  than read as somebody's own words. `late_plan_pr_head` is a reading rather than a claim, and neither the retry nor
  the release is decided by it: it says which change wore the notice, so a pull request somebody pushed to under the
  hold is reported and left holding the same notice against the same recorded reading. It is **not**
  `late_published_sha` below — that one is the head the gate was entered on and what a settlement pins its push to,
  and the hold never writes it.
- **Publication provenance.** `late_post_publication`, `late_source_stage`, `late_published_pr_number`, and
  `late_published_sha` say how the gate was entered and what it was entered from — one durable write puts all four
  down together, and a comment carrying only some of them is damage read from either end: the marker gone would say
  the entry was taken before publication, and any of the other three gone would leave the marker over nothing. Asked
  as a group ahead of every handler, on the five stages that publish onto a pull request and on `workflow:decomposing`
  too: the adjudication decides which pull request the verdict was taken over, which head to pin the push it licenses
  to, and which stage to hand the issue back to, entirely from this group — so a partial one there would settle a
  post-publication candidate as though nothing had published it, route it to `workflow:implementing`, and retire the
  evidence behind it.
  What writes them is the gate standing in front of every push onto a pull request the remote already carries — the
  shared dev-fix publication and the no-feedback bounce behind it, both validating recoveries, the three conflict
  publications, the base sync's auto-rebase and its crash recovery, and the final docs pass; the implementing seam,
  which opens the pull request rather than pushing onto one, writes none of them. They are additive inside an additive
  group, and their absence is the answer rather than a gap: a generation carrying none of them was entered *before*
  the work was published, which is what every record written without this group describes — so a live pinned comment
  answers the question with no migration having reached it, and the write leaves the group off rather than spelling
  that one state a second way. `late_post_publication` is read off the literal `true` and written only while it is
  set, for the reason the cancellation marker is: `bool("false")` is `True`, and reading the flag for its truthiness
  would tell a reconciliation there is a pull request to act on. The three fields beside it are what a
  pre-publication entry has no need of and a post-publication one could not re-derive. `late_source_stage` is the
  workflow label the issue was taken out of and the state a settled adjudication continues at — read through the label
  vocabulary, so a value that is not one of them reads back absent rather than as a state a later tick would obey, and
  the adjudication runs under `decomposing` rather than the label it came from, so a stage that was not recorded is
  not one anything could recover. Being a label is not enough: the group is written and read as context only while
  this field names one of the five that publish onto a pull request the remote already carries. `workflow:ready`,
  `workflow:blocked`, and `workflow:umbrella` each own an edge to the adjudication for reasons of their own and have
  no pull request behind them, and `workflow:implementing`'s own push is what *opens* the pull request — so a group
  naming one of them is refused at the write and reads back as no publication context, rather than sending a later
  reconciliation to measure and push a candidate no post-publication stage committed. It is also where a settled
  `single` verdict puts the issue BACK, which is what the five `workflow:decomposing → <published state>` edges
  exist for: that stage is the only owner of the completion the candidate still owes — a docs watermark and its
  `in_review` handoff, a conflict round, another reviewer look — and returning every one of them to `implementing`
  instead would walk the issue back to a point it had already passed.
  The push itself is not left to that stage: the settlement makes it, because the settlement is the last tick holding
  the head the verdict was measured over. `late_published_pr_number` is the pull request the work already has, and
  `late_plan_pr_number` beside it is whichever pull request this cycle's hold marked, and on a generation entered past
  publication the two settle on the same change: a cycle that held a plan pull request and was then re-measured on the
  far side of a push reads back with them disagreeing only until the next tick, which releases the first hold and
  takes the second (see *Held PR* above). `late_published_sha` is the head that pull request was left on, frozen at
  entry like every other late SHA because the branch moves under a reconciliation that re-read it. It is the late
  group's own copy rather than a reading of `implementing_published_sha` above: that key is the publishing stage's
  live record and is overwritten by the next push, while this one is evidence one generation is reconciled against.
  Which generation drops it matters: a settled `single` retires the whole record, and so does an umbrella's own
  terminal, but the **split's** retirement onto `workflow:umbrella` keeps the group where it drops the measurement.
  Everything that supersession licensed is still to come at that point — the children the umbrella's walk releases
  and the branch its terminal reclaims, both on later ticks — and this group is the only thing left on the issue
  naming which pull request was closed and the head it was closed over, so the walk, the reclamation, and the
  terminal each re-read it before they act. The terminal's retirement drops it last, immediately behind the barrier
  that asks it one final time. The flag alone proves none of it — `LateGeneration.has_publication_context` holds
  only while the stage, the pull request, and the head are all readable beside it, so a group a hand edit half-damaged
  reads as context nothing may act on rather than as a publication with no pull request to name, and
  `LateGeneration.with_publication` refuses to record one that cannot name all three. A restart's fresh cycle keeps
  none of the group and needs none: what it puts the issue back into is `decomposing` or `implementing`, which is a
  pre-publication attempt again.
- **External-resource ledgers.** `late_resources` holds one `{kind, target, state}` entry per obligation the remote is
  owed — kind `snapshot_ref` / `branch` / `plan_pr` / `child`, state `pending` / `retained` / `reclaiming` /
  `reconciled` / `failed`
  — keyed on kind and target, so a reconciliation repeated after a crash updates the entry it already wrote instead
  of appending a second one. `reclaiming` is the decision, written *before* the delete that carries it out, so a tick
  that died between the push and the record of it has something durable to retry. What the retry does not get is a
  pass on the proof: the consumers are read again on every visit, immediately ahead of the delete, and one that came
  back keeps the ref and leaves the entry `reclaiming`. The single exception is a ref the remote no longer has: the
  delete has already happened, what is left is finishing it, and a consumer that came back to it is answered by the
  receipt and the child's own guard rather than by keeping a ref nobody holds. Every
  state but `reconciled` is still owed. `late_consumers` is the direct snapshot consumers, deduplicated and ordered,
  since the reclamation rule asks about each of them once — and it is read from the other end too, as the one record
  that can vouch for a child claiming this split in a body marker anybody can paste. Only a positive whole number is
  one — `True`, `2.5`,
  and `"7"` are not issues anything can ask GitHub about, and neither the reader nor `with_consumers` will convert
  one into a consumer id. Neither ledger is ever *reduced* to what this binary understood: an entry it cannot type, or a
  consumer list it cannot read, is carried through verbatim beside the typed view and written back exactly as it
  came, and `LateGeneration.has_opaque_ledger` says so — and while it does, `with_resource` and `with_consumers`
  refuse an update to that ledger rather than returning a record the next write would silently drop back to the
  verbatim copy. The two are preserved and written **independently**, and the reclamation refuses them
  independently: an untypable entry on `late_resources` means no reclamation can be recorded at all, while one on
  `late_consumers` means only that no snapshot's proof can be taken — the superseded branch, which owes no consumer
  anything, is still deleted and still retried. "Typed" is strict there, because the alternative to
  preserving an entry is rewriting it from what was understood — an entry counts as one this binary wrote only when
  it carries exactly the three fields it writes, each holding a value this vocabulary knows, so a state it cannot
  read is **not** `pending`, a field it never wrote is not noise to drop, and a target that is not a usable
  identifier is not one to re-encode. The damaged-identity case is preserved the same way: a record whose
  `late_cycle_id` cannot be read writes its two ledgers and nothing else, because an obligation does not stop being
  owed when the identity beside it is damaged. Dropping any of it would be an obligation deleted from the issue that
  still owes it — a cleanup that looks complete, or a snapshot reclaimed as though nobody were waiting on it — so a
  generation holding an opaque ledger is one nothing may treat as settled.
- **The split's own registers.** `late_split_children` is the ordered, positional list of the children THIS
  generation created — entry `i` is the child that owns slice `i` of its manifest — and `late_links_announced` says
  the forward-link comment has been made. Both live on the generation rather than beside the stage's shared keys
  because both have to be scoped to one adjudication. The stage's `children` list and `dep_graph` belong to
  whichever decomposition last wrote them, and an issue that was decomposed, saw its children resolve, and then
  implemented an oversized candidate still carries the old ones — so a transaction reading `children` would adopt
  **completed** issues by manifest index, and `decomposed_at` would suppress the very announcement the split owes.
  The stage's list and graph are written *from* the register instead, which replaces the earlier decomposition's
  rather than leaving one standing. The register answers one thing more, and to a reader outside the transaction: a
  non-empty one says this generation's candidate has already become children, which is what tells a settled split's
  retained publication group from a pair somebody froze and never counted (see
  [`delivery-stages.md`](delivery-stages.md#the-size-gate-on-a-published-pull-request-every-push-onto-an-open-pr)).
  `late_phase` carries that answer in the window the register cannot — a child is created before the write that
  records it — and the register carries it from there on, including through a retry that rewound the phase to the
  boundary it started from. The register is read all-or-nothing: an entry this binary did not write makes
  the whole field read back empty, because skipping one would shift every child after it onto somebody else's slice
  — and an empty answer costs a marker lookup rather than a wrong adoption. That lookup is the other half: every
  child is created carrying `<!--orchestrator-late-child:issue=…:cycle=…:generation=…:index=…-->`, so a child created
  into a crash before its number was recorded is adopted rather than opened twice. The issue is part of that identity
  because a cycle is minted per issue and repeats across them, while the lookup is not scoped to one parent's
  children — without it, two parents on their first candidate would each carry `cycle=1:generation=1:index=0` and one
  would adopt the other's child. Both fields are also cleared whenever the generation counter advances: they are the
  split transaction's own one-shot receipts, so a register carried into a revision would have the new manifest adopt
  an old child by index and a link receipt would swallow the announcement the new split owes. Neither external ledger
  is cleared with them — a ref the remote holds is owed whatever the next generation decides — and the counter is
  refused from advancing at all once either a child or a snapshot obligation is recorded, since the commit a
  recorded ref was created for is the one its reclamation compares against.
- **Inherited lineage.** `late_ancestry_root_issue`, `late_ancestry_depth`, `late_ancestry_parent`,
  `late_ancestry_cycle_id`, `late_ancestry_generation`, `late_ancestry_snapshot_ref`,
  `late_ancestry_snapshot_sha`, `late_ancestry_mirror_first`, `late_ancestry_base_branch`, and `late_declared_scope`
  are what a child born of a
  late split carries, and they are a separate group from the generation above because they answer a separate
  question and outlive it: a generation is minted, adjudicated, and retired inside one issue, while an ancestry is
  written once when the child is created and is still true after that child has been implemented, split again, and
  closed. The depth and the root are what the child's own size gate mints its generation from, so automatic
  splitting stops at the same bound three generations down as it does at the root — a child that could not say how
  deep it is would read as a root and buy the lineage another generation, which is why an unreadable depth reads
  back unknown rather than 0. The cycle, the generation, and the parent issue are what a record about this child is
  correlated back to the adjudication that created it by. The snapshot ref and commit are the only durable pointer
  to the work the child is meant to reuse, since the branch it was committed on is superseded and the pull request
  that carried it is closed — both halves or neither, because a ref with no commit cannot be verified against
  anything and a commit with no ref names work nothing can fetch. `late_ancestry_mirror_first` travels with that pair
  and is a claim about the world rather than about the child: that any reclamation which can take this ref drops
  **this host's copy of it first**. The child's own reuse guard reads a surviving copy as proof no reclamation has
  happened, and that reading holds only against an orchestrator ordering the two — a pointer written before this one
  did (the remote ref first, the mirror best-effort behind it) can leave a copy standing beside a ref that is gone.
  Written `true` by the split that seeds the child and absent otherwise, so an unstamped pointer is answered with one
  read-only `ls-remote` instead of the free local read. Nothing migrates: the stamp is written by the binary that
  would do the reclaiming, so its absence is the whole question answered. `late_declared_scope` is the slice the
  adjudication assigned, and it is what the child's own late prompt states rather than an issue body somebody has
  since edited. Every field is additive and read fail-closed like the generation's own: an issue that reached this
  workflow another way carries none of these keys, and a hand-edited one reads back absent rather than becoming a
  lineage nobody wrote. The ref is checked against the namespace that owns it rather than merely for being a string
  — a value outside `refs/orchestrator/late-split/` names a branch, a tag, or nothing, and handing one to a child is
  worse than handing it none. The record is READ where it matters most: a split refuses outright when the ancestry
  disagrees with the generation's own lineage, because a generation naming a shallower depth or a different root is
  one minted without this record — and a shallower depth is exactly how a lineage would buy itself a generation past
  `MAX_LINEAGE_DEPTH`.
- **Pending owner check.** `late_owner_check_pending` says a completed run's outcome has not yet been cleared by a
  fresh read of the issue it belongs to. It is written *before* that read is taken and dropped when one succeeds or
  the cycle is cancelled, and while it is set no later tick may treat the generation as settled, however small,
  decided, or parked it looks: reconciling it is the FIRST thing a tick does, ahead of the size gate, the hold, and
  any spawn — and while it is set the generation counts as live for the kill switch and the hand-relabel
  guard too, since an undersized revision is exactly the state a size-keyed gate would route out of this mode with
  the read still owed.
  It is durable because nothing else would bring the workflow back to that read — a revision that came back under the
  ceiling routes past the gate and an issue parked for a human routes past everything, so a retry hung off either
  would never run — and it is written ahead of the read rather than out of its failure because a process killed
  mid-read never sees the failure at all. It is written by the COMPLETION rather than by the guard, in the one write
  that records what the run left, and that holds for every completion: a verdict, a re-measured candidate, a timeout,
  an unusable reply, an outcome too large to record, a moved candidate, and a developer reconciliation nobody could
  make. A tick dying on the way to the guard therefore leaves the park standing and the read owed, rather than a
  generation still reading as `adjudicating` that the next tick pays for another agent against. That same write
  carries whatever else the completion staged, which is how the
  durable half of a park gets out ahead of the comment announcing it. See
  [`../workflow/roles.md`](../workflow/roles.md#the-owner-read-a-finished-run-has-to-pass).
- **Accepted candidate.** `late_exempt_sha` is the one commit a `single` verdict let past the size gate, and it is
  the whole of what that verdict is worth durably: the gate measures whatever a stage is about to publish, so a
  candidate handed back with its generation cleared and nothing else would be measured past the ceiling again and
  adjudicated again. It names exactly the commit that was measured, which is also the whole invalidation rule —
  anything committed on top of it is work nobody adjudicated, does not match, and is measured as the fresh candidate
  it is. There is no clearing step to remember and no window in which a stale exemption covers a moved head. The one
  thing that MOVES it is an authorized rewrite — a squash on approval, or the clean base rebase the per-tick refresh
  publishes, whose contribution fingerprints identically to the accepted one, granted by `late_transfer` and recorded
  by the `late_rewrite_*` group below — and that move is a write of its
  own rather than a widening of the match: it belongs to the receipt of the push that landed rather than to the grant
  before it, so a verdict is never left on a commit no remote carries. `late_rotation` stages it into the push tail's
  own write, so the exemption, the identity, the phase, and the account of what the remote holds land together or not
  at all. That
  is also why the pre-tick base refresh reads the CHECKOUT and the LABEL before it decides whether this record
  freezes the branch: a rebase while the head is still the accepted commit, and the gate has still to act on it,
  would have the gate measure the rewrite past the ceiling and re-route a decision a human has already made — while
  freezing on the record's presence alone would take every issue that ever earned a verdict out of the base refresh
  for the rest of its life, review and its rebases included.

  It shares that window with `late_approved_sha`, and the two are not duplicates of each other. The approval is
  written in the same breath and answers a different question: *this commit is owed a push, and no other may be pushed
  in its place*. So it freezes by presence — as the whole pair, `late_approved_lease` included, because the two go
  down in one write and a lease standing alone is the damage the dispatcher parks on a tick later, by which time a
  hold keyed to the commit alone would have rebased and force-pushed the branch that park is about. One approval is
  set aside all the same, and it is the refresh's own rather than a stage's: where `late_approved_lease` IS a
  still-pinned `pending_auto_base_rebase_push_sha`, the freeze would shut the interrupted auto rebase out of the
  recovery that anchor exists for ([Base refresh](#base-refresh)). The late reading freezes the same way, on any of
  the fields the write that mints a generation puts down rather than on `late_candidate_sha` alone, and is never set
  aside. It is proved before anything spawns, and is spent by the publication that lands — durably ahead of the
  relabel that hands the issue to `validating`, since past that label implementing never runs on the issue again and
  nothing else would ever drop it. `late_approved_lease` rides with it and is spent by the same write: the head the
  pull request stood on when the approval was taken, for an approval on the **published** side. The generation that
  froze that head is retired by the very write that approves the commit, and the push it licenses has not run yet — so
  if that push fails, or the settlement hands the candidate to another stage, the only head left to read is whatever
  the pull request has become since, and the retry skips the measurement because the commit is already approved.
  Pinned to what was frozen, a pull request somebody moved in between rejects the push; pinned to what can be read
  now, it is force-overwritten by work measured against the head it used to be on. Read fail-closed like every other
  late commit field, and refused where it cannot be read: an approval on the published side whose lease is absent or
  hand-edited has nothing left to pin against, and the one head still available is the one the lease exists to catch,
  so it parks `late_measurement_failed` with nothing pushed. The one exception is a pull request already standing ON
  the approved commit: the push it licenses has already been made, so there is nothing left to pin and the debt is
  settled instead of parked. Empty is the ordinary answer for a pre-publication approval — which is what every
  implementing-seam approval is, and whose push correctly takes its own reading of the remote. After that the
  exemption is on its own, still saying *this commit needs no measuring* for every later tick that finds the branch
  where the verdict left it — a claim the gate keeps reading and the base refresh stops honouring, since past the
  handoff the branch is review's. Each covers what the other cannot: the approval covers the wait for the push and
  could not survive it without freezing the branch for good, and the exemption covers every tick past it and could not
  be read by presence for the same reason. Read and written fail-closed like every other late field: only a whole git
  object id is one, a `record_exemption` handed anything else refuses rather than writing a value the gate would read
  as a bypass, and a hand-edited field reads back as no exemption at all. The one write that does drop it is a
  restart's projection, which keeps nothing about the attempt that ended: the branch that commit was on goes with it,
  so an exemption left behind would name work the fresh cycle has no way to reach and never adjudicated.

  **What that commit carries.** `late_exempt_base_sha`, `late_exempt_candidate_sha`, `late_exempt_fingerprint`, and
  `late_exempt_fingerprint_format` are the semantic identity of the accepted change, written with the exemption in
  the same pinned write and outside `LATE_STATE_KEYS` on the same terms — the exemption says which COMMIT was
  adjudicated, and these say which CHANGE was, which is the only question left once that commit has been rebased,
  squashed, or made afresh. What licenses a publication is still `is_exempt`, the exact SHA compared whole against
  the commit in hand; what this group licenses is the exemption MOVING onto the commit an equivalent workflow
  rewrite replaced the accepted one with, which the authorization group below records and nothing else grants.
  The pair is the generation's own frozen base and the accepted candidate, and the digest is the canonical
  fingerprint of the contribution between them
  ([`../architecture.md`](../architecture.md#fingerprinting-a-prospective-contribution-gitmeasurementfingerprintpy)),
  taken from the frozen pair the decomposer inspected rather than from the checkout's head or a base read at
  settlement time: the worktree is writable for the whole of an adjudication, so what it stands on is not evidence
  of what a human ruled on. The format version travels with the digest because two ids taken under different rules
  are not comparable and nothing about the ids themselves would say so. The group is read whole or not at all: a
  missing member, a member that is not the shape its field takes (an abbreviated end, a truncated digest, a version
  spelled as text), a candidate that is not the commit `late_exempt_sha` names, a version this build does not
  compute, and a pinned comment written before the group existed each read back as no transferable identity — while
  the exact-SHA exemption beside them goes on exempting the matching commit exactly as it did. A settlement whose
  fingerprint reading could not be taken records none of them and settles anyway, for the same reason: what an
  absent identity costs a later tick is the transfer, never the decision a human already made.
  `record_semantic_identity` refuses on the way in too — a field that is not a whole object id or a whole digest,
  and an identity naming any commit but the exempt one, are not written at all.

  The group belongs to the commit `late_exempt_sha` named when it was written, so `record_exemption` **drops it
  whenever it moves that field to another commit**. Nothing else would: a verdict whose fingerprint could not be read
  records the commit alone and writes nothing over the fields beside it, and those fields match by name — an issue
  that accepts A with an identity, then B with none, then A again with none would otherwise hand A's first digest
  back as what the last adjudication decided, over a base that generation never measured. Re-recording the SAME
  commit keeps it, which is what a settlement resumed between the exemption write and its handoff needs: the identity
  standing there is one its own earlier pass derived from the pair this generation is still frozen on.

  **What authorized it to move.** `late_rewrite_kind`, `late_rewrite_phase`, `late_rewrite_from_sha`,
  `late_rewrite_from_base_sha`, `late_rewrite_to_sha`, `late_rewrite_to_base_sha`, `late_rewrite_fingerprint`,
  `late_rewrite_fingerprint_format`, `late_rewrite_pr_number`, `late_rewrite_source_stage`, and `late_rewrite_lease`
  are the evidence one transfer was granted on, on the
  [`rewrites`](../../orchestrator/workflow/late_split/rewrites.py) owner and outside `LATE_STATE_KEYS` on the same
  terms as the two groups above. They go down BEFORE the push they license and they move **nothing**: the exemption
  and its identity stay on the commit a human ruled on, because the object the rewrite produced is on no remote yet
  and a verdict rotated onto it there would be stranded by a push that failed or a process that died. What this
  group records is the *permission* for a later write to move it — and that later write is the one that receipts the
  landed push, where the exemption, the identity, and the account of what the remote holds go down together or not
  at all. `record_rewrite_publication` is that write, staged by `late_rotation` into the push tail's settlement, and
  it moves all three in one statement — a reader is entitled to find them agreeing, and any two of them apart is a
  comment nothing here can tell from a hand edit. It is held to the record rather than to its caller: only a
  permission this build can read back whole and still finds `authorized` is spent, so a damaged group, one bound to
  a commit this issue does not exempt, and one already `published` each refuse instead of being repaired. Being
  readable is not being *valid*, and the settlement is held to the second as well: what licenses the move is the
  permit `late_transfer` re-asked on that same tick, carried down the push tail beside the commit it proved out for.
  A refusal there is not a hold — the rewrite falls through to the ordinary cumulative gate, and a count under the
  ceiling publishes the same commit — so a settlement reading this group alone would rotate a verdict onto a rewrite
  nothing revalidated and install the digest the permit had just declined. A permission no permit vouched for is
  left exactly where it stands: not spent, and not dropped either, since the remote is now on a head the permit
  accounts for and a later tick whose refusal has cleared can still settle it. A permission the publication went
  PAST — the push put some other commit on the pull request, so the head it was granted against is gone — is dropped
  on the rollback's own terms instead, since what is left is a claim about a push that cannot happen.
  `late_approved_sha` and its lease ride the GRANT's own write instead, and they have to: by then the rewrite has
  already replaced the branch's commits with one, so a comment that explains that commit and does not say a push is
  outstanding is one the next squash reads as *nothing to squash* — reported as success, never measured, never
  pushed. The kind and `late_rewrite_source_stage` are held TOGETHER rather than one at a time, since each is a
  value this build knows and only the pair says whether the record describes a rewrite anything here produced: a
  `conflict_rebase` recorded against `validating`, or a `squash` against `resolving_conflict`, types in both halves
  and names a rewrite that stage does not make. The kind is bounded to rewrites this workflow makes itself, and
  there are three: `squash`, the collapse a reviewer's approval earns; `auto_clean_rebase`, the replay the pre-tick
  base refresh force-pushes once the stage that had to act on the exempt commit has handed the issue on; and
  `conflict_rebase`, the one `workflow:resolving_conflict` runs when a branch has stopped merging cleanly. Each is
  held to the stages its own producer names -- `validating` for the squash, `resolving_conflict` for the conflict
  rebase, and the four the refresh drives for its own -- so a pairing no owner here produces reads back as no
  authorization at all. None of them claims the contribution SURVIVED either: a replay that resolved content
  conflicts and a squash of work nobody adjudicated both carry a kind this build authorizes over evidence that
  fingerprints to something else, and the permit refuses them on the fingerprints rather than on the kind. The
  publication group scopes the whole claim to one push onto one pull request. `late_rewrite_phase` is what says
  whether the move has happened, and every other reading turns on it. It binds the group to the exemption, and which
  end binds follows from it: `late_rewrite_from_sha` while the record stands at `authorized`, `late_rewrite_to_sha`
  once the receipt has moved it to `published`. It is also what a rollback reads — a force-push the remote refuses
  resets the branch back onto the head the rewrite found it on, which the record names as `late_rewrite_from_sha`
  for a squash (it collapsed that commit) and as `late_rewrite_lease` for a rebase (it read the anchor for itself),
  so what the reset owes is dropping the permission it will never spend; an `authorized` record is therefore
  droppable and a `published` one is not. An *outstanding* permission is read two more ways on the tick after a
  crash: it says a push is owed for the commit it names, so the approval beside it defers to the permit rather than
  being spent on the object id, and it says the receipt has not landed, so a remote already standing on
  `late_rewrite_to_sha` is that permit's own push rather than a move somebody else made. Both are asked of the
  phase, not of the commit — the permission and the debt go down in one write for one commit, and a hand-edited
  target would otherwise make the permit invisible. The group is read whole or not at all on the same terms as the
  identity: a missing member, a value that is not the shape its field takes, a kind or a phase this build cannot
  account for, a digest scheme it does not compute, a stage that does not make the kind recorded beside it, and a
  bound end that is not the commit `late_exempt_sha` names each read back as no authorization, which costs a
  rollback the drop and never lets one happen on evidence nobody can check. `late_rewrite_fingerprint` is held to
  the same standard from the other side: a permit re-derives both contributions for itself and refuses where the
  digest already recorded is not the one it took, since a grant that carried on would write its own reading over the
  record — a repair of evidence nobody checked.

  `late_rewrite_proof` sits beside that group and deliberately outside it. It records which reading proved the push
  a settlement was taken on had landed — `pushed` for the leased force-push that moved the pull request off the head
  the permit was granted against, `already_published` for the leased no-op that found the remote standing on the
  rewritten commit already — and it is the one fact nothing later could re-derive, since the receipt looks identical
  either way. `record_rewrite_publication` writes it in the same statement as the move, and the write behind the
  `late_transfer` record it feeds drops it, so a process lost between the settlement and that record leaves the next
  reader something to report from rather than a verdict that moved with nothing anywhere saying so. It is outside the
  group a reader is held to WHOLE because the transfer is settled whether or not it has been reported, and a record
  short of this member is not one to refuse. It is read by PRESENCE all the same: the key standing over a proof this
  build does not know, a phase the settlement never reached, or an authorization it cannot read whole is a comment
  saying two things at once, and every road that asks parks rather than reading it as nothing owed —
  `workflow:decomposing` included, since nothing writes a note it cannot read back and the statement that settles a
  transfer puts the note and the phase down together, so there is no settlement in flight for that refusal to hold
  up. A fresh grant drops it with the transfer it described, since the phase going back to `authorized` is what
  would leave it unreadable beside the new one.

  Every rewrite this workflow settles goes through the same push tail, so every one of them has the window this note
  exists for — the squash a reviewer's approval earns, the replay `workflow:resolving_conflict` publishes, and the
  base refresh's own rebase. Only the last has a recovery route that would come back for it, and the other two
  resume into a stage with nothing to say about a transfer. So the record a lost report still owes is made by
  `late_reconcile`, ahead of every handler on whatever label the resumed issue wears: it settles nothing and stops
  nothing — the verdict moved and the receipt beside it already says which commit the remote holds — and what it
  adds is the account of it, on both sinks, once.

  A note that cannot produce one is the other half of the same seam, and it PARKS there. The reader that makes the
  record answers "nothing to report" for a note over a permission, a phase, or a reading nothing can account for —
  the same answer it gives a comment carrying no note at all — so read no further the reconciliation would walk
  past it, the account would never be made, and the corrupt note would stand for the life of the issue while the
  stage ran behind a verdict nothing here can account for. So `late_claims` asks the transfer's own reader among
  the claims a record can make and fail to produce, on the same five publishing stages the other four are asked on,
  and the refusal takes the once-only park those share. `workflow:decomposing` is asked it beside the publication
  group — the two records that mode did not write and cannot repair — while the reading and the approval are not,
  since it is mid-way through deciding both and has failed to produce neither.

  Being unreadable is not being absent, and the difference is what a WRITER asks. A grant replaces the whole group
  rather than adding beside it, so a group that CLAIMS the commit currently exempt and cannot be read back is
  evidence a transfer may not overwrite to repair: the permit refuses, the exemption stays where the adjudication
  put it, and the rewrite is measured by the ordinary gate until a human settles the comment. A group whose
  `late_rewrite_to_sha` names some other commit is the one exception, and it is not a claim about anything a
  transfer is doing — the exemption moved on since, which dropped the identity and left this group describing a
  commit nothing exempts — so it is replaced without ceremony. Read as a claim it would refuse every transfer the
  issue could ever earn again.
- **Pending collapse.** `late_collapse_head`, `late_collapse_base_sha`, and `late_collapse_count` are what a
  squash-on-approval says it is about to do, written on the
  [`collapses`](../../orchestrator/workflow/late_split/collapses.py) owner and outside `LATE_STATE_KEYS` on the same
  terms as the three groups above — the gate retires the generation a squash is measured under the moment it
  approves the commit, so a record cleared with one would be gone before the push it exists to recover ever
  happened. They go down **before** the reset, and they have to: a squash collapses the approved commits into one
  object with the same tree, so past that reset the head it replaced is off the branch, the base it was read over is
  not derivable from the object that replaced it, and the count is gone with the commits it counted — while what is
  left on the branch is indistinguishable from a branch nobody ever squashed. Read as the second, an interrupted
  rotation takes the *nothing to squash* road and is reported as a success that measured nothing and pushed nothing,
  with reviewer-approved work reaching the merge button neither counted nor on the remote.

  Three fields and no more, because what a recovery may act on is what it can check: the pull request is re-read,
  the checkout is re-proved, the contribution is re-fingerprinted, and the ceiling is this build's own. What the
  record supplies is only what no reading taken afterwards could. The head is the rollback target and the head the
  force-push is leased against; the base is the end both contributions are read from when
  [`late_transfer`](../../orchestrator/workflow/stages/implementing/late_transfer.py) decides whether an
  adjudication's exemption may move onto the rewrite; the count is what the handoff's `:package: squashed N commits`
  notice is worded from.

  Read whole or not at all, like every other late record: a missing member, an end that is not a whole object id,
  and a count no squash collapses (one is the branch a squash *leaves*) each read back as no pending collapse. Being
  unreadable is not being absent here either, and the caller asks both — a comment CARRYING one of those members is
  claiming a collapse it cannot produce, and the branch behind that claim is exactly the one commit that reads as
  having nothing to squash, so the squash refuses rather than reporting success.

  Shape is not enough to ACT on either. Before a resumed publication runs, both recorded ends are peeled as objects
  this host really holds, the base has to be a commit the head was really built on — a walk between two histories
  that never met reports a number like any other, so the count is no ancestry proof — the history between them is
  walked against the recorded count, and the commit on the branch has to carry both the tree the recorded head left
  and that base as its one parent, which is what a squash produces by construction. The parent matters as much as
  the tree: the same tree re-parented onto a base that has since advanced is a commit that *reverts* whatever that
  base added. A record failing any of those is one somebody could have written and this repository never produced,
  so the branch is left exactly where it was found and the tick refuses.

  `late_collapse_count` is the number of commits the branch really carried, walked rather than counted from their
  subjects: `git commit --allow-empty-message` makes a commit that contributes no subject, so a count taken from
  the subjects is short by however many of those there are — and the recovery would then refuse a collapse this
  workflow really made as miscounted.

  The record is ended by the write that ends what it claims and by no other: the reset a rollback made, the reset
  that never ran, and — for a push that landed — the approval handoff's own write, which is deliberately the write
  taken **before** the relabel. The count is what the `:package: squashed N commits to 1` notice is worded from, so
  a notice that was owed and did not post leaves it standing; and past the relabel the issue belongs to
  `documenting`, which never runs the recovery that would answer a claim left there. A tick that dies before that
  write comes back to the same branch and the same answer — an already-published collapse is finished as the leased
  no-op it is, and an untouched branch is squashed afresh.
- **Settled handoff.** `late_collapse_handoff_sha` is what that write leaves in the claim's place, and it exists for
  the one boundary the group above cannot cover: the relabel is a second call, and an issue left on
  `workflow:validating` with the record simply dropped is one the next tick runs a second reviewer on, over a branch
  already approved, squashed, and published. It names the commit the move is owed over, which is the whole of what
  the move needs and the only thing that says it is owed. The validating recovery route reads it ahead of the
  reviewer, moves the label, and drops it in a write of its own behind that label — and spends it only while the
  pull request is still standing on the commit it names, since anything that moved the publication on has moved the
  work past the round the record was about, and the branch then goes to the reviewer rather than on to
  `documenting` unread. It is deliberately NOT a member of the group above: nothing about the rewrite is
  outstanding by then, so it freezes no branch out of base sync and refuses no resume. An approval that collapsed
  nothing leaves none, and neither does one whose commit is not a whole object id: the value is spent on a
  comparison against the head the pull request stands on, so one no commit could equal is one that comparison can
  never catch — and on an issue with no pull request to read, nothing else stands between such a value and a label
  moved past the reviewer. It is read for a usable value rather than for presence, the opposite of the group above
  and for the opposite reason — the worst an unreadable one can cost is the reviewer round the route would have
  saved, so it is dropped and that round runs.
  The record left by a relabel that DID land is ended by `documenting`, at the top of its own tick, and that stage
  is the only owner that can end one: having the issue is the proof the move happened, and the label history cannot
  tell a move that never happened from one a drift unwind later reversed. Left standing there, the unwind's
  re-review would be answered by relabelling the unchanged head straight back to `workflow:documenting`.
- **Sealed consumer ledger.** `split_ledger_sealed` says the register of children a split recorded is FINAL. The
  count written before the first create (`expected_children_count`) is what tells a partial ledger from a whole one,
  and a loop a cancellation stopped can never reach it — so the ref its children were cut from would be held on a
  proof no pass could complete, and the owner's terminal with it. The seal is written only by that loop, and only
  where every child that exists is already on the register: every barrier that ends it is asked after the write
  recording the child in hand, and no further one will be opened. A **resumed** walk stopped before it reached the
  first unrecorded index writes none, because a child an earlier attempt created and never recorded would not be
  on the register and only the adoption lookup can say otherwise. The field holds the **cycle** the seal belongs to
  rather than a bare flag, and is believed by that cycle alone. It is a decomposition key, so the write that clears
  late mode leaves it exactly where it was: read as a bare "yes", a later split stopped mid-loop would look complete
  and release the ref its own unrecorded children were cut from. A drift reset drops it beside the count it is a
  fact about, and so does a restart's projection, which keeps nothing about the cycle whose register it sealed.
  Anything that is not a positive identity reads back as no seal at all — which holds the ref rather than releasing
  it.
- **Applied terminal.** `late_terminal_cycle_id` and `late_terminal_confirmed` are one two-phase record of a cycle's
  `rejected`, and they sit outside `LATE_STATE_KEYS` with the retired cycle for the same reason: clearing late mode
  drops exactly the generation's own group, and this is a fact about that generation an ending writes and a later
  tick reads back. Together they are the only durable evidence that the label an operator removes to authorize a
  restart was ever applied — without it an issue whose workflow label a human stripped mid-cleanup, and one whose
  terminal write GitHub refused, are both indistinguishable from one whose `rejected` was taken off deliberately, and
  the fresh cycle would start on a gesture nobody made.

  Two phases exactly as an external obligation has them. The **decision** — the identity alone — goes down before the
  label write, so a tick that dies in between has something durable to come back to. The **proof** goes down only for
  a `rejected` that actually landed. Only the pair authorizes a restart; an attempt is not a terminal. Recording the
  decision drops any proof standing beside it, since the same field is reused by every cycle an issue ends.

  The proof is reached three ways. `_retired` takes the write **returning**, and has to: PyGithub does not refresh an
  issue's cached labels when `set_labels` succeeds, so reading the label back there would answer with the one the
  issue wore a moment ago — and a closed owner leaves the sweep on that write with no second visit to correct it.
  `_terminal_proved` takes the other side, recording the proof for any visit that *finds* the label on the issue,
  which is what makes the record compatible with cancellations that ended before it existed: such an issue wears
  `rejected` and records nothing, and the first pass to see it writes the proof down.

  `_terminal_recovered` covers what neither reaches. Two records look identical and mean opposite things: the label
  landed and the process died before the receipt, and the label write GitHub *refused*. Nothing revisits the closed
  `rejected` owner the first leaves, so the operator's reopen is the next thing that happens, and their removal makes
  the two indistinguishable locally. A cancellation that ended before this record existed is a third, carrying
  neither half. So the remote's own label history is asked, and the decision is deliberately **not** required to ask
  it — demanding one would leave every pre-field cancellation needing a second removal. What gates the read instead
  is that the issue is unlabeled over a cancelled cycle that owes nothing and has no proof, which also bounds the
  cost: an unlabeled owner whose cleanup is unfinished is visited every tick, while one that owes nothing is written
  back to `rejected` on the same tick it gets no proof, so the walk costs one request per removal rather than one per
  tick.

  What that read asks is which workflow label **this orchestrator** applied **last**, not whether `rejected` was ever
  applied. The actor narrows it because a terminal is a write this orchestrator makes, and a collaborator is free to
  apply and remove the same name by hand — reading one of those back would let somebody outside the workflow forge
  the record of a write it never made, so the events are filtered on the same account the pinned comment is
  authenticated under, and a client that could not establish one answers nothing. The newest narrows it because an
  issue reaches this terminal once per cycle, so a repeat carries an older one in its history, and adopting that
  would authorize a fresh cycle off a removal an operator made a cycle ago. The cycles are separated by construction:
  a cycle exists only because a restart retired its marker, and a restart retires only once its own target label has
  landed as one of *this orchestrator's* applications — which is why a restart that finds the target already applied
  by somebody else takes the name off and puts it back — so a stale `rejected` always has a later application of its
  own standing after it. Control
  labels are excluded too, since a `paused` applied over a terminal is a modifier rather than a state this workflow
  moved the issue to. A history whose newest application is some other state, one naming nothing this vocabulary
  recognizes, and one that could not be read all fall the same way: the terminal is written again rather than a fresh
  cycle started on a removal nobody made.

  The read happens from **behind** the reconciliation, in the same pass and after `_reconciled` has run, because what
  decides whether anything is still owed is the record the ending has just settled rather than the one it found. An
  obligation the ending *discovers* — the branch a supersession left behind and never wrote down, derived from the
  announcement's own receipt — is on no ledger until that pass puts it there, so adopting a proof in front of it
  would let the restart project the branch away with the receipt it was derived from. The cost is one tick: the pass
  that recovers the proof is the one before the pass that restarts. With all three proof paths, the operator's first
  removal is still the one that authorizes the fresh cycle.

  Both fields are read fail-closed (a hand-edited identity, or a `"true"` string, is no proof at all) and both
  are dropped by a restart's projection, with the fresh cycle's own ending writing them again.
- **Approved commit.** `late_approved_sha` is the commit this issue owes a publication and no push has carried yet. It
  goes down in the same write that approves one — the retirement a small candidate earns, the exemption a `single`
  verdict records, and the grant that authorizes a rewrite to carry one of those exemptions over, where the
  permission and the debt have to be one write or neither — and is dropped by whichever handoff spends it (the
  recovery that republishes, or the ordinary
  `validating` advance, which writes the drop durably ahead of the relabel), by an adjudication that supersedes it,
  and by any publication naming a different commit: a debt recorded for work nothing is going to push would freeze the
  branch for the rest of the issue's life. It is a floor as well as a debt — a run resumed on top of that commit has
  to move the head to have committed anything. Like `late_exempt_sha` it names one commit and is deliberately outside
  `LATE_STATE_KEYS`: the generation it came from is retired before the push it licenses, so a record cleared with the
  group would leave nothing on the issue naming the work — which is exactly what a replacement host would then publish
  over. It is proved before anything spawns and it is what the `late_candidate_moved` park is answered by, together
  with a status read that has to prove the tree around it carries nothing. What either
  read does with it is a comparison, never a substitution — a head that cannot be peeled, or one that peels to
  anything else, leaves the park exactly where it is. It freezes the branch out of the pre-tick base refresh for as
  long as it stands, and there the freeze IS the remedy: what settles this park is an operator putting the worktree
  back, so a rebase between their `git checkout` and the tick that would have noticed moves the head off the approved
  commit again and leaves the one park answerable without a comment with nothing left to answer it.
- **Published commit.** `implementing_published_sha` is the commit the last gated push carried — the one that passed
  the gate, or the checkout's own head on a push the switch named none for, since `DECOMPOSE` keeps candidates out of
  the gate rather than off the remote and is an operator's to turn back on. It keeps the implementing spelling it was
  minted under, but it is written by every seam the gate stands in front of, the pushes onto an already-open pull
  request included: each of those has the same window behind it, and a receipt naming what reached the remote is
  what tells a candidate a later tick still owes a push from one it has already made. On that side it is read
  together with the head the gate froze, because it is a local note about a remote fact: a receipt naming a commit
  the pull request has since moved off is a record of a publication that is over, and the candidate goes back through
  the ordinary reading rather than being waved past as already published. For the same reason it is not evidence that
  a pull request found somewhere OTHER than where a caller entered it got there by this issue's own push: it is never
  cleared, so a branch a revert or a rewrite rewound onto a commit published rounds ago would be measured and
  force-pushed over. Only the three readings a live window drops — the candidate a caller names, `late_approved_sha`,
  and a live generation's `late_candidate_sha` — answer that question. It is the same commit the push was named
  against rather than a second reading of the checkout, which could have moved while the push and the pull request
  were in flight; the pre-push half of that decision is durable as `late_approved_sha`, so a tick that died in the
  window leaves a receipt either way. It is written in the same pinned
  write that spends every record the gate decided by and ahead of the relabel that hands the issue to `validating`.
  It exists for the window between those two: a relabel GitHub would not take, or a process that died before it,
  leaves an implementing issue whose branch is on the remote and whose pull request carries it, with the approval and
  the generation both already gone. Read as work nobody has ruled on, that branch is measured again against a base
  that has moved and a ceiling that may have been retuned, and an oversized answer would route it to adjudication —
  with the push and the pull request already made, which is the one outcome the size gate exists to prevent. Recorded,
  the next tick recognizes the commit, publishes it without a reading, reuses the pull request that already carries
  it, and lands the label. Like `late_exempt_sha` it names one commit and only it, which is the whole invalidation
  rule and why there is no clearing step: work committed on top is work this stage has not published and is measured
  as the fresh candidate it is, and the next publication overwrites it. It freezes the base refresh on exactly the
  terms `late_exempt_sha` does, and for exactly as long: the refresh reads the CHECKOUT and the LABEL, so the branch
  is held still only while the head is still this commit *and* the issue still carries a deciding-stage label. That
  window is the one the record exists for — between the push and the relabel the branch is on the remote, its pull
  request carries it, and a rebase there would move the head off the commit the next tick has to recognize, leaving
  it to re-decide a published branch. The freeze ends with the handoff: past `workflow:validating` the label no
  longer matches, and keeping a pushed branch in step with base is the PR-aware sync's own job, which is the only
  route that can move it without stranding the reviewer's SHA.
- **The head that publication replaced.** `implementing_published_lease` is written with the receipt above and never
  on its own, exactly as `late_approved_lease` is written with its approval: it names the head the recorded push was
  PINNED to — the one the gate's entry froze, or, for the accepted push a `single` settlement makes from its own
  approval, the lease beside that approval. An initial publication froze no head and records none. It exists because
  the receipt cannot date itself. Read alone the receipt goes on naming a commit this stage pushed rounds ago and so
  vouches for any pull request somebody rewound onto it; read with the head it replaced it answers for one window and
  no other — a push made from the head a caller froze, on a tick that died before the relabel behind it. Both the
  size gate's entry and the `single` settlement's own reconciliation ask for the pair, and a receipt whose head is
  absent or names some other commit forgives no moved head at all. Cleared with every receipt that is written rather
  than left for the next one to inherit, since a receipt wearing an earlier attempt's head is the one that vouches
  for a publication somebody else moved.
- **Retired cycle.** `late_retired_cycle_id` is the one fact about a dropped generation that outlives the drop: the
  write that clears late mode records which cycle it was clearing. It exists for a single window — a poll observing
  the close *inside* that write leaves a cycle-scoped receipt on the thread, and the record it would be adopted
  against has just stopped naming a cycle, so a process that dies before its own post-write barrier would strand the
  observation with nothing left to correlate it to. A record carrying the stamp and no generation is asked once per
  owner per process whether the thread has that cycle's receipt; one that does gets the cycle put back, cancelled,
  with the ledgers the retirement carried across, and the ordinary ending runs from there.
  Every retirement that drops a cycle records it — the `single` publication's, the umbrella terminal's, and the size
  gate's own drop of a candidate it measured at or below the ceiling, which needs it for the same reason: the barrier
  behind each write belongs to the process that made it. All three take the same window around that write, and the
  gate's is the one with the most to lose behind it: past its retirement come a pushed branch, an opened pull
  request, and a relabel to `workflow:validating`, so a close dropped in that interval would hand a closed issue to
  review. The latch is asked ahead of the write and the window's own answer behind it, and a close either side of it
  ends the cycle instead — cancelled, from the generation still in the call's own memory, with nothing published to
  take back. It is also what the next candidate on the issue mints its
  cycle after, so a measurement taken after one that published cannot answer to the number that one did.
  It names **one** window and outlives no other, because the receipt it reads is a comment and comments are
  append-only: a correlation left standing would let a cycle-scoped receipt be adopted against a record whose cycle
  is two generations newer, moving a completed owner from `done` to `rejected`. Two rules end it. A generation
  written with an *identity* supersedes it — which is both the adoption consuming its own marker (the mark it writes
  puts the cycle back) and an operator's authorized restart superseding it. So a terminal that retires cycle N names
  N and nothing else, and a receipt for any earlier cycle on the same thread matches nothing an adoption would read.
- **Cancellation.** `late_cancelled`, `late_cancelled_at`, and `late_cancelled_phase` are irreversible within a
  cycle: once the owner has been observed closed, a later tick that sees it reopened re-marks the same cancellation
  and moves none of the three. Two passes observe it. The post-agent owner guard is one — a fresh read taken after
  every completed late run, before anything it earns happens, and taken again inside the split transaction before
  every step the remote keeps: each child it creates, and the announcement, supersession, and activation behind
  them. A child is the one thing created here that nothing takes back, and a close a poll saw while that worker
  held the issue reaches no other pass on the tick it happened. The closed-owner cleanup sweep is the other, and it
  is what catches a close at any of the boundaries no agent was running at — a measurement, a hold, and
  everything past the transaction — as well as one the scheduler could admit no worker for, which the dispatcher
  holds and this sweep takes on a later tick. Either writes the mark durably *before* any external effect and
  emits `late_cancellation` from that write, so the record is one per cycle rather than one per visit. What the
  remote is still owed stays on the two ledgers for the
  [cleanup path](delivery-stages.md#closed-owner-cleanup-sweep-no-label-of-its-own) to settle, and only once it has
  does the owner reach `rejected`. A cancelled cycle is nobody's to adjudicate, relabel, or route, so a reopened
  issue does not get this cycle back. The dispatcher's own pinned-state guard catches a reopened owner, runs the same
  cleanup, hands it to no stage handler, and writes the same `rejected`
  ([delivery-stages.md](delivery-stages.md#the-reuse-guard-every-dispatch-ahead-of-every-handler)) — reaching that
  terminal is the only way back into ordinary work, and what authorizes the fresh attempt is an operator removing
  the label rather than a human reopening the issue. That guard refuses a cancelled cycle under *every* label it can
  be wearing, since each one names a handler that would act on the issue rather than end it, and it writes the
  terminal wherever the graph declares the edge from — plus `ready` and `blocked`, which the cycle's own decomposer
  writes as its ordinary outcome and which no query would ever bring a tick back to. The unlabeled state is refused
  with the rest of them: the [restart](#late-generation-state) is asked one guard ahead, so an issue reaching the
  refusal with no label is one the restart already declined — and letting it fall through would hand a cancelled
  cycle to the pickup path, which greets it as new. That ordering is also what keeps a restart between its label
  write and its retirement safe: it wears a live-looking label over a record that still says cancelled, and the
  refusal would answer that by handing the issue `rejected` again.

  What the unlabeled state still decides is whether the terminal may be written from it, and the RECORD answers
  rather than the label, because three different issues wear the same nothing. One is the handshake — an operator
  took `rejected` off, and re-applying it would undo the only authorization a restart has. Another never got the
  terminal at all: a human who strips a workflow label mid-cleanup leaves the ending owed under a state it cannot be
  written from, so every visit since has settled obligations and stopped. The third had it attempted and refused. The
  *proof* half of the terminal record separates the first from the other two.

  `late_cancelled_phase` is the boundary the cancellation interrupted, kept because `late_phase` becomes
  `cancelling` and that answers nothing: whether the consumer ledger accounts for every child cut from this
  generation's snapshot is read off the phase whenever the record cannot prove that for itself, so a record that
  forgot where it was cancelled from could never prove a ref reclaimable again. A cancelled record carrying no such
  boundary — one an older binary marked, or one whose own field was damaged — reads as unprovable and keeps the
  ref. The boundary an interrupted transaction stood at is kept rather than rewound: no boundary before a split --
  `measuring`, `holding_plan_pr`, `adjudicating`, `owner_check` -- is ever written over `snapshotting`,
  `splitting`, or `superseding`, and the record refuses that move itself rather than each writer remembering to,
  since a transaction re-entered after a crash comes back through every retry above it. A child is created before
  the write that records it, so the phase is the only thing that says a loop was in flight when nothing is recorded
  yet. Beside that, a pre-split phase is believed only as far as the record bears it out: one standing beside a
  recorded consumer, a split child,
  or the stage's own `expected_children_count` — written in the same durable step as `splitting` — is a partial
  split wearing an earlier name, and its ref is kept. The count is asked of every boundary before the phase is
  consulted at all, since a record it proves finished is whole wherever it stands — which is what answers both
  `splitting`, written before the first create and again beside every child recorded, and a `snapshotting` a
  retry rewrote over a split that had already finished. It is also what upgrades a pinned comment an earlier
  binary rewound, since nothing migrates records already in flight.
- **Pending restart.** `late_restart_pending`, `late_restart_target`, `late_restart_cycle_id`, and
  `late_restart_predecessor` are written before the restart's own external effects by the
  [`restart`](../../orchestrator/workflow/late_split/restart.py) owner, and beginning a restart is create-or-keep — a
  marker already names the cycle it intends, so a crash between the write and the label resumes that cycle rather
  than minting a second one. *Believable* is the condition on both halves, and it takes the whole marker: the pending
  flag is set, the target is one of the two labels a restart may apply (`workflow:decomposing` /
  `workflow:implementing`), the predecessor is exactly the cycle the record is on, and the pending cycle is exactly
  the next one after it. A marker failing any of those is a damaged field rather than a restart in flight and is
  re-minted from the current cycle — honoring one would hand the fresh attempt a number an audit record never issued
  (cycle 2 with a pending 99), an ancestry nothing wrote (a predecessor of 500 under cycle 2), or a label nobody
  chose. The *requested* target is checked before any of that, so a marker already standing never excuses an argument
  outside the pair. Retiring is the one step that refuses instead of re-deriving: the fresh cycle keeps no ledger, so
  `retire_restart` raises while any obligation is still pending, retained, failed, or of a shape this binary could
  not read — restart is reachable only from a cancellation whose cleanup completed, and retiring over an unsettled
  ledger would discharge the obligation by forgetting it. `restart.obligations_settled` is the same question a caller
  can ask first. What it projects when it does retire is a fresh cycle keeping only the identities that link it to
  the one before: the cycle it is, the issue and root it belongs to, and the cycle it succeeds — with the lineage
  depth back at the root's 0 and the generation counter back at 0, because a restarted issue is a fresh attempt with
  room to split rather than a cancelled one wearing a new number.

  Who writes those fields is
  [`late_restart.py`](../../orchestrator/workflow/stages/decomposition/late_restart.py), the stage owner the
  dispatcher asks ahead of the cancelled-cycle refusal. What it requires before it writes anything is a *settled*
  cancellation — a cycle that exists, one a close already ended, and one that owes nothing — plus proof the terminal
  was applied (`late_terminal_confirmed` beside this cycle's `late_terminal_cycle_id`) and the gesture that removed
  it: an OPEN issue wearing no workflow label at all. Both halves are needed, because the gesture, a workflow label a
  human stripped mid-cleanup, and a terminal write GitHub refused all leave the issue looking identical, and only the
  record separates them.

  "Owes nothing" is both readings, not either: the ending's own outstanding list and `obligations_settled` overlap
  without containing each other, so the guard asks the cancellation owner as well as the domain. Only the ending
  reports a `late_plan_pr_number` standing with no `late_plan_pr_body` beside it — a hold nothing can prove, carried
  on no ledger, and repairable only by a human — and projecting the fresh cycle over one would delete the last thing
  on the issue naming a pull request this orchestrator left marked and open. Only the domain counts an undischarged
  child receipt or a consumer ledger this binary could not type, and a restart that reached its retirement over one
  of those would be refused there with the marker already down and the label already applied. Asking both is also
  what keeps this guard and the refusal behind it exactly complementary: the tick one stops is the tick the other
  runs its ending on. A marker already standing answers the gesture for itself
  whatever label the issue now wears, since only this owner writes one and only after the authorization was proved.
  Everything else is inert: an unlabeled issue with no late cycle is an ordinary pickup under the ordinary rules, a
  cancellation still owing the remote is the cleanup path's, and a closed issue is the sweep's.

  `ALLOWED_ISSUE_AUTHORS` is deliberately not asked on this path. It guards the one route a stranger reaches on
  their own — an unlabeled issue picked up automatically — and nobody reaches a restart that way: it takes a pinned
  comment only this orchestrator writes and a label removal GitHub grants only to a repository's own people. The
  fresh attempt is authorized by whoever made that gesture rather than by whoever filed the issue, so an outsider's
  issue an operator has decided to restart is restarted.

  Two identities are repaired before anything is written, and neither is the cycle: `late_current_issue` becomes the
  issue the pinned comment was read off, because a field naming another one would file the fresh cycle — and both
  sinks' records of it — under an issue the pass is not about; and `late_root_issue` is kept where the record is this
  issue's own and re-derived from `late_ancestry_root_issue` (falling back to the issue itself) otherwise, because a
  root naming no issue is a record the telemetry contract refuses outright, which would let a restart run to
  completion saying nothing about itself. On a healthy record both are the values already there.

  The transaction is three ordered steps over the one pinned comment, and each is idempotent so a crash resumes at
  the step that is still owed. The marker goes down first, `late_phase` moving to `restarting` with it. Then the two
  external effects: one notice on the thread, suppressed by a marker scoped to the cycle being minted, and the
  target label, skipped where the issue already wears it *and this orchestrator is what applied it*. Where somebody
  else applied that same name, it is taken off and put back instead: what the write leaves behind is not only the
  label but the bot-authored application separating this cycle from its predecessor's terminal, which
  `_terminal_recovered` reads below, and GitHub records no event for a label already present. A notice an earlier
  pass posted is *adopted* rather than
  merely recognized — posting tracks its id in memory and the write that would make it durable is the retirement two
  steps later, so a pass whose label or retirement then failed left the id nowhere, and the marker suppresses the
  repost that would have tracked it again. Only once both effects have reconciled is the marker retired.
  Which label is applied is the current `DECOMPOSE` setting's answer at the moment the marker is written and the
  *record's* answer from then on, so a restart begun under one setting and resumed under the other finishes the
  label its own notice announced. A refusal from either effect is logged, recorded as a `late_failure` carrying
  `restart_failed`, and left for the next visit with the marker standing; `backlog` / `paused` defer the whole of it,
  since the authorization sits on the issue's own surface and cannot be lost.

  The retirement's own write is the **projection**, and it is a whitelist rather than a list of deletions: every
  stage shares this comment and each adds keys of its own, so naming what to drop would carry whatever the naming
  was not written for. What survives is the pinned comment's own identity (the payload is rewritten in place rather
  than a second comment minted), `orchestrator_comment_ids`, the four cumulative `issue_*` usage counters, the two
  agent-run ledger fields the `run_ledger.py` owner names as its projected group (`agent_run_allowance` and
  `agent_runs_used` — a lifetime ceiling a restart handed back would be no lifetime ceiling at all, and the allowance
  travels beside the count so a fresh cycle is not parked on its first run by a ceiling the projection dropped), and
  the fresh generation's own identity. Everything else goes — every session id, `pr_number` and `branch`, `children` /
  `dep_graph` / `expected_children_count` / `split_ledger_sealed`, the whole `late_ancestry_*` group and the
  exemption beside it with the identity it carries, `awaiting_human` / `park_reason`, `user_content_hash`, the
  retry, review-round and park counters, `agent_run_reservation` (a launch, not a fact about the issue — the fresh
  cycle has none),
  `agent_run_limit_notice` beside the park it explains (an obligation is a claim about one park, and the sentence it
  carries quotes a spend the fresh cycle will re-read for itself), and every
  timestamp.

### The late run

The keys above are the late-split DOMAIN's, and they describe the generation. Beside them sit the `decomposing`
stage's own, which describe the RUN that adjudicates one:

- `late_agent` and `late_session_id` — the locked spec and the session it opened.
- `late_agent_role` — the role the run was recorded under (`decomposer` for the adjudication itself).
- `late_run_cycle_id`, `late_run_generation`, and `late_source_sha` — the cycle, the generation, and the exact commit
  the run was spawned against.
- `late_result_verdict`, `late_result_category`, `late_result_question`, and `late_result_children` — what it
  completed with: the verdict, the category beside it, the sentence a `question` asked, and the ordered child
  manifest a `split` decided on.

They are written by [`late_session.py`](../../orchestrator/workflow/stages/decomposition/late_session.py) and are
deliberately NOT in `LATE_STATE_KEYS`: clearing late mode drops exactly the domain's group, and a locked backend
outlives that reset the same way `decomposer_agent` outlives a drift-driven session reset.

How much of a held PR's description may be preserved is decided by what the run still has to record beside it. The
write that starts the run has no safe failure — parking is another write of the same oversized comment — so before a
description is replaced, the whole prospective comment is rendered with the run's record already in it: the spec this
issue is locked to (an operator's command line, bounded by nothing here), the identities, and the bounded session id
a finished run pins. A description too long to hold with that beside it is refused while nothing has been touched.

`late_session_id` is dropped by a fresh spawn and KEPT by a resume. One late run in three is a resume: a human's
substantive answer to a categorized question continues the conversation that asked it, so the pinned id survives the
pre-spawn write and is passed to the CLI. Every other run drops it, so a backend that surfaces no id of its own
cannot leave the next tick resuming the run this one replaced. Which it is takes both halves — the caller says it is
carrying an answer, and the record says that session really ran against this cycle, generation, and commit, since a
session opened before a revision replaced the candidate holds a conversation about work nobody is adjudicating.

The identity fields are written before the agent starts, and that write deliberately carries the retry accounting
unchanged. `retry_count` and `retry_window_start` are incremented in memory to gate the spawn, but they become
durable only on a path that records what the run decided — so a run the tick then declines (an operator's `paused`
label, a shutdown sweep) costs the issue's daily budget nothing, exactly as a declined run in every other stage does.
The session id is pinned at the two exits that persist, a timeout and a completed reply, and at neither of the two
that do not.

The three identities beside the result are what makes a recorded verdict believable. A run's answer decides the
candidate it names — its own cycle, its own generation, AND its own source commit. The cycle is required because the
generation counter is not unique without one: a restart mints a fresh cycle and puts the counter back where it
started, and these run keys are outside `LATE_STATE_KEYS`, so they survive the clear that ends the old cycle. Without
it, generation 1 of a restarted cycle would read generation 1 of the cancelled one's verdict as its own. A result
taken against a commit that has since been replaced is not this candidate's either, and a fresh spawn drops the
previous result before it starts. Together they are what keeps a tick that crashed after a finished agent run from
paying for a second one — a second run is not free, and it is free to decide differently — and what keeps it from
acting on the wrong answer instead.

A result records the WHOLE of what its verdict decided, and is read back as an answer only while it does. A `single`
needs nothing beside itself. A `question` carries the category it was asked under and the sentence it asked, because
announcing it is that outcome's own external effect. A `split` carries the ordered child manifest, because the
manifest *is* what a split decided — a marker without it would refuse to re-run the adjudicator while the answer it
stood for was gone. The agent's rationale is the one part deliberately not kept: it is prose, it belongs on the issue
thread, and nothing acts on it. A recorded manifest is rewritten from the three fields a child issue is created out
of, so nothing an agent put beside them travels into the comment humans read.

Half of an outcome is not one, in either direction. On the way in, what is measured is the whole comment the write would
produce — the preserved held-PR body and every other stage's keys included, since a result small on its own can still
be
the one that pushes the comment past what GitHub accepts — and an outcome past that budget (`MAX_RECORDED_BODY`,
GitHub's limit less headroom for the keys other stages still write) is refused *whole* rather than shortened: a
truncated question asks something nobody said and a truncated manifest names children nobody proposed. The issue parks
instead of being left decided in a way no later tick could see, and learning the same thing from a failed write would
mean the agent had already been paid for. On the way out, an incomplete record reads back unanswered: a `question` with
no sentence or no category, and a `split` with no manifest or one the split validator refuses, would each suppress the
next spawn and then have nothing to announce or create. Every field is read through the same defensive readers the
domain's are, so a damaged `late_result_verdict` reads back the same way — unanswered — because publishing on a verdict
nobody recorded is not recoverable.

A park this mode leaves is attributed durably, because the next attempt has to tell its own park from another stage's.
Most of them are *superseded* by the attempt that follows: a hold that failed has been reconciled by the time the
retry gets there, a missing worktree is back, a run that timed out or answered unusably is about to be re-run, and
each of the three split-transaction steps — `late_snapshot_failed`, `late_children_failed`, and
`late_supersession_failed` — is about to be reconciled again from the same recorded verdict, at no agent's cost.
Those
are
retired — `awaiting_human` and `park_reason` cleared — the moment the hold reconciles, ahead of both the spawn and the
reuse of a recorded answer, because `awaiting_human` is exactly what suppresses the announcement a question verdict
earns. A stale one would silence a question durably recorded and never said out loud — whether this attempt produced it
or a crashed run recorded one whose comment never reached the issue. Six are not retired, because none of them is a
step that failed. `late_question` is the announcement itself, and the issue really is waiting on the human it names;
`late_content_drift`, `late_revision_dirty`, `late_revision_unmeasured`, and `late_revision_unanswered` are the
workflow waiting to be told what an edited scope, a worktree the developer left changed, a candidate nobody could
measure, or a developer that changed nothing and vouched for nothing now means. Retiring one of
those would drop the very state the next tick reads to tell a human's answer from the silence before it.
The shared `retry_cap` is the sixth and the plainest: a retry is exactly what it refuses, so an attempt that
superseded it would clear the flag and meet the same spent budget one step later — saying the same sentence once a
poll and taking down, in between, the park a human has to answer. It is held at the top of the adjudication instead,
behind only the reconciliations an earlier tick left owed and the live-generation gate, and while it stands the tick
ends there having proved no evidence, re-marked no pull request, read no thread as an answer, spawned nothing, and
written nothing. What lifts it is a trusted `/orchestrator continue` and nothing else — not an edited body, not the
clock reaching the end of the window, not an untrusted account's copy of the command, and not a `paused` tick, which
never reaches a handler at all.
`late_owner_unreadable` is left out for a different reason again: it IS answered by a retry, but by the pending-check
reconciliation that runs ahead of all of this, and that step reads the standing reason to decide whether it owes the
thread a follow-up — so retiring it here would erase the only durable evidence that this mode had said anything to
retire — and what
each of those answers earns is in
[`../workflow/roles.md`](../workflow/roles.md#what-the-humans-can-still-change-while-a-candidate-is-frozen).
The same attribution is what keeps a park idempotent: reconciliation is retried on every eligible tick, so a park
already standing for the reason being taken again — including one this tick retired and is re-taking unchanged — is
written but not announced a second time. What is suppressed is the notice, not the park. "Unchanged" is the whole
claim, so the two things that could change it end the suppression: an agent RUN, after which a second categorized
question or a second unusable reply says something the first notice did not, and a human's ANSWER, after which
whatever parks next is news even under the same reason. Only the reconciliation retries that spawn nothing and find
the same wall stay quiet — suppressing the others would leave an outcome recorded, durable, and never said out
loud. A retirement is a state
change like any other, so the one branch that would otherwise return without writing — the reuse of a recorded
answer that owes no announcement — persists it rather than clearing a park only in memory.

A revised candidate nobody could measure is this mode's own reading, and it parks on the FIRST miss whatever step it
stopped at. `late_revision_unmeasured` is the reason, neither guard reads the other's — the size gate's silence is
scoped to a standing `late_measurement_failed`, and this mode's to its own list — so the bounded quiet retry the two
transport steps earn at the gate buys nothing here: a remote that would not answer for the base asks a human on the
tick it happens, and `late_measurement_miss_count` and `late_measurement_failure` above go on describing the gate's
own readings, so a miss taken once the issue is back there starts a bound of its own. What the two reading roads do
share is the record: the same `late_failure` carrying `measurement_failed`, with the same `measurement_failure` step
and `detail` line beside it, under `stage: decomposing` because that is where a re-measurement is taken
([`../observability/event-streams.md`](../observability/event-streams.md#late-split-records-both-sinks)). And what
each suppression is keyed on follows what re-takes the park: here a reconciliation that spawned nothing and met the
same wall, which is the same sentence under the same REASON, and at the gate the pair the post-publication
reconciliation re-reads once a poll, which is the same sentence only while those readings go on stopping at the same
STEP.

The record goes out before the effect it earns, and the owner read goes out between them. A question is written and
persisted BEFORE the comment announcing it, so a crash between them costs one repeated comment — the window every
park in this repository has — and never a second run of an agent that already answered; and the announcement itself
is made past the guard, so a question is not posted to a thread somebody closed while the agent was answering it. The
next tick reconciles the announcement from `awaiting_human`: a recorded question the issue is not yet waiting on a
human for is posted from the question the record kept, rather than re-earned.
