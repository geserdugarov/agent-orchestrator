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
  only specific `awaiting_human` session-failure parked *retry* flows: pausing is never a `park_reason`, so a continue
  command is not an un-pause and does not clear `paused`. It is unrelated to un-pausing, but not exempt from it — the
  hard skip fires in `_process_issue` before any handler, so a continue comment posted on a paused issue is deferred
  with everything else until the label is removed.
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

Three of those edges belong to the late size gate and are declared ahead of the handlers that write them.
`workflow:implementing → workflow:decomposing` is the route a clean committed candidate measured past the threshold
takes instead of publishing — adjudication runs under the existing decomposing label rather than a state of its own.
The existing `workflow:decomposing → workflow:implementing` edge beside it is the way back that a `single` verdict
takes, carrying the exemption naming the adjudicated commit, so the ordinary publication reconciles that exact commit
the way it does for any other change.
`workflow:decomposing → rejected` and `workflow:umbrella → rejected` are the one terminal a late generation whose
owner was closed mid-adjudication reaches, once its external cleanup is reconciled, under whichever of the two labels
it had reached; they are also the only way a pre-PR state reaches a terminal at all. A restart after such a
cancellation needs no edge of its own: the operator authorizes it by *removing* `rejected`, so the label a restart
applies is written from the unlabeled entry, and both terminals keep their empty edge set — a rejected issue left
labeled stays inert. The pinned state those transitions move an issue through is
[below](#late-generation-state).

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
  cannot deadlock those children.
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
convict a branch nobody touched. So are the three records that freeze a branch on their own: the two a discussion
tick writes BEFORE the thing they describe (`discussion_round_open` and `discussion_publishing_sha`, which a tick
that died mid-round leaves standing with no park at all, and with the commit it died holding on the branch), and the
`read_only_baseline_sha` the guard writes in place of a park it clears, which stands until the dev run commits.

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
eight sweep labels: `workflow:implementing`, `workflow:documenting`, `workflow:validating`, `in_review`,
`workflow:fixing`, `workflow:resolving_conflict`, `question`, `discussion`. Each is queried under its pre-namespace
spelling too, because a closed issue is the one case no other pass revisits: on a repository whose labels the
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

Pre-PR labels (`workflow:decomposing` / `workflow:blocked` / `workflow:umbrella` / `workflow:ready`) are not swept
closed — a closed issue at those stages is a hard human stop until an operator relabels.

The closed-issue sweep issues one closed-issue query per sweep label the repository actually carries, per repo, every
tick — a fixed request cost that drives GitHub primary-rate-limit exhaustion on multi-repo hosts. A pre-namespace
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

`done` and `rejected` are terminal no-ops. Every handler receives the active `RepoSpec`, so `git worktree add`,
`git fetch <spec.remote_name> <spec.base_branch>`, push-token resolution, and PR-base selection all flow from the spec.

### Pinned state

Per-issue durable state lives in a single **pinned comment** on the issue (`<!--orchestrator-state {...json...}-->`).
The schema is defined by `read_pinned_state` / `write_pinned_state` (see `github.pinned_state.PINNED_STATE_MARKER` /
`PINNED_STATE_RE`). `read_pinned_state` trusts a comment as state only when it is authored by the account backing the
orchestrator's token AND its whole body is the marker, so neither a third party's forged marker nor an ordinary
bot-authored comment that embeds the marker in prose can preempt state (see
[pinned-state authentication](../security.md#pinned-state-authentication)). The keys that matter for the state
machine fall into a few groups:

- **Agent identity.** `dev_agent` + `dev_session_id` (locked dev session — see
  [in-flight session lock](../workflow/command-specs.md#in-flight-session-lock)),
  `review_agent` (traceability only; reviewer is fresh per round), `decomposer_agent` + `decomposer_session_id`
  (parents), `question_agent` + `question_session_id` (`question` stage), `discussion_agent` +
  `discussion_session_id` (`discussion` stage), `late_agent` + `late_session_id` (the late adjudication of an
  oversized committed candidate — see [the late run](#the-late-run) below). The last four pairs are separate pins on
  purpose: each seeds from `DECOMPOSE_AGENT` on its own first spawn and is then locked independently of the others on
  the same issue, so a flip of `DECOMPOSE_AGENT` between two rounds can neither move a conversation onto a backend
  that never ran it nor hand that backend a session id it never issued. The three conversation pairs also *resume*
  their own session id on a human reply, and the late pair resumes on exactly one: a substantive trusted answer to the
  categorized question the adjudicator asked, which is a reply to the agent that asked it. Every other late run is a
  fresh conversation against the frozen candidate — see [the late run](#the-late-run) for the two conditions a resume
  takes.
- **Decomposition.** `children`, `dep_graph` (`{child_idx_str: [child_idx, ...]}` — GitHub has no first-class blocks
  relation), `decomposed_at`, `pickup_comment_id`.
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
  base-sync flow — every PR-stage handler short-circuits when `park_reason in _AUTO_REBASE_PARK_REASONS`. The late
  size gate re-sets its own reasons for the same kind of reason: `late_plan_pr_hold_failed`,
  `late_generation_incomplete`, `late_worktree_missing`, `late_worktree_mutated`, `late_adjudicator_timeout`,
  `late_manifest_invalid`, `late_result_unrecordable`, `late_owner_unreadable`, `late_pr_unreconciled`,
  `late_snapshot_failed`, `late_children_failed`, `late_supersession_failed`, `late_content_drift`,
  `late_revision_dirty`, `late_revision_unmeasured`, `late_revision_unanswered`, and `late_question` — see
  [the late run](#the-late-run) for which of them the next attempt retires. `late_owner_unreadable` is the one of
  them that recovers on its own: it is a GitHub read that failed after the agent had already answered, so the retry
  re-reads rather than re-running anything, and the tick that finds the issue readable again posts the same one-time
  follow-up a transient `validating` park does — before the write that clears the park, so a crash between them loses
  the write and not the sentence. What drives that retry is `late_owner_check_pending` on the generation rather than
  the park itself, which is also why this reason is taken only when the issue is not already parked on something a
  human has to answer. On an issue that already is, the notice that other park staged is still said when the reason
  it stands on is one no later attempt supersedes — the four revision and drift parks — since nothing else ever
  would, and an `awaiting_human` with no sentence behind it stands for as long as the read keeps failing.
- **Undelivered park notice.** `late_park_notice` is the `{reason, message}` a late park has recorded and not yet
  said. The flag is durable before the comment is posted — a comment GitHub refuses must not take a finished run's
  result with it — so without this field a refused post leaves an `awaiting_human` nothing can tell from one whose
  comment landed, and every later tick reads the flag, takes the human as told, and says nothing. It is written
  beside the flag on the same write and dropped by the post that discharges it, so a park whose sentence is still
  owed is never counted as a repeat and is re-said at the top of the next eligible tick. It is matched against the
  standing `park_reason` (a notice for a park something has replaced or answered is dropped rather than said), left
  to the fresh attempt for the reasons that attempt supersedes, dropped when the cycle is cancelled, and refused
  whole — loudly — when it would not fit the pinned comment, the same budget a recorded outcome is refused past. Not
  one of `LATE_STATE_KEYS`: a park outlives the generation that took it. Owned by
  [`late_notice`](../../orchestrator/workflow/stages/decomposition/late_notice.py).
  It is a claim about the thread, so the thread settles a disagreement with it. The post and the write recording it
  cannot be one operation, so a write that failed after a post that landed leaves the field claiming a sentence is
  owed to an issue that already has it — and the first thing a tick does is look for that sentence among the comments
  above `last_action_comment_id` (the mark a park's own mention ratchets, and only on a write that landed, which is
  what scopes the search to this episode). One found there discharges the obligation and repairs the watermark to it.
  Without that step the redelivery would repeat a comment, and — worse — the owner guard would read the standing
  obligation as proof nobody was told and clear its park without the recovery follow-up it promises.
- **In-review watermarks.** `pr_last_comment_id` (issue thread + PR conversation, shared IssueComment id space),
  `pr_last_review_comment_id` (inline PR review comments), `pr_last_review_summary_id` (PR review summary bodies). Only
  non-empty `CHANGES_REQUESTED` or `COMMENTED` review IDs ever advance the summary watermark; `APPROVED`, `DISMISSED`,
  `PENDING`, and empty-body reviews are filtered before the bump.
- **Final-docs handoff.** `docs_checked_sha` + `docs_verdict` (`updated` / `no_change`) set by `_handle_documenting`'s
  success exits. `ready_ping_sha` records the head the in_review handler already posted a `:bell:` HITL ping for.
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
  AT that SHA predates this stage — which is what `read_only_relabel.py` reads to let a discussion held on an
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
  runs, and once the dev commits, HEAD moves off it and the key is dropped. `publication._advance_to_validating`
  spends it too, since an issue leaving for `validating` has published and would otherwise carry the key — and
  everything the key holds — out of this stage with it.
  Standing beside `discussion_plan_sha`, it is also the record that says a handoff was ACCEPTED and nothing here has
  published since, which is a state a crash can leave an issue in for polls at a time: the write lands before the
  developer runs and an interruption drops everything staged after it. While it stands,
  `read_only_relabel._reconcile_open_plan_handoff` takes the guard's own reading again on every tick — the same plan
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
  head, or park as `auto_base_rebase_push_failed`.
- **Counters / timestamps.** `retry_window_start` + `retry_count` (24h fresh-spawn budget shared between implementing
  and decomposing), `silent_park_count` (dev-session silent-park counter), `dev_resume_count` (per-dev-session resume
  budget; once it reaches `DEV_SESSION_MAX_RESUMES` the session is retired and respawned fresh from durable state, reset
  to 0 on every fresh spawn), `merged_at` / `closed_without_merge_at` terminal stamps, and the per-round stamps
  `last_question_at` / `last_discussion_at` the two operator-applied conversation stages set on every run they settle.
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
  the measurement, the reconciliation phase, the local content fingerprints, the held plan PR, the external-resource
  ledgers, the owner read a finished run still owes, the cancellation marker, and the pending-restart marker. The one
  commit an accepted candidate publishes under sits beside that group rather than in it, since clearing the generation
  is exactly what it has to survive. Every key, and what an absent one means, is in
  [Late generation state](#late-generation-state) below.

The legacy `codex_session_id` key (written before `dev_agent` existed) is still honored on read by `_read_dev_session`:
it round-trips to `spec="codex"` with no args so an older orchestrator's pin keeps running on codex.

### Late generation state

The `late_*` keys are the late size gate's own group, and they are **additive**: an issue that never entered the gate
carries none of them and reads back as an absent generation, so no migration reaches a live issue and a handler that
reads and writes late state on every issue leaves a legacy pinned comment exactly as it found it. The keys are spelled
once, on [`orchestrator/workflow/late_split/state.py`](../../orchestrator/workflow/late_split/state.py) —
`LATE_STATE_KEYS` is the whole of what one GENERATION owns inside the pinned comment, `read_late_generation` /
`write_late_generation` are the round trip through them, and `clear_late_generation` is defined as dropping exactly
that list and nothing else. One late key deliberately sits outside it, on the
[`exemption`](../../orchestrator/workflow/late_split/exemption.py) owner beside it: `late_exempt_sha`, described
below, is written so the generation CAN be cleared and would be worthless if the clear took it. The typed record the
group round-trips through is `LateGeneration` on the `models` owner beside
it. A write with no `late_cycle_id` records only what the issue still owes — the two external ledgers, if either
holds anything — and drops the rest rather than keeping a half-record no audit line or child lineage could be
correlated to. Every field is read defensively: a hand-edited or older value that cannot be typed reads back as
absent rather than raising on a tick that has committed work to reconcile. Which reader a field goes through
is the field's own contract rather than its Python type — an identity has to be positive, a measurement non-negative,
a depth inside the lineage, a flag literally `true`, and a restart target one of the two labels a restart may apply.
The hex fields are read at their exact lengths: a frozen commit is a whole git object id (40 or 64), because nothing
here ever records an abbreviation, and a local fingerprint is a whole SHA-256 digest (64), because a truncated one is
not a hash anything could be compared against. Only a real integer counts as a number at all: a bool, a float, and
a numeric string are each a value nothing wrote. So a `late_threshold` of `-1` beside a `late_additions` of `0` does
not make an unmeasured candidate report as oversized, a `"false"` string does not arm a cancellation or a pending
restart, and prose in a `late_candidate_sha` never becomes live state — and what a read refuses, the next write drops
rather than preserving.

- **Identity.** `late_cycle_id` and `late_generation` are monotonic and never reused, so a record naming cycle 2
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
- **Held plan PR.** `late_plan_pr_number` and `late_plan_pr_body` — the pull request whose body a cycle-marked hold
  replaced, and the body it replaced, kept so the original can be restored. What restores it is the release the
  `single` reconciliation runs: the identity says which pull request this cycle marked, and the body has to BE that
  hold verbatim before it is written over, so a description a human rewrote — or edited a sentence of, leaving the
  hidden marker in place — is left as theirs. The hold text is keyed to the cycle and quotes nothing that moves
  inside one precisely so that comparison is possible: the generation counter advances on every reconciliation that
  lands, and a body keyed to it could never be reconstructed after a re-measurement.
- **External-resource ledgers.** `late_resources` holds one `{kind, target, state}` entry per obligation the remote is
  owed — kind `snapshot_ref` / `branch` / `plan_pr` / `child`, state `pending` / `retained` / `reconciled` / `failed`
  — keyed on kind and target, so a reconciliation repeated after a crash updates the entry it already wrote instead
  of appending a second one. `late_consumers` is the direct snapshot consumers, deduplicated and ordered, since the
  reclamation rule asks about each of them once, and only a positive whole number is one — `True`, `2.5`, and `"7"`
  are not issues anything can ask GitHub about, and neither the reader nor `with_consumers` will convert one into a
  consumer id. Neither ledger is ever *reduced* to what this binary understood: an entry it cannot type, or a
  consumer list it cannot read, is carried through verbatim beside the typed view and written back exactly as it
  came, and `LateGeneration.has_opaque_ledger` says so — and while it does, `with_resource` and `with_consumers`
  refuse an update to that ledger rather than returning a record the next write would silently drop back to the
  verbatim copy. "Typed" is strict there, because the alternative to
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
  rather than leaving one standing. The register is read all-or-nothing: an entry this binary did not write makes
  the whole field read back empty, because skipping one would shift every child after it onto somebody else's slice
  — and an empty answer costs a marker lookup rather than a wrong adoption. That lookup is the other half: every
  child is created carrying `<!--orchestrator-late-child:issue=…:cycle=…:generation=…:index=…-->`, so a child created
  into a crash before its number was recorded is adopted rather than opened twice. The issue is part of that identity
  because a cycle is minted per issue and repeats across them, while the lookup walks one workflow label rather than
  one parent's children — without it, two parents on their first candidate would each carry
  `cycle=1:generation=1:index=0` and one would adopt the other's child.
- **Inherited lineage.** `late_ancestry_root_issue`, `late_ancestry_depth`, `late_ancestry_parent`,
  `late_ancestry_cycle_id`, `late_ancestry_generation`, `late_ancestry_snapshot_ref`,
  `late_ancestry_snapshot_sha`, `late_ancestry_base_branch`, and `late_declared_scope` are what a child born of a
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
  anything and a commit with no ref names work nothing can fetch. `late_declared_scope` is the slice the
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
  decided, or parked it looks: reconciling it is the FIRST thing a tick does, ahead of the size gate, the plan-PR
  hold, and any spawn — and while it is set the generation counts as live for the kill switch and the hand-relabel
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
  it is. There is no clearing step to remember and no window in which a stale exemption covers a moved head. Read and
  written fail-closed like every other late field: only a whole git object id is one, a `record_exemption` handed
  anything else refuses rather than writing a value the gate would read as a bypass, and a hand-edited field reads
  back as no exemption at all.
- **Cancellation.** `late_cancelled` and `late_cancelled_at` are irreversible within a cycle: once the owner has been
  observed closed, a later tick that sees it reopened re-marks the same cancellation and keeps the first stamp. What
  observes it is the post-agent owner guard — a fresh read taken after every completed late run, before anything it
  earns happens — which writes the mark durably and emits `late_cancellation` from it, leaving what the remote is
  still owed on the two ledgers for the cleanup path to settle. A cancelled cycle is nobody's to adjudicate, relabel,
  or route, so a reopened issue starts a fresh cycle rather than resuming this one.
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
  the one before: the cycle it is, the issue and root it belongs to, and the cycle it succeeds.

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

How much of a plan PR's description may be preserved is decided by what the run still has to record beside it. The
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
produce — the preserved plan-PR body and every other stage's keys included, since a result small on its own can still be
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
retry gets there, a missing worktree is back, a run that timed out or answered unusably is about to be re-run. Those
are
retired — `awaiting_human` and `park_reason` cleared — the moment the hold reconciles, ahead of both the spawn and the
reuse of a recorded answer, because `awaiting_human` is exactly what suppresses the announcement a question verdict
earns. A stale one would silence a question durably recorded and never said out loud — whether this attempt produced it
or a crashed run recorded one whose comment never reached the issue. Five are not retired, because none of them is a
step that failed. `late_question` is the announcement itself, and the issue really is waiting on the human it names;
`late_content_drift`, `late_revision_dirty`, `late_revision_unmeasured`, and `late_revision_unanswered` are the
workflow waiting to be told what an edited scope, a worktree the developer left changed, a candidate nobody could
measure, or a developer that changed nothing and vouched for nothing now means. Retiring one of
those would drop the very state the next tick reads to tell a human's answer from the silence before it.
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

The record goes out before the effect it earns, and the owner read goes out between them. A question is written and
persisted BEFORE the comment announcing it, so a crash between them costs one repeated comment — the window every
park in this repository has — and never a second run of an agent that already answered; and the announcement itself
is made past the guard, so a question is not posted to a thread somebody closed while the agent was answering it. The
next tick reconciles the announcement from `awaiting_human`: a recorded question the issue is not yet waiting on a
human for is posted from the question the record kept, rather than re-earned.
