# Delivery stage handlers

The stages that carry an issue from pickup to a merged PR: pickup and decomposition, the family walks that hold a
parent behind its children, the dev / reviewer / docs loop, and the two labels a PR bounces through. Each section is
one handler — its trigger, the pinned state it reads, its internal flow in the order a tick runs it, and every
transition it may produce — plus the drift hook the drift-sensitive handlers share.

The two operator-applied conversation stages are in [`conversation-stages.md`](conversation-stages.md); the labels,
per-tick flow, and pinned-state keys these handlers are typed by are in
[`labels-and-state.md`](labels-and-state.md); the compact lifecycle reference is in [`lifecycle.md`](lifecycle.md).
Which module owns each handler is in
[`../architecture/workflow-modules.md`](../architecture/workflow-modules.md) and the dispatch that reaches one in
[`../architecture.md#stage-handlers`](../architecture.md#stage-handlers); what each agent role's prompt grants and
forbids is in [`../workflow.md`](../workflow.md).

## `_handle_pickup` (no label → `workflow:decomposing` or `workflow:implementing`)
- **Trigger**: open issue with no workflow label **and no pinned comment**. What this handler does is *greet* an
  issue — the "picking this up" comment, a drift baseline over a thread it assumes nobody has worked, and the
  issue's pinned comment — so an issue that already carries one has been through it, and greeting it again writes a
  second pinned comment that is invisible from the moment it is written (`read_pinned_state` answers with the first
  authenticated one it finds) while the finished workflow in the old one goes on deciding. The dispatcher therefore
  leaves an issue whose workflow label a human removed exactly where they left it, saying so once a tick; the way
  back in is applying a workflow label by hand. The one unlabeled issue with a pinned comment that *is* answered is
  the [restart](labels-and-state.md#late-generation-state), which reaches the same two labels by projecting the
  pinned comment it already has rather than through this path's greeting, fresh state, and author allowlist.
- **Input**: issue title/body/comments; `config.DECOMPOSE` (default on); `config.ALLOWED_ISSUE_AUTHORS` (default empty
  → allow all).
- **Action**: when `ALLOWED_ISSUE_AUTHORS` is set, an issue authored by anyone outside the list is silently skipped
  (log only); otherwise post a "picking this up" comment, anchor `pickup_comment_id`, snapshot `user_content_hash`
  over title + body + non-orchestrator comments, then route to `workflow:decomposing` (`DECOMPOSE=on`) or
  `workflow:implementing` (`DECOMPOSE=off`) and run that stage's handler in the same tick, so an unlabeled issue's
  first tick ends inside its second stage.

The allowlist, both routes, and the order they publish the comment, hash, label, and pinned state in all live in
`workflow/engine/pickup.py`; the same-tick handler call is a call-time import of the chosen stage's owner under
`workflow/stages/` — `decomposition/run.py` for one route, `implementing/handler.py` for the other.

## User-content drift detection

The drift-sensitive handlers — `_handle_decomposing`, `_handle_ready`, `_handle_blocked`, `_handle_umbrella`,
`_handle_implementing`, `_handle_validating`, `_handle_documenting`, `_handle_in_review`, `_handle_resolving_conflict`
— run `_detect_user_content_change` somewhere in their flow. The hash covers the issue title, body, and every
human-authored *issue-thread* comment body (PR-conversation comments are not in the hash). The hash, the seven
filters below, and the routes a detected drift is handed to all live in `workflow/engine/drift.py` (the two
bare-operator-command filters are read off the owners of those commands).

`_handle_in_review` is the exception in ordering: it runs the four-surface fresh-feedback ID scan FIRST and routes any
unread human comment past those watermarks to `workflow:fixing`, so the drift check that follows reacts only to
changes the ID scan didn't catch (title/body edits, and edits to existing issue-thread comments whose ids are already
below the watermark).

`_handle_fixing`, `_handle_question`, and `_handle_discussion` deliberately skip the drift check. `_handle_fixing`
refreshes `user_content_hash` itself once it has consumed the PR-side feedback; `_handle_question` and
`_handle_discussion` run their own conversation flows on an operator-applied label nothing routes into, so rerouting
an edited issue to `workflow:decomposing` would take it out of the conversation a human deliberately put it in.

Non-human content is filtered seven ways:

- pinned-state comments by `PINNED_STATE_MARKER`;
- orchestrator-posted comments by `_ORCH_COMMENT_MARKER` (an HTML comment embedded via `_with_orch_marker`, invisible in
  rendered Markdown, survives id-cap eviction);
- legacy orchestrator comments by id from `orchestrator_comment_ids`;
- third-party Bot/App accounts (Dependabot, Renovate, CI bots) via GitHub's `user.type == "Bot"` structural flag;
- a bare `/orchestrator continue` operator command via `_is_bare_orchestrator_continue` — it is an operator control, not
  requirements content, so it must not shift the hash and route the nudge through drift handling instead of the stage's
  intentional session-limit retry (a comment carrying the command *alongside* genuine guidance is not bare, so it still
  shifts the hash);
- a bare `/orchestrator add-agent-runs N` command via `run_grant._is_bare_command`, for the same reason and one of its
  own: the dispatcher answers that command and then hands the **same tick** to the stage below, so a hash counting it
  would meet the handler as a body edit nobody made — `validating` would resume the developer on "the human edited the
  issue" instead of running the reviewer round the issue stopped mid-way through. Filtered in **both** hashing modes,
  since the legacy algorithm the flag below reproduces predates the command entirely; guidance beside the command is
  requirements here too, so it is not bare, it shifts the hash, and the drift road carries those words to the agent;
- untrusted authors via `github.comments.is_trusted_author` when `ALLOWED_ISSUE_AUTHORS` is set (opt-in; empty
  allowlist trusts everyone), so an outsider's comment cannot shift the hash and re-trigger drift on a public repo.
  The same trust helpers filter the conversation text fed to agent prompts: `_recent_comments_text` (implement /
  review / documentation / decompose / question / drift-resume) and `_thread_text` beneath it, which the `discussion`
  stage calls directly over its own thread snapshot — with one documented retention, the orchestrator's own comments
  by recorded `orchestrator_comment_ids`, since that stage's full-context prompt rebuilds a conversation the
  orchestrator is half of (see
  [the trust boundary](../security.md#comment-trust-boundary-allowed_issue_authors)); the awaiting-human resume paths
  that quote new
  replies directly (`filter_trusted` in the implementing, validating, decomposing, documenting, resolving_conflict,
  question, and discussion resumes) plus the auto-rebase-park retry-unpark in `_sync_pr_worktree_to_base`; and the
  four-surface
  PR-feedback scans driving the `in_review` -> `workflow:fixing` route, the fixing dev-resume, and the `/orchestrator
  continue` batch replay (`filter_trusted` in `_scan_fresh_pr_feedback`, the drift-resume PR-conversation block,
  `_rescan_fixing_feedback`, and `_reconstruct_pending_fix_batch`). On every awaiting-human resume — and the
  auto-rebase retry-unpark — the filter runs on the whole `comments_after` batch up front, so it gates the non-empty
  check, the quoted follow-up, the consumed-watermark advance, and — in `workflow:validating` — the `/orchestrator
  add-review-rounds` review-cap command and the reviewer-respawn nudge; an untrusted comment resumes none of those
  sessions and does not advance the watermark (it is re-filtered on each later tick, never marked consumed). The
  `/orchestrator continue` that renews a spent spawn budget on a `retry_cap`-parked `workflow:decomposing` or
  `workflow:implementing` issue is read through the same filter (`filter_trusted` in each stage's
  `retry_cap._trusted_replies`), so what buys an agent run there is a trusted account's word and nothing else. The
  `/orchestrator add-agent-runs N` that widens a spent LIFETIME agent-run allowance is filtered in the one place it
  is read — the dispatcher's own hold, `run_grant._lifts_the_park` — for the same reason and one more: an untrusted
  request earns no receipt either, since a reply is a comment somebody else's word paid for and posting one would
  spend the watermark a trusted operator's command is read against. An
  untrusted comment therefore neither shifts the drift hash, sets a
  pending-fix bookmark, routes `in_review` to `workflow:fixing`, resumes an awaiting-human decomposer / developer /
  reviewer / question / documenting session, retries a parked auto-rebase, satisfies the `/orchestrator
  add-review-rounds` review-cap command, renews an exhausted spawn budget, buys an issue past its lifetime agent-run
  ceiling, nor reaches any agent prompt.

`_detect_user_content_change` durably persists the baseline on its FIRST encounter via `gh.write_pinned_state`, so an
early-return tick cannot silently absorb a later edit as the new baseline. It also carries a **legacy-hash
normalization** path: a baseline written by the pre-issue-#729 algorithm counted a bare `/orchestrator continue`
comment, so after deploy it would compare unequal to the new hash even with no real edit. Before reporting drift the
helper recomputes with the old algorithm (`_compute_user_content_hash(..., include_bare_continue=True)`); if that
reproduces the stored baseline the delta is purely the algorithm change, so it persists the new baseline and reports no
drift — a bare continue outstanding at deploy time cannot fire one false "issue body/content changed" route. On drift
the action depends on lifecycle position:

- **`workflow:decomposing`** — handled inline at the top of `_handle_decomposing`: drop `decomposer_session_id`, wipe
  `children` / `dep_graph` / `expected_children_count` / `umbrella`, clear park flags, post a `:pencil2: issue content
  changed` notice, then fall through in the same tick so the decomposer re-spawns against the updated body. An issue
  standing on a `retry_cap` park is held one step ahead of this (see that handler's step 1): the re-spawn it falls
  through to is exactly the spawn that park refused, so the edit waits with everything else the issue carries until
  a human continues it.
- **`workflow:ready` / `workflow:blocked` / `workflow:umbrella`** (no implementation has started) — route back to
  `workflow:decomposing` via `_route_drift_to_decomposing`: same state-wipe + notice, plus a label flip to
  `workflow:decomposing`. `decomposer_agent` is preserved across this transition so a mid-flight `DECOMPOSE_AGENT` env
  flip cannot retarget an in-flight issue. Any previously-tracked children are listed in the notice as ORPHANED — the
  orchestrator no longer tracks them, so the operator must close any that no longer apply.
- **`workflow:implementing` / `workflow:validating` / `in_review` / `workflow:resolving_conflict`** (a dev session
  exists and possibly a PR) — post a `:pencil2: issue body changed; resuming dev session` notice (on the issue for
  implementing/validating, on the PR for in_review/resolving_conflict), advance `last_action_comment_id` past every
  visible comment, resume the locked dev session with `_build_user_content_change_prompt`, and route the result
  through `_post_user_content_change_result`. On `workflow:implementing` an **unspent `retry_cap_continued`** outranks
  the recorded session and sends the edit down the no-session road instead (hash persisted, park cleared, fall through
  to the gated fresh spawn, which builds its prompt from the body the human just wrote): an issue parked on a spent
  budget sits there for as long as it takes somebody to answer, so the requirements move under it, and what the
  continuation bought is one fresh spawn — the only run the budget counts. Resumed, the agent would run on the
  human's attempt while the grant stayed on the issue, ready to buy a second run nothing charged.
- **`workflow:documenting`** — route back to `workflow:validating` (no docs spawn) — see the handler section below.

Result routing in `_post_user_content_change_result`:

- a shutdown-`interrupted` resume short-circuits before any branch below: the helper self-guards (returns `"parked"`
  without posting, parking, or pushing) and the drift callers in turn bail WITHOUT writing pinned state (in_review /
  resolving_conflict guard ahead of the helper via `_ignore_if_interrupted`), so the killed run leaves durable state
  untouched for the next process to retry;
- a clean pushed fix hands straight back to `workflow:validating` from every stage that runs the drift resume; from
  `workflow:implementing` the drift path publishes through the shared committed-work seam, so the size gate measures
  the resumed commit before `_on_commits` opens/pushes the PR;
- a no-commit reply whose clean HEAD is strictly ahead of the remote PR branch (a fix a prior parked / interrupted run
  committed but never pushed) is published through the push tail and counted as a pushed fix (`_stranded_fix_unpushed`),
  ahead of the ack check;
- a no-commit reply is otherwise treated as an ack ONLY when it carries the explicit `ACK: <reason>` marker the resume
  prompt instructs the dev to emit when existing work already satisfies the edit;
- any other no-commit response falls back to `_on_question` and parks awaiting human.

Per-stage specifics:

- For **`in_review`** drift, both the "pushed" and "ack" outcomes reset `review_round` (a drift invalidates the prior
  approval) and bounce directly back to `workflow:validating`. The drift block also captures unread PR-conversation
  comments past `pr_last_comment_id` BEFORE posting its notice so the shared id space doesn't silently swallow a PR
  comment.
- For **`workflow:resolving_conflict`** drift, ONLY the "pushed" outcome relabels back to `workflow:validating` (with
  `review_round=0`, `conflict_round` bumped). Ack and parked outcomes stay on `workflow:resolving_conflict` — the
  rebase work is still unfinished. An `interrupted` resume (shutdown sweep killed the run mid-flight) short-circuits
  BEFORE `_post_user_content_change_result` and returns WITHOUT writing pinned state, so the refreshed
  `user_content_hash` / consumed-comment changes are discarded and the next process re-detects and re-runs the drift
  resume (the caller guards via `_ignore_if_interrupted` ahead of the helper; the shared helper also self-guards on
  interrupted as a backstop, returning `"parked"`). A mid-run `paused` / `backlog` (`pause_guard=True`) short-circuits
  the same way, right after the interrupted check.
- For **`workflow:implementing`** drift, the resume runs only when `dev_session_id` is recorded. With recovered
  unpushed commits but no session the handler parks (the commits were authored against the pre-drift body). With no
  session, no recovered commits, and `awaiting_human=True`, park flags are cleared so the fresh-spawn branch fires
  this tick against the updated body.
- For **`workflow:validating`** drift, the handler defers to the awaiting-human branch when `park_reason` is
  reviewer-side (`reviewer_timeout` / `reviewer_failed`): a "retry" reply after a reviewer failure must re-spawn the
  reviewer, not the dev. The new baseline is still persisted so the next tick doesn't loop.

The hash is re-persisted on every reaction so a single edit triggers exactly one re-route, not a loop.

## `_handle_decomposing` (label `workflow:decomposing`)
- **Trigger**: each tick while the label is `workflow:decomposing`.
- **Input**: issue + comments + pinned state (`decomposer_agent` / `decomposer_session_id`, retry-budget keys,
  `children`, `dep_graph`, `expected_children_count`, `umbrella`).
- **Internal flow**: a `retry_cap` park whose sentence was never said is replayed at entry, ahead of every step
  below and of the late route among them (`_replay_owed_notice` — see
  [the retry budget](labels-and-state.md#the-retry-budget)); it says what the park is for and writes, and the tick
  carries on.
  0. **Late adjudication route.** Behind only that replay, and before every step below it, the tick asks which of the
     two questions wearing this label it is about (`_late_adjudication_owns_the_tick`). An issue whose record carries
     a live late generation is not waiting to be decomposed — its implementation is committed and was measured past
     the ceiling — so the whole tick belongs to the late coordinator (`late_coordinator.py`) and no step below runs,
     no scratch worktree is created, and the initial decomposer is never spawned. The coordinator is asked on *every*
     tick rather than only on the ones that look late, because its own first steps are the reconciliations an earlier
     tick left owed — a park notice a refused comment stranded, an owner read nobody could take — and those are owed
     by exactly the records the gates below would route past. On an issue that never entered the size gate it costs
     one pinned read that has already happened and answers immediately. What it does under that label is
     [`../workflow/roles.md`](../workflow/roles.md#what-a-late-adjudication-is-asked-and-what-it-may-answer).

     The spent spawn budget's park is asked inside that route as well as below it, because the two questions wearing
     this label reach an agent by different roads. A live generation standing on `awaiting_human` +
     `park_reason="retry_cap"` is held by `late_retry_cap._park_owns_the_tick`, behind only the reconciliations
     above and the live-generation gate: the tick ends there having proved no frozen pair, re-marked no pull
     request, read no thread as an answer to a question about the requirements, spawned nothing, and written
     nothing. What lifts it is the same trusted `/orchestrator continue`, buying the same single attempt, spent at
     the same gate in front of the adjudicator's own spawn. The refusal that TAKES that park is staged through the
     late park owner, so the generation, the frozen candidate, the hold's record of the pull request, and the
     sentence the thread is owed (`late_park_notice`, not `retry_cap_notice`) all land on one write before a word of
     it is posted — and the redelivery, the already-posted reconciliation, and the audit phases are the ones every
     other late park gets. No session is retired by the grant there: the pre-spawn record already opens a fresh
     conversation for every run that is not answering a question a human has answered.

     Which field the sentence sits on depends on which owner took the park, so the hold reads **both** before it
     reads the thread for a command. A `retry_cap` park the shared parking form took under this label — on an issue
     that had not entered the size gate, or before this owner existed — owes its sentence on `retry_cap_notice`, and
     the entry replay above is the only thing that says it. That replay stands down for exactly one reason, a thread
     it could not read, and this hold is the very next step: reading only its own field there would call the park
     explained, take a second read that may well succeed where the first failed, and buy an adjudication with words
     written before the human was ever asked.

     "Not an adjudication" is not the same answer as "never entered the gate", so one more question stands between
     the two. A record whose candidate the measurement put at or below the ceiling — a developer revision a human's
     guidance bought, re-frozen and re-measured — has had its size question **answered**: there is no verdict to
     earn and no children to create, and the initial decomposer would re-plan an implementation that is already
     written. That issue is relabelled `workflow:implementing` and falls into that handler on the same tick, exactly
     as the kill-switch route below does — so the ordinary publication reconciles the exact commit already on the
     branch. What the handback owes the pull requests first is what an accepted verdict owes them: this generation's
     "do not merge" notice comes off the held PR (a refusal parks under `decomposing` with the record untouched, so
     the retry is free), and `pr_number` is moved to the pull request the measured commit is actually on — or
     dropped where the recorded one is settled, since a merged plan PR carried into `implementing` ends the issue as
     `done` on a design the revision was never published under.

     The record itself is deliberately KEPT across the label, and retiring it is the implementing gate's own step.
     It is the only thing saying this issue's size question was asked and answered, so a tick that dropped it and
     then failed to move the label would leave a `decomposing` issue the initial decomposer could not tell from one
     that never entered the gate. Kept, the gate finds a measurement it recorded for the commit in hand and settles
     it there, retiring it (the generation dropped, its cycle kept as `late_retired_cycle_id`) durably ahead of the
     push it licenses — which is where the freeze on the base refresh and the live-cycle reading a close is answered
     against both end. A restart's fresh cycle is deliberately not this case: it carries an identity and no
     candidate at all, and it really is waiting to be decomposed.
  1. **The spent spawn budget's own park** (`retry_cap._park_owns_the_tick`), asked behind the late route — which
     holds its own copy of this park for an issue under adjudication — and ahead of every step below it. An issue
     standing on `awaiting_human` + `park_reason="retry_cap"` is stopped on its budget, and each of the three steps
     below would walk past that park for a reason of its own: the drift reset clears park flags and wipes the
     manifest, the kill switch clears the same flags and routes the issue to implementation, and the awaiting-human
     resume reads any trusted reply as the answer. None of them is the answer this park asked for, so while it stands
     the tick ends here having written nothing, spawned nothing, and said nothing — which leaves the manifest, the
     children already open on GitHub, the locked decomposer session and its `decomposer_agent` spec, `pr_number`, and
     a late generation's whole record exactly as the park found them. Each held tick emits the `standing` audit phase,
     so a park that goes on refusing is visible as that rather than as a workflow that went quiet.

     A park that still owes the thread its sentence (`retry_cap_notice`) is held before the thread is read for an
     answer at all. The entry replay above is what says that sentence, and saying it moves the response boundary
     past everything written under the old one — so while the obligation stands, a command on the thread is one
     written before the question was asked. The replay leaves it standing for exactly one reason, a thread it could
     not read, and a second read taken here is as likely to succeed as the first was to fail: read clean, it would
     buy an attempt with words nobody wrote in reply and consume the notice they were owed on the way out.

     What lifts it is a trusted `/orchestrator continue` on the thread past `last_action_comment_id`
     (`_grant_continuation` — see [the retry budget](labels-and-state.md#the-retry-budget)): the renewal the park's
     own notice asks for. The command is taken with whatever else its comment carries, since a decision that arrives
     with an explanation is still the decision and the explanation reaches the fresh decomposer through the prompt it
     is spawned on; an untrusted account's copy of it buys nothing. The grant clears the park, retires
     `decomposer_session_id` while keeping the `decomposer_agent` spec — what the attempt buys is the fresh
     conversation the budget refused, and a spawn pins an id of its own only where the backend hands one back, so an
     id left standing would be replayed by the reply to a question or a timeout that surfaced none — and is written
     down BEFORE the spawn it pays for, so a tick that dies (or a run a mid-run `paused` or a shutdown declines)
     leaves the attempt where the human put it. What it buys is one attempt, spent at the same gate in step 5 that
     refused the last one. A thread this tick could not read hands out nothing and holds the park, since a grant
     made on a read that established nothing spends an attempt no human asked for.
  2. **User-content drift check** (inline) — see drift section above.
  3. **Half-finished decomposition recovery.** If `expected_children_count` is set OR `children` is non-empty (a prior
     tick crashed mid-split), the handler cannot safely respawn the decomposer. When `expected_children_count` is set
     and `len(children) < expected_children_count`, park with `decomposition_crash`. Otherwise repair any child whose
     pinned `parent_number` was never seeded, then finalize to `workflow:umbrella` (when the flag is true) or
     `workflow:blocked`. Two owners take those markers away from this recovery: an issue already parked awaiting a
     human, and one carrying a live late generation — the split transaction writes the same two markers and resumes
     from its own durable facts, so finalizing on its behalf would hand a parent on before its snapshot, its
     supersession, or what the remote is owed had been settled. Either way the tick ends having changed nothing.
  4. **DECOMPOSE kill switch.** If `config.DECOMPOSE` is off when this handler runs, clear decomposer-side park flags,
     ratchet `last_action_comment_id` past every visible comment, flip the label to `workflow:implementing`, and fall
     into `_handle_implementing`. Step 3 runs first so orphan children are not abandoned. An issue parked on
     `retry_cap` never reaches this step at all (step 1 holds it), so the switch cannot clear a park a human was
     asked to answer — and it loses nothing by waiting, since no decomposition runs while that park stands; the tick
     after a continuation routes it here as usual. An issue carrying a live late generation — recorded, not
     cancelled, and either oversized or still owing the post-agent owner read — takes neither branch: the tick
     returns leaving it exactly where it is, because the legacy route would publish a committed candidate measured
     past the ceiling as though a `single` verdict had been recorded for it. The owed
     read is the half a size-keyed gate misses: a revision that came back UNDER the ceiling is no longer oversized,
     and nobody has established that the issue it belongs to is still open. The same issue relabelled by hand never
     reaches this handler — or any other — at all: the dispatcher puts the label back first. See
     [`../workflow/roles.md`](../workflow/roles.md#what-the-humans-can-still-change-while-a-candidate-is-frozen).
  5. **Awaiting-human resume OR fresh spawn.** Resume on a new comment; otherwise gate on the per-issue retry budget
     (shared with `implementing`; an exhausted one parks the issue durably as `retry_cap`, says so once, and is held
     by step 1 from there on — see [the retry budget](labels-and-state.md#the-retry-budget)), ensure a read-only
     worktree, resolve the spec via `_read_decomposer_session`, persist `decomposer_agent` BEFORE invoking
     `run_agent`, and spawn the decomposer. A
     mid-run `paused` / `backlog` re-check (`_paused_during_agent_run`) right after the run returns short-circuits
     both branches BEFORE the usage fold, timeout / read-only park, manifest parse, child creation, or relabel, so the
     next tick re-runs the decomposer from durable state.
  6. **Read-only check.** If the worktree now has commits or dirty files, park awaiting human and KEEP the worktree for
     operator inspection. The decomposer is read-only — without this guard, `_handle_implementing`'s recovery path
     would later push decomposer-authored work as implementation. A launch that never became a process
     (`invoked=False` — the agent-run circuit refused it) short-circuits ahead of this check and of the pause
     re-check above it: nothing in that worktree is its doing, and a `decomposer_dirty` park in its name would
     overwrite the durable `agent_run_limit` one the refusal took
     ([The agent-run circuit](labels-and-state.md#the-agent-run-circuit)).
  7. **Parse the manifest** via `_parse_manifest` (regex captures the fenced ` ```orchestrator-manifest ` block):
     - invalid manifest → park with the parse error.
     - no fenced block → treat as a question; park.
     - `decision == "single"` → post the collected-context comment (rationale plus the manifest's optional
       `affected_files` / `notes`, built by `_build_single_decision_comment`) so the implementer inherits the
       decomposer's groundwork via `_recent_comments_text`; label `workflow:ready`, stamp `decomposed_at`.
     - `decision == "split"` → for each child call `gh.create_child_issue(...)` with label `workflow:blocked` (the
       child's only birth label) and seed the child's pinned state with `parent_number`; persist `children` /
       `dep_graph` / `umbrella` on the parent; activate no-dep children by flipping `workflow:blocked` →
       `workflow:ready` (best-effort, since `_handle_blocked` / `_handle_umbrella` also treats no-dep children as
       deps-satisfied).
- **Output**: parent → `workflow:ready` / `workflow:blocked` / `workflow:umbrella` / `workflow:implementing`, OR a
  HITL park.

## `_handle_ready` (label `workflow:ready` → `workflow:implementing`)
- **Trigger**: each tick while the label is `workflow:ready`. Reached by a `single`-decision parent or a
  freshly-created child.
- **Action**: post the pickup comment if needed, bump `last_action_comment_id` to the latest visible comment id (so
  comments posted while the issue sat in `workflow:decomposing` / `workflow:blocked` are marked consumed before the
  implementer reads them at spawn), flip to `workflow:implementing`, fall through into `_handle_implementing` on the
  same tick.

## `_handle_blocked` (label `workflow:blocked`)
- **Trigger**: each tick while the label is `workflow:blocked`.
- **Input**: pinned `children` (parent only), optional `dep_graph`, `parent_number` (child only — seeded at
  child-creation time).
- **Internal flow**:
  1. No `children` and `parent_number` is set → no-op (the parent walks the dep graph).
  2. No `children` and no `parent_number` (manual relabel suspected) → park.
  3. Read each child's current label.
  4. Any child `rejected` → park parent awaiting human.
  5. Any child closed but its label is not `done` / `rejected` / `in_review` → retry `_finalize_if_pr_merged` (covers
     an externally-merged child whose own handler has not yet finalized) before falling through to the manually-closed
     park.
  6. Every child `done` → flip parent → `workflow:ready`.
  7. Walk children: any `workflow:blocked` child whose recorded dependencies are all `done` gets relabeled
     `workflow:ready`. A child with no recorded deps is also flipped (vacuous all-done over an empty list).
- **Output**: parent → `workflow:ready` (all done), OR a sibling unblocked, OR a HITL park, OR a no-op for a child
  still waiting on its dependencies.

## `_handle_umbrella` (label `workflow:umbrella`)
- **Trigger**: each tick while the label is `workflow:umbrella` (only ever a parent — set by the decomposer when the
  manifest's `umbrella` boolean is true).
- **Input**: pinned `children` and optional `dep_graph` on the parent, plus the late generation's obligation ledger
  when the umbrella was made by a late split.
- **Internal flow**: mirrors `_handle_blocked` for the rejected / manually-closed checks and dep-graph walk. The only
  difference is the all-done terminal: when every child reaches `done`, reconcile whatever the issue still owes a
  remote, and only then post a checkmark comment, stamp `umbrella_resolved_at`, set label `done`, and close the issue.
  A `children`-less umbrella is treated as corrupt state and parks.
- **A publication a split superseded is asked about again here.** An umbrella made by a split entered PAST
  publication has two things left that the closed pull request licensed: the children this walk releases, which are
  taking over the work that change carried, and the branch its terminal reclaims, which is the one that change points
  at. The transaction proved the supersession before each of its own steps and then stopped; this handler is what
  runs from then on. So the record's own publication group — which the split's retirement keeps for exactly this,
  alongside the identity, the commits, and both ledgers — is re-read **immediately in front of each of those two
  acts**: by the activation walk before *every* relabel it makes, and by the reclamation before *every* branch it
  deletes. Neither is asked here, one layer up, and that is the point — the child scan this handler takes is a
  request per child and the snapshot rule may spend a read-only probe of its own, so a reading taken before either
  is one the act behind it has already outlived. It is asked a **third and fourth** time on the terminal road — by
  the settlement the terminal waits on, before anything is said, and once more immediately in front of the
  retirement write, since the resolution comment and the latches between them are requests a reopen can land inside.
  A refusal at the second of those writes nothing, so the next tick reads the record exactly as this one found it;
  what it does cost is a sentence already sent, which is why that sentence carries a marker and is gated on the
  **thread** as well as on the stamp the retirement write puts down. That marker names the cycle and generation,
  since an operator restarting a rejected cycle keeps the thread, and it is stamped **only** on an umbrella a
  post-publication split made — nothing refuses the others past their sentence, so they keep the stamp as their
  sole gate and spend no listing. Those two are the answer no ledger
  carries: a reclamation that finished owes nothing, so a human who
  restores the branch and reopens the change afterwards finds every entry settled and `done` free to fire — over an
  open change carrying superseded work, with the retirement write behind it dropping the group that could have said
  so. Nothing is written back for it; nothing IS owed the remote, and an entry claiming otherwise would send a later
  pass to delete a branch a human put back on purpose. A pull request somebody reopened, merged, or pushed to holds
  all three:
  the children stay where they are (the walk latches, so a reopen between two relabels releases the first and not
  the second), the branch entry stays owed (nothing was attempted, so nothing is recorded `failed`), the terminal
  stays held, and the reason is logged on every tick that holds. It costs one lookup per release and one per delete;
  an umbrella the initial decomposer made, or one from a split entered before publication, reads back as no
  publication and pays nothing.
- **And the record that group sits on is not an unfinished size reading.** The same retirement that keeps the group
  drops the measurement, so what an umbrella a late split made carries on its pinned comment is a whole publication
  group, a candidate, and no count — which is also the shape of a tick that died between the freeze and the diff.
  The reconciliation the dispatcher runs
  [ahead of every handler](#the-size-gate-on-a-published-pull-request-every-push-onto-an-open-pr) asks whether the
  split has settled before it reads any of that: a `late_phase` of `splitting`, `superseding`, or `cleaning_up`, or
  a non-empty `late_split_children` register, and the tick goes to this handler. Without that question the group
  names the stage the gate was entered from while the issue wears `workflow:umbrella`, so every poll is held for a
  human as a reading read off a stage the issue has left, and the walk below never runs — the children of a split
  would be the one thing a split can leave permanently unreleased.
- **What the terminal waits on.** An umbrella made by a late split
  ([`../workflow/roles.md`](../workflow/roles.md#what-a-cleared-split-actually-does)) owes two things — the branch its
  superseded candidate was committed on, and the immutable ref that candidate was preserved under — and this is the
  last tick that could settle either: nothing revisits a closed umbrella, and no other handler reads that ledger. So
  `late_cleanup` retries every `branch` entry that is not `reconciled` — taking down the remote ref, the checkout,
  and the local ref, and settling the entry only once a read afterwards proves all three gone — and deletes each held
  `snapshot_ref` once every recorded direct consumer has **ended**, which all-children-resolved has just made true,
  proved off the child scan this handler already took rather than off requests of its own. "Ended" is the consumer's
  own issue state, not its label: reaching `done`, being `rejected`, and a human closing it all close the issue, and
  reopening preserves the label — so a child reopened while still wearing `done` is live again and keeps the ref. A
  branch target outside the orchestrator namespace or belonging to another issue is refused rather than deleted; a
  consumer that cannot be proved ended keeps the ref.
- **A park settles the same ledger, and decides no terminal.** All-children-resolved is not the only reading that
  ends every consumer: a child `rejected` and a child closed by hand both park the parent for a human, and both
  closed the child — which is the reading the rule takes. Since nothing revisits an *open* umbrella either, a park
  that returned before settling would hold a reclaimable ref and a superseded branch for as long as the human took
  to answer. So the parked path runs the same settlement from the same fresh scan that parked it, reports only what
  it actually did, and leaves the park itself untouched: still `awaiting_human`, still open, still on `umbrella`.
- **Whether the ledger names every consumer is asked first**, off the record's own phase, because the proof above is
  only as complete as the list it walks. A child is created and then recorded in two writes — it must be, since a
  child on GitHub the parent does not record is a child nothing would come back to — so while `splitting` stands the
  list may be short by one that already exists. Its length decides nothing there: a set of ended consumers says as
  little about the child it has not reached as an empty one does, and nothing on the ref is reclaimed either way.
  Either side of the loop the list is whole — before the split nothing has been created, and past it the loop ran to
  the end — which is also what lets an *empty* list settle a ref no child was ever cut from, the snapshot being
  retained ahead of the first child. A restarted cycle, or a phase this binary cannot type, proves nothing and keeps
  the ref.
- **The boundary an interrupted transaction stood at is kept, and a phase before the loop is believed only as far as
  the record bears it out.** A phase is not written only forwards: a transaction re-entered after a crash comes back
  through the whole coordinator, so the hold reconciled before anything spawns, the spawn itself, and the
  claim each completion writes would each name a boundary of their own. None of them is written over
  `snapshotting`, `splitting`, or `superseding` — the record refuses that move itself — so a re-entered split
  carries every one of those steps under the boundary it interrupted. That
  matters most in the window with *nothing* recorded, which no ledger can speak to: a child is created before the
  write that records it, so a loop that died between the two leaves an empty list beside a real issue on GitHub, and
  the phase is all that says so. Beside that, the pre-split phases (`measuring` through `snapshotting`) say "nothing
  has been cut from this ref" only on a record that shows no split ever started — a consumer or a split child on
  the ledgers, or the `expected_children_count` the transaction writes in the same durable step as `splitting`,
  ahead of its first create. That count is what upgrades a pinned comment an EARLIER binary already rewound: the
  guard stops new rewinds and nothing migrates records already in flight, so what has to answer for one of those
  is the evidence no phase write ever touched. That same count is then asked of *every* boundary, ahead of the
  phase, because a record the count proves finished is whole wherever it happens to be standing — and more than
  one boundary needs it. `splitting` is two answers rather than one: the phase goes down before the first create
  *and again beside every child recorded*, the last one included, so a crash between that final write and the
  announcement leaves a complete ledger wearing a mid-loop boundary. `snapshotting` is the same question one retry
  later: a transaction resumed after a park rewrites it over whatever boundary it had reached, so a finished split
  comes back wearing the one it started from. Reading either as mid-flight retains the ref for good and holds the
  terminal with it, since nothing revisits a cancelled owner to move the phase on — so the count is compared
  against the positional register the loop appends to, and a register that reached it is a loop that finished. A
  stale count from an ordinary decomposition of the same issue reads the same way and is meant to — being wrong in
  that direction keeps a ref and holds a terminal, where being wrong in the other deletes the only copy of a
  child's work. Past the loop no corroboration is needed: the transaction reaches `superseding` only once every
  child is created *and* recorded.
- **The delete is a small transaction.** The proof above is a reading of live issues and cannot be reproduced, so the
  entry is written `reclaiming` *before* the delete — which is what stops a tick that died between the push and the
  record of it from leaving a ref the ledger calls retained and the remote no longer has. Every recorded consumer is
  then re-read **past that write and immediately ahead of the delete**, because the scan the pass qualified the ref
  on was taken before the branch half ran and before anything was recorded, and each of those steps is a request a
  human can reopen a consumer during. A consumer that came back inside that window keeps the ref: nothing is asked of
  the remote, the entry stays `reclaiming`, and the terminal is held. What is left is the delete request itself,
  which is irreducible.
- **A recorded decision buys one thing.** A later visit acts on a `reclaiming` or `failed` entry only to **finish a
  delete the remote already took**: past the consumer proof it costs one read-only ask about the ref itself and
  qualifies only if the remote no longer has it. A ref still there is one a reopened child may still be cutting
  from, and no record of a past decision outranks the reading in front of it. A transport that raises rather than
  answering is read as the refusal it is, so no attempt is ever spent without a typed `snapshot_delete_failed`
  behind it.
- **The children are told before the entry closes, and told with a comment.** After a delete the remote accepted,
  and before the entry is written `reconciled`, every recorded consumer gets one comment saying the snapshot has been
  reclaimed and that reuse now needs an explicit new split cycle. It carries a hidden marker naming this owner, cycle,
  and generation, so a consumer already holding one of ours is not told twice. The ref is never recreated.
- **A cancelled cycle tells none of them.** The receipt is what a live split owes children it is still responsible
  for; an ending a human's close forced is responsible for none, and leaves each of them exactly as it found it —
  the entry reconciles on the delete alone. Nothing about the ref goes unsaid: the transport drops this host's
  mirror *before* the remote ref and refuses the whole reclamation if that copy cannot be proved gone, so a child
  reopened afterwards finds no mirror, asks the remote once, and is stopped and told by its own guard — which is
  where the receipt would only ever have been read anyway.
- **This owner never writes a consumer's pinned state.** That comment is written *whole* by whoever writes it, so a
  handler of the child's own that read it before this pass and wrote it after would silently undo anything recorded
  here — and a label is no proxy for "no writer": a terminal finalize sets `done` / `rejected` *before* its last
  write, and closed `workflow:ready` / `workflow:blocked` are swept by nothing, so a consumer left on one never
  becomes terminal at all. A comment is appended rather than rewritten, reaches a consumer in every state a consumer
  can be in, and cannot be lost. What acts on it is the child's own guard (below). A consumer the pass could not
  reach, or whose thread it could not read or post to, leaves the entry `reclaiming` rather than reconciling it —
  reconciling is what stops anything coming back, and for a closed owner this pass is the only thing that would. A
  refused delete tells nobody.

## The agent-run-limit hold (every dispatch, ahead of every handler)
- **Trigger**: `_route_issue_to_handler` on any OPEN issue whose pinned comment carries `awaiting_human` with
  `park_reason="agent_run_limit"` — the durable state an issue is left in once it has spent every agent run its
  lifetime ceiling (`MAX_AGENT_RUNS_PER_ISSUE`) allows. It shares the pinned read the guards beside it take, so it
  costs no extra comment walk. The park itself, the sentence it owes, and the fields behind both are in
  [`labels-and-state.md`](labels-and-state.md#pinned-state). What hands the park an exhausted reading is the tracked
  spawn boundary itself ([The agent-run circuit](labels-and-state.md#the-agent-run-circuit)), never a stage handler:
  the ledger is read where a run is about to be spent, so this hold is what a park taken there leaves behind.
- **Why here and not in a stage**: the issue this is about is one *every* handler below would touch, and each in a
  way that is right about some other park. `awaiting_human` routes `implementing` to a resume on the next trusted
  reply, the conversation stages to the answer their agent asked for, and the spent-budget holds to a command that
  buys another attempt. None of those buys back a run, and a lifetime total is spent once — no window elapses under
  this park — so it is held once, ahead of the table, rather than taught to thirteen handlers. The one command that
  answers it is asked in the same place and for the same reason: the ledger is spent by every role at every stage,
  so there is no one handler a human would say it on.
- **Where in the order**: behind the two guards that RUN rather than merely answer — a cancelled cycle's own ending
  and the restart an operator authorized — because both are endings rather than work, and a park that outranked them
  would leave each owed for as long as the issue is stopped. Ahead of everything else, including the
  live-adjudication and reuse guards.
- **What it does**: replays the sentence the park still owes (nothing below it runs to say one, so a notice a refused
  post or an unreadable thread left owed would otherwise stay owed for good), logs the hold once a tick, records a
  `standing` phase on the `agent_run_limit` event stream, and returns before the label's handler is reached. A park
  already explained says nothing more, however many ticks meet it.
- **The one thing that lifts it** is a trusted `/orchestrator add-agent-runs N` (`workflow/engine/run_grant.py`),
  read off the unread thread of an OPEN issue once the park's own sentence has been said, and nowhere else — the
  closed-issue exemption below is asked first, since what a close reaches is a terminal rather than a road that
  spends anything. A thread this tick could not read is a park held one more poll: silence buys nothing.
  Valid — an exact positive whole number no larger than `MAX_RUNS_PER_COMMAND` — it persists an allowance of exactly
  `used + N`, clears this park alone, consumes the batch it read plus the acknowledgement it posts (and nothing that
  arrived in between — the boundary is derived from ids this tick observed, never re-read off the thread), records
  `granted`, and lets the SAME tick reach the stage handler, since the run a human just paid for is the one the
  issue was stopped for. Anything else leaves both counts untouched: a malformed, zero, negative, or excessive
  request earns one marker-scoped receipt and a `refused` phase under a park that still stands, and an untrusted one
  is answered with nothing at all. The fields, the markers, and the ordering are in
  [`labels-and-state.md`](labels-and-state.md#pinned-state).
- **The one exemption is a CLOSED issue.** What a close reaches below is a terminal — the merged, rejected, and
  human-closed finalizers, and the cleanup sweep that settles a generation ledger — and each of those ENDS the issue
  rather than spending anything on it, so refusing them would leave a spent issue permanently mid-ending: a pull
  request nothing finalizes, a receipt nobody posts, a ledger no sweep settles. The poll's own closed reading counts
  beside the object's, since an issue closed when it was enumerated is one the tick was routed on the strength of.

## The reuse guard (every dispatch, ahead of every handler)
- **Trigger**: `_route_issue_to_handler` on any issue whose pinned ancestry still names a snapshot ref. It shares its
  pinned read with the live-adjudication guard beside it, so it costs no extra comment walk. Both step aside for
  `workflow:decomposing` — an issue under adjudication is working from its own candidate, not an ancestor's snapshot —
  but only once that read PROVES the adjudication is the issue's own. The label alone proves nothing: a consumer
  closed while it was being decomposed comes back with the label exactly where it was and no generation at all, and
  waving it through would spawn the decomposer against the reuse instructions in its body naming a reclaimed ref.
- **Why here and not in a stage**: the issue this is about is one no handler would touch. A consumer that ended wears
  `done` or `rejected`; reopening leaves the label exactly where it was, and both are terminal no-ops below. Asking
  before the table also means a relabel straight to another stage cannot route around it.
- **Why the child decides**: the owner that reclaimed the ref cannot make this safe from its side, for the reason
  above. Evaluated on the child's own dispatch there is nobody to race: whatever a concurrent writer did to the
  record, the child reads it again and decides again.
- **What it asks, in order**: first the **receipt** — the comment the reclamation posted on this child, marked with
  the owner, cycle, and generation its ancestry names, and authored by the orchestrator's own account. That is the
  authoritative answer, because it records what *happened* rather than what a later reading suggests: a local mirror
  nobody got round to dropping, or a ref somebody pushed again at the same commit, would both make the world look
  untouched while the guarantee the child was given — that its candidate provably came from one adjudication — is
  gone. It costs one walk of the child's own thread per tick, paid only by issues a split created. A thread that
  could not be **read** is not a thread with no receipt on it, and the two may not be collapsed: everything asked
  after this can look untouched while the answer that outranks it sits unseen, so an unreadable thread **holds** the
  dispatch there and then.
- **And when no receipt landed** (a crash, a thread it could not post to): this host's own mirror, which costs
  nothing on the wire. That shortcut is bought by the order a reclamation runs in rather than assumed of it — the
  mirror is dropped *before* the remote ref is touched at all, and a mirror that cannot be proved gone refuses the
  reclamation instead of being logged past, so a mirror still present says nothing has been reclaimed (a ref deleted
  by hand is the one exception, and a child can still read the candidate out of the copy it left). "Still present" is
  read as an identity, not an existence: the copy is a ref in the object store every agent's worktree shares, so it
  is resolved and compared against the exact commit the ancestry records. A copy standing at anything else is
  somebody's write — it says nothing about the ref on the remote and is not a candidate to work from — and goes to
  the ask like an absent one.
- **The shortcut is conditioned on the pointer, not assumed of the world.** It is taken only where the ancestry
  carries `late_ancestry_mirror_first`, the stamp a split writes onto every pointer it seeds. A pointer written
  before that ordering existed belongs to a world where the remote ref went first and the mirror came down
  best-effort afterwards — so a surviving mirror there is as likely to be the residue of a finished reclamation as
  proof one never started, and the child pays the read-only ask instead of trusting it. Nothing migrates: the stamp
  is written by the binary that would do the reclaiming, so its absence is the whole question answered.
- **What the ask decides.** A mirror that is gone (or a pointer with no stamp) is worth one read-only `ls-remote`
  for the exact ref and commit the ancestry records, and the three answers are three different verdicts. `absent` is
  the reclamation this child was not told about, and it parks. `mismatch` is the ref carrying somebody else's commit
  — not the candidate this child was promised, and not something to start work against either — so it parks too,
  under its own reason (`late_snapshot_repointed`) and its own comment; nothing here re-points or deletes that ref,
  exactly as the reclamation refuses one for a human. `unreadable` is an outage, which is evidence of nothing: the
  dispatch is **held** — no park, no comment, no write, and the same question next tick — because parking every
  late-born child through a rate-limit window would be a self-inflicted stop, while continuing would start an agent
  against a ref nobody could vouch for.
- **A child with no recorded ancestry at all** is not automatically an issue of no lineage. The split records a child
  on the parent's ledger *before* it seeds that child's ancestry — a child on GitHub the parent does not record is a
  child nothing would come back to — so a seed that failed leaves an issue whose **body** carries the split's own
  marker and whose pinned comment carries nothing, while the reclamation still counts it as a consumer and still
  leaves its receipt. The body is what decides whether to look, and it costs nothing: the dispatcher already has the
  issue, and every issue no split created stops there without a request.
- **A body marker is corroborated, never believed.** It is the one lineage claim in this workflow that comes out of a
  field the world can write, while everything it competes with is authenticated — a pinned comment only the
  orchestrator writes, a receipt checked against its author. So the **owner's own generation is read fresh** and has
  to vouch for the claim: the same `late_cycle_id` and generation counter, and this issue's number among
  `late_consumers`. A claim it does not vouch for is a claim about nothing and the guard steps aside — parking an
  issue, comment and HITL mention and all, on the strength of a sentence somebody typed into its body is the
  denial of service this check refuses, and it is also the honest answer for the *other* crash window (a child
  created before its number was recorded), since an owner may not reclaim a ref while its own ledger can be short one
  child. A record that could not be read, one whose consumer list this binary cannot type, and one naming no
  candidate are a different answer: the claim may be true and this tick cannot tell, so the dispatch is **held**.
- **What a vouched claim buys** is the whole pointer the failed seed never wrote — the ref the identity mints, and
  the commit the owner recorded preserving — so the ask is the same ask the recorded shape makes: is *this* candidate
  still obtainable. It has to be asked, because the receipt cannot cover the window it is posted after: a ref is
  deleted first, so a silent thread is what that window looks like, and so is a thread this tick could not read. The
  verdicts are the recorded shape's four, re-pointed included. The park writes back the lineage the body claims —
  never the pointer, which was assembled out of the owner's record rather than out of anything this issue holds —
  which both repairs what the failed seed owed and is what stops the question being asked again.
- **What a refusal does**: drops `late_ancestry_snapshot_ref` / `late_ancestry_snapshot_sha`, parks the issue
  (`awaiting_human`, reason `late_snapshot_reclaimed`, or `late_snapshot_repointed` where the ref survived and its
  commit did not) with a comment naming the ref and the owner, and
  returns before the label's handler is reached. Dropping the pointer is what makes the guard cost nothing on every
  tick after — and both writes are taken on the issue's own dispatch, so there is no second writer to lose them to.
- **Anything not `reconciled` holds the terminal**, ref and branch alike — a `retained` ref included. There is no
  reading under which an object still on the remote is settled, and an umbrella closed over one is an object nothing
  would ever come back for: the parent is `done` by then and no pass revisits it. Keeping the label *is* the retry,
  and the reason it is held is logged on every tick that holds, since a hold attempts nothing and so writes and emits
  nothing. An opaque *resource* ledger blocks outright, and so does any ledger entry on a record whose cycle identity
  is damaged; an umbrella with no recorded generation and no ledger owes nothing and answers without a write. An
  opaque *consumer* ledger is refused separately, because the two are preserved and written separately: it is what a
  snapshot's proof would be taken from, so the ref stays — while the superseded branch, which owes no consumer
  anything, is deleted and retried as usual.
- **Output**: terminal `done`, OR a sibling unblocked, OR a HITL park, OR a held terminal (something still owed), OR
  a no-op.
- **Two more questions ride the same read**, and both are asked FIRST. One is an owner whose cycle a close already
  ended and whose ending has not been written. Cancellation is irreversible within a cycle, so a human who reopens
  the issue does not get that cycle back, and *every* label it can be wearing — the unlabeled state included — names
  a handler that would act on the issue rather than settle it. That guard runs the cleanup below, reaches no
  handler, and writes that cycle's `rejected` ending from wherever the graph declares the edge — or, from the
  unlabeled state, only where the record shows the terminal was never applied. The other is the **restart** an
  operator authorizes by taking that `rejected` back off
  ([labels-and-state.md](labels-and-state.md#late-generation-state)), which is asked one step ahead of it: a restart
  writes its target label before it retires its own marker, so a tick that crashed in between finds a live-looking
  label over a record that still says cancelled — and the cancellation guard would answer that by handing the issue
  `rejected` again, undoing the authorization the restart is halfway through honoring.
- They come first because they are the ones that have to *run* rather than merely answer, and the two above can
  refuse indefinitely — the reuse guard *holds* a dispatch, writing nothing, for as
  long as an ancestor's ref cannot be asked about, and an owner of its own cancelled cycle nested under one would
  spend that whole outage never reconciling its own held PR, branch, or ref. Nothing is lost by the order: neither a
  cancelled cycle nor a restart mid-transaction starts any work, so neither question above is about anything either
  is going to do, and both are asked again on the tick after the ending or the fresh cycle is written.

## Closed-owner cleanup sweep (no label of its own)
- **Trigger**: an issue that is **closed** while still carrying one of the four cleanup-routed labels —
  `workflow:decomposing` or `workflow:umbrella`, where an adjudication runs, and `workflow:ready` or
  `workflow:blocked`, where an interrupted ending can be *left*. The second pair is the close/agent race made
  recoverable: a decomposition outcome writes one of them, a run spawned before its owner was observed closed lands
  after that observation, and the latch that would route the ending dies with the process — so without the query a
  restart before any cleanup pass loses the ending for good, receipt on the thread or not. Only their **closed**
  issues are asked about; an open `ready` issue is polled and dispatched exactly as ever. It
  is reached past the `backlog` / `paused` filter that parks everything else, because dropping one of these loses
  the close itself — an observed close ends the late cycle irreversibly, and this is the only pass that would ever
  record that, so an owner parked while closed would come back from a reopen and an unpause with a live generation
  and spawn against it. The control label defers what the pass would *do*, not whether the cycle ends: the sweep
  reads the same label and stops at the mark. The
  closed-issue sweep yields those four states beside its own recovery labels, on the same
  `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` cadence and through the same label cache and absent-label throttle, so it costs
  no request on a tick that sweep is skipping anyway (see
  [labels-and-state.md](labels-and-state.md#pollable-issues-and-finalization)).
- **An owner with no cycle left is asked one question before it is stepped over.** That question is the
  retirement's own correlation: a terminal that made its retirement durable and then died leaves a record naming
  which cycle it dropped, and a close observed inside that write leaves a receipt on the thread naming the same one.
  Where the two agree the cycle goes back cancelled and this pass ends it like any other, `rejected` included.
  Where they do not, what is left is an umbrella whose terminal is due and whose label never landed: that state is
  `umbrella` and closed — exactly what this sweep queries — and the record says which terminal it
  earned (`umbrella_resolved_at`), so `done` is written here. A write GitHub refuses keeps the label, which is the
  retry, so the pass after it writes what this one could not. Anything else with no cycle is left alone: every
  umbrella the initial decomposer made carries no generation and no stamp.
- **Why it is not the label's handler**: every one of the four names a stage handler that would resume the workflow
  the close ended — one spawns the decomposer, one walks the dependency graph and activates children, one hands the
  issue to a developer. The dispatcher
  therefore reads *closed* before it reads the label and routes to `late_sweep._handle_closed_owner_cleanup`
  instead, ahead of even the live-adjudication relabel guard. That classification then **binds**: the submit carries
  a `cleanup_only` route the worker cannot re-derive, so a human who reopens the issue between the poll and the
  refetch cannot turn a cap-exempt submit into an agent-spawning stage handler.
- **Reaching this route at all is what says a close was observed**, and an observed close cancels the generation
  irreversibly. So the handler's own re-read decides how far the pass goes, never whether the cycle ends: an issue
  that is open again is marked cancelled all the same and stopped there — nothing external is done to an issue
  somebody has just reopened, and no terminal is written — and the mark is what hands it to the dispatcher's own
  guard, which owns a reopened cancelled owner and settles it from the next tick.
- **A submission no pass settles is latched, not dropped.** The scheduler admits no second worker for an issue one
  is already running, and this is the only submission whose loss costs an *observation* rather than a turn: the poll
  saw the issue closed, and if a human reopens it before the next pass, no later poll sees that again. So the
  dispatcher latches the reading on `workflow/engine/observations.py` instead of discarding it, and the next tick
  reads it back and routes the issue to this sweep on the strength of it — ahead of the label, ahead of the close,
  and out of the family bucket, because the reading those come from is exactly what the reopen took away. What the
  sweep does with an owner that is open again is the bullet above: mark the cancellation and stop.
- **A pass that RETURNED is not a pass that finished the ending, and the reading is kept where nothing else would
  come back.** A cleanup can run every step and leave the ending owed: a consumer that is live again keeps the ref,
  a remote that refuses a delete keeps the branch, and the `rejected` terminal is one more request GitHub can
  decline. What that decides is only whether this reading is the *last* route. An owner still wearing any of the
  four swept labels is one a later tick reaches on the `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` cadence an operator set — the
  label staying put *is* the retry — so the latch is handed back there rather than costing a cleanup pass per tick
  for as long as the ending is owed, which for a live consumer can be indefinitely. An owner that is *open* again
  hands it back too, because the sweep may not advance a reopened issue and the dispatcher's own guard owns it. What
  is kept is an owner that is closed, still owes something, and wears a label no query asks for; a read that could
  not answer is kept for the same reason, since it established nothing.
- **What the ending owes outlives the process holding that reading, so the label is repaired too.** The sweep
  queries four labels, and an owner can be moved outside all of them — by a hand relabel, or by an operator putting
  a closed owner onto a terminal over a cycle that still owes something. Inside this process the held reading is
  what brings a tick back to such an owner; after a restart, nothing would. So a sweep that leaves an obligation
  owed puts the owner back under `decomposing` or `umbrella` (whichever the `umbrella` flag says the record
  reached), unguarded, as the repair of a move this workflow never made — and the reading is handed back once that
  repair landed, because the label is now the durable route. A repair GitHub refuses leaves the reading as the only one
  there is, and it is held and said out loud. An ending that finished is left exactly where it is: it wrote
  `rejected`, which is what takes an owner out of the sweep for good.
- **The latch is a barrier the run in flight is held to, not just a note for the next tick.** The worker that owns
  the issue asks it before every step the remote keeps, through the same owner read those barriers already take
  (`late_owner._read_owner` consults the latch before it asks GitHub). That is the reading GitHub cannot give back: a
  close and a reopen that both happened inside one of the run's own steps leaves the issue reporting `open`, and only
  the poll ever saw otherwise. A latched close therefore ends the cycle where the run stands — the cancellation
  persisted by the worker that owns the pinned comment, and nothing further spawned, created, or activated.
- **Where those barriers are, and why each one is there.** Every one of them sits immediately before a step nothing
  takes back, and each covers a window of *remote work* the poll runs beside:
  - **the child loop**, before every child including the first — the write that forces the parent to be an umbrella
    stands ahead of the first create — once more inside the create itself, since the orphan lookup that precedes it
    walks the whole repository on a resumed pass, once *behind* it, and once more between the read of the child's
    own pinned comment and the write that adds to it: the create is a request too, so is that read, and what a close
    inside either leaves is a real GitHub issue. That one is recorded either way (a child nothing names is the one
    state no pass can clean up) and written to never (a cancelled cycle owes its children nothing);
  - **each publication step** — the announcement, the supersession, and the retirement that hands the parent to
    `workflow:umbrella`;
  - **the activation walk** (`activation.py`), before *every* relabel rather than once for the walk: a relabel is a
    request, and a close latched after the first child was released must not release the second. Asked on **both**
    sides of the publication licence a late split's children carry, since that licence is a lookup of its own and a
    close observed inside it would reach nothing else before the relabel landed;
  - **the spawn** (`late_coordinator`), asked twice and the second time right against it — a worktree probe, a
    retry-budget write, a hold reconcile, and the write that records what this attempt IS all stand between a tick's
    own gates and the one step that puts an agent on somebody's repository. What the record then claims is an
    attempt nobody made, which the next tick reconciles for free; an agent that ran is what nothing takes back. Both
    are asked with the retry accounting handed back, since the cancellation a latch takes is itself a write and a
    run nobody started may not cost the issue a counted attempt — or the one a continuation bought;
  - **the developer revision** (`late_revision`), three times: as the tick is entered, again right against the
    resume — the revising notice it posts in between is a request the poll runs beside — and once more when the run
    comes back, which stops the remeasure that would write a fresh candidate over a cycle a close already ended.
    The poisoned-session retry inside the shared resume is guarded with them, since that is a *second* agent and an
    issue somebody closed is owed neither;
  - **the `single` publication** (`late_settlement`, and `late_handback` behind it), asked between *each* of its own
    steps — the reconciliations, the exemption write (which carries the identity of the accepted contribution beside
    it, since the retirement below takes the frozen pair it was read over off the record), the handoff label, and the
    accepted notice — because these are
    the barriers protecting the *record* rather than an effect: the last write drops the generation entirely, and
    both the sweep and a receipt adopted from the thread read that generation to decide there is anything to end.
    Past that write a refusal is too late, so the answer there is a **reinstatement**: the generation is still in
    the call's own memory, and it is written back and cancelled from there. What was published stays published —
    the exemption, the notice, and the handoff label are none of them this owner's to take back;
  - **the reclamation itself** (`late_cleanup.py`), between every obligation it settles, between every two of the
    receipts a reclaimed ref owes its children — each is a comment on somebody *else's* issue, so a close observed
    after the first is one the second may not be written over — between the fresh consumer
    proof and the ref delete it authorizes — a ref that is gone while the record still reads live is a reclamation
    nothing afterwards can attribute to the cancellation that earned it — and again between that delete and the
    receipts behind it — and once more inside each of THOSE, since proving a child untold is a thread walk of its own
    and the comment it authorizes stands behind it — each is a request, and the receipts are the one cleanup effect
    that writes to somebody *else's* issue. The mark does not buy a shortcut through the reclamation rules; what it
    changes is what the settling owes anybody, since a cancelled cycle tells its consumers nothing;
  - **the umbrella walk** (`umbrella.py`), past the child scan, behind the settlement its terminal waits on, and
    once more immediately before the write that records the resolution — the scan is a request per child, and so is
    the settlement. `done` is the write that cannot be recovered from, because it takes the issue off every label
    the closed-owner sweep queries, so what makes it safe is the write *ahead* of it: one pinned write that stamps
    the resolution and **retires the cycle** together, carrying the two ledgers across the way the `single`
    publication's own retirement does. A close observed before that write stops the terminal outright and leaves the
    owner on `umbrella` with the mark down, where the ending retires it to `rejected` from a label the sweep still
    queries. One arriving after it is a human closing an issue this orchestrator had already finished — every child
    resolved, every obligation reclaimed, the cycle over — which is not a cancellation, and leaves no live cycle
    under the terminal for anything to have to find. That write is itself a request, so the latch is asked once
    more *behind* it, off the same `observations.retiring` window the `single` retirement holds: there the
    answer is a **reinstatement** rather than a refusal — the generation is still in the call's own memory, so it
    goes back cancelled, no terminal is written, and the owner keeps `umbrella` where the ending reaches it. That
    barrier is this process's, so the write records `late_retired_cycle_id` exactly as the `single` retirement does:
    a process that dies before reaching it leaves a record naming the cycle it dropped and a receipt on the thread
    naming the same one, and the sweep adopts the two together rather than finishing a terminal over a close.
    Every window a crash can land in is one the next pass repairs:
    before that write the owner is on `umbrella` with a live cycle, which the sweep and the umbrella poll both
    already own, and after it the owner is on `umbrella` with the resolution recorded and no cycle at all — which
    the sweep finishes by writing the label the record already earned, retrying for as long as GitHub refuses it,
    because the owner keeps the label the sweep asks for until one lands. The closing notice is gated on the same
    stamp, so a terminal resumed after a crash says it once;
  - **the activation's own answer, carried out**. The walk holds the children it has not reached, which is the whole
    of what a shared dep-graph walk may decide; the split transaction asks again behind it and ends the cycle on the
    answer, because reporting settled would send it on to reclaim the superseded branch with no mark saying why.

  The barriers past a claim-bearing read take the latch alone: a claim names `owner_check`, and writing it over
  whatever boundary the tick actually reached is the rewind the record refuses.
- **It is latched where the close is READ**, which is the enumeration that classified the issue — not where the
  reading is later carried. Between the two stands the rest of that enumeration (a label read per issue in the
  repository) and the submit decision itself, and a worker already holding the issue asks the latch before every
  irreversible step it takes for the whole of that window: a reading installed only once the scheduler had refused
  would leave that worker free to spawn, create a child, or activate one against an issue the poll had already seen
  ended. It is taken for every closed issue the fan-out set records, which is exactly the set whose route carries a
  closed reading; a closed issue drained in the family bucket is a hard human stop with nothing to finalize.
- **A close the enumeration never saw is taken at the REFETCH.** An issue open when it was listed carries no
  reading at all — nothing was latched, because there was nothing to latch — and the refetch every route takes on
  its way to a handler can be where that stops being true. From there the reading exists in one place only, and
  everything behind it can fail: the pinned read the guard is built on answers a refusal of its own, and the write
  that marks the cancellation is a request like any other. So both refetching paths take the observation against the
  object they just got back — the sequential loop, which has no hand-off to hold one for it, and the worker, whose
  hand-off carries the poll's *older* reading instead — and hold it across the pass, so a pass that could not mark
  anything leaves the reading for the next tick rather than losing it to a reopen.
- **The durable half is written there too.** A latch is memory, so an accepted submit whose task never starts — a
  scheduler shutdown, a process that dies before the worker takes it — would otherwise leave the observation with
  nothing on the remote saying it happened, and a human who reopens the issue before the next process polls it takes
  the reading away for good. So the receipt goes on the thread while the record can still name the cycle it belongs
  to, from the object the enumeration already listed: one pinned read per closed fan-out issue, and the same read
  answers whether the reading is owed at all — an issue whose record says there is nothing to end has its latch
  dropped again right there, so the machinery is carried only by the owners that need it (and the admitted pass
  skips its own end-of-pass probe, since the poll already asked that record).
- **It is process-wide rather than per-scheduler** because the readers are stage handlers deep inside a worker, and
  the alternative is threading a scheduler through thirteen handler signatures that have nothing to do with it. It
  is dropped by the pass that RAN (`settle_close` from the worker, once its pass returned), never by the submit that
  was accepted: an accepted submit is not a cancellation persisted — the worker refetches the issue first, and that
  read can be the thing that fails — so a pass that raises anywhere latches the reading again. A task that never
  runs at all — a scheduler shutdown, or a process that dies between the submit and the worker taking it — leaves the
  latch standing, which is the point: the next tick routes the issue to the sweep on the strength of it.
- **The cycle a retirement drops is recorded outside the group that write clears.** The window above is memory and
  the barrier behind the write is this process's, so a process that dies between them leaves a receipt naming a
  cycle and a record that no longer names one — and the guard below returns on a record with no cycle, so nothing
  would ever look at that receipt. `late_retired_cycle_id` is the one fact about the dropped generation that
  outlives the drop (like `late_exempt_sha`, deliberately outside `LATE_STATE_KEYS`): a record carrying it is asked
  once per owner per process whether the thread has that cycle's close receipt, and one that does gets the cycle put
  back — cancelled, with the ledgers the retirement carried across — so the ending has something to run from. The
  correlation ends where its window does, and only there: any generation written with an identity supersedes it (the
  adoption's own mark included, which is what consumes it, and an operator's authorized restart with it). Both
  retirements that drop a cycle record one — the `single` publication's and the umbrella terminal's — because what
  the correlation is for is the process that dies before its own barrier, and that barrier belongs to whichever
  process made the write. A terminal retiring cycle N names N and nothing else, so a receipt for any earlier cycle
  on the same thread matches nothing an adoption would read.
- **A retirement in flight is a record that answers for a cycle it no longer names.** A published `single` drops its
  generation and then asks the latch, and between those two the record carries no cycle identity at all — which is
  the one thing every reader of a close consults. A poll reading it there would answer "nothing to end", drop the
  observation, and leave the barrier behind the write asking a latch nobody is holding any more. So the worker holds
  `observations.retiring_cycle` across its own write and that barrier: inside the window the record's silence proves
  nothing, the reading is kept, and the receipt the poll leaves on the thread is scoped to the cycle the window names
  — which is the only place that cycle can still be read, and what makes the durable half survive the retirement at
  all. Outside the window the same reading IS dropped, and correctly: the publication completed, and the ordinary
  terminal arc the issue's label names owns the closed issue from there.
- **The probe and the receipt are one read.** Whether the reading is still owed and what the receipt should say are
  the same question about the same record, and two reads of a record a worker is writing can disagree — one seeing a
  cycle and keeping the observation while the other sees the retirement behind it and leaves the thread saying
  nothing, which leaves the reading in memory alone for a restart to take. So the owner writes the receipt from the
  read that decides it and answers the dispatcher with what that read established; a read that failed keeps the
  reading, which is the only answer a request that established nothing is entitled to.
- **The durable half is a marked comment on the issue thread.** A latch dies with the process holding it, so the
  first pass to latch a close also posts one cycle-scoped receipt
  (`<!--orchestrator-late-close-observed:issue=N:cycle=C-->`). A *comment* rather than a pinned write for the reason
  the latch exists at all: the pinned comment is written whole, so a second writer racing the worker that owns the
  issue would drop whatever that worker recorded in between, while a comment is added and races nothing. Posting is
  best effort — a receipt GitHub refuses costs durability, not the reading, which is still latched and still ends
  the cycle on the next barrier the run reaches.
- **A refused receipt is retried, not lost.** The post is attempted by every pass that latches a close and settled
  by the first that lands one: the memo suppressing further attempts (`observations.receipt_written`) is written by
  the attempt that succeeded, so a comment GitHub declines is tried again on the next poll. Without that, an
  observation with no durable half would be one a restart takes away entirely — the latch alone does not survive the
  process.
- **The attempt is claimed, and the memo is counted against the reading it was claimed for.** Asking whether the
  thread already carries a receipt and getting one onto it are two operations, and the other two parties are inside
  that gap: a second poll owing the same observation (a worker's failed pass and the following tick's enumeration
  meet there), and the worker running the pass that settles the reading. So `observations.claim_receipt_post` hands
  out the sole right to attempt the post — one poll walks the receipt-less thread, not two — and it carries the
  per-owner **generation** that reading was taken at. Every `settle_close` moves that generation, so a receipt
  landing either side of a settlement records no memo at all: without it the memo would stand for a reading nobody
  holds, and the *next* close — a fresh cycle an operator authorized by removing `rejected` — would be suppressed
  into having no durable half, which a restart before its worker reaches a barrier takes away entirely. The claim is
  handed back either way, by the write that recorded the memo or by the failure that recorded nothing; a claim left
  standing would suppress every later poll's receipt for good.
- **The receipt is read back once per owner per process.** After a restart the fresh process finds an issue a human
  reopened, a record still saying the cycle is live, and nothing in memory; the dispatcher's own cancelled-cycle
  guard therefore scans the thread for a receipt scoped to the cycle the record names, adopts it, marks the
  cancellation, and runs the ending from the mark. The scan is claimed through `observations.claim_receipt_scan`, so
  a thread carrying no receipt is walked on the first tick that sees the owner and never again — what it recovers is
  an observation a *dead* process was holding, and every observation this one makes is in the latch, which costs no
  request. The claim is held for the length of the walk and handed back where the walk established nothing — a
  listing that raises leaves `observations.scanning_receipt` by exception and the claim goes with it — because a
  claim standing over a read that established nothing would send every later tick straight past the receipt and on
  to the live stage handler. It is handed back again whenever a receipt actually LANDS: a claim taken when the thread
  carried nothing proved nothing about one posted since, and every later pass would read straight past it. Cycle
  scoping is what keeps an old close from ending the fresh cycle an operator authorized by removing `rejected`.
- **Every path that runs a cleanup holds its observation the same way.** The scheduler's fan-out submit, the in-tick
  parallel one, and the sequential stream all wrap the pass in `dispatch._cleanup_observation`, with the refetch
  *inside* the wrapper — that read is the first thing a cleanup spends and the likeliest to fail, and a pass that
  raised marked nothing. Without the wrapper the exception is merely logged and a reopen before the next tick resumes
  the uncancelled cycle.
- **A closed owner whose label names an ordinary terminal is still cancelled.** The cleanup route takes a closed
  owner on either label an adjudication runs under; what reaches the dispatcher's own guard closed is the one window
  no label covers — a `single` verdict hands its issue to `workflow:implementing` a moment before it retires the
  cycle. Nothing else would end that cycle: the terminal arc that label names drains a merged pull request or a
  human close and writes the late record off nowhere, and the relabel guard beside it merely puts `decomposing`
  back, which a reopen before the next tick takes away again. So the guard marks it from the reading it already has
  — the closed issue it was handed and the record it already read — and the ending runs from the mark.
  The dispatcher covers the same window on both sides of the submit. An **admitted** task carries the poll's closed
  reading with it (`_PollReading`) and applies it on the worker thread before the guard reads the refetched object —
  a human who reopens between the poll and the refetch would otherwise leave the fresh reading saying open with a
  live cycle under it — and it holds that reading across the pass, latching it again on the way out unless the pass
  actually spent it. Spending it is not the same as finishing: the pinned read the guard is built on answers a
  refusal of its own, so a tick that could not read the record refuses the issue and marks nothing. A **refused**
  submit latches the reading **first**, then drops it again only where the record positively says there is nothing
  to end. All three tick paths do this. The order is
  the whole of it — the probe is a request, and a request can fail or can land after the very retirement it was
  asking about, so a reading conditioned on it would be lost to either. A latch held over an issue with no cycle
  costs the next tick one cleanup pass that settles it; a reading dropped costs the close itself. It is taken on the
  refusal rather than ahead of admission, because an admitted submit runs the label's own handler and settles
  nothing.
- **A cancelled cycle is refused under every label, and the terminal lands where the graph allows.** Every workflow
  label names a handler that ACTS on the issue rather than settling it, so a cancelled cycle wearing any of them is
  refused whatever it says — a human who relabels such an owner is asking for work on a cycle a close already ended.
  Where `rejected` is *written* is the transition graph's answer for every label a workflow wrote: each state a late
  cycle can be interrupted on declares that edge, and `question` — applied by an operator who wants the issue discussed
  rather than ended — does not, so an owner there is refused and said out loud rather than relabelled out from under
  whoever put it there. `ready` and `blocked` are the exception, and they are not a human's placement: a decomposer
  spawned before the close writes one of them as its ordinary outcome and lands *after* the close, so an ending refused
  there is one refused on every visit the sweep makes, forever — neither label declares the edge, and the sweep is what
  brings a tick back. The terminal is therefore written from both, unguarded, as the repair of a move this workflow
  never made. The **unlabeled** state is refused with every other one, and the RECORD rather than the label decides
  whether the terminal may be written from it: an operator who removed `rejected` to authorize a restart leaves the
  issue wearing nothing, and so does a human who stripped a workflow label mid-cleanup and an ending whose terminal
  write GitHub refused. Where this cycle's terminal is proved applied, re-applying it would undo the one
  authorization a restart has, so it is not written; where the proof is missing, the terminal is still owed and is
  written once the obligations settle. Falling through is what does not happen either way — the pickup path behind
  the guard would greet a cancelled cycle as new.
- **A control label defers everything past the mark, and nothing before it.** `backlog` / `paused` park an issue
  outside the state machine, and the ending is external work — a held pull request closed, a branch deleted, a ref
  reclaimed — so none of it runs while the label is on. The cancellation itself is still persisted, because the pass
  the park would drop is the only one that would ever record the close: an owner parked while closed would otherwise
  come back from a reopen and an unpause with a live generation. That reading also survives the partition filter, so
  a parked *closed* issue is bucketed rather than discarded.

  The waiver is exactly that wide, and it is re-applied behind the mark. A record with no late cycle marks nothing
  at all, and the cancelled-cycle guard answers "not mine" for one — so without a second ask a parked issue would
  reach the stage handler its label names, which is the one reaction an operator applied `paused` to prevent. The
  same is true after a reopen between the poll and the worker: the reading is still the poll's, the record still
  owes nothing, and the park is still the answer.
- **A held observation outranks every filter above the partition.** It is not a reading of the current tick's, so
  what that tick can see about the issue has already been overtaken. A `backlog` / `paused` park no longer drops it
  — the sweep it routes to defers every external step anyway, which is exactly what the park asks for, and never the
  mark. And an issue the enumeration does not yield at all, because a human moved its label off the four the closed
  sweep queries, is added by NUMBER on the strength of the observation alone; the worker's own refetch decides the
  rest. All three tick paths do this: the partition for the scheduler and parallel modes, and the sequential stream
  sweeps whatever its enumeration never reached.
- **Why it fans out rather than joining the family bucket**: that bucket's cap exemption is all-or-nothing, so one
  open `workflow:decomposing` issue sharing the tick would make a closed owner cap-counted — and under a saturated
  cap the whole bucket is skipped, which stops the repository reclaiming refs for as long as its decomposer is busy.
  Partitioned as fan-out, the owner carries its own `cap_exempt=True` submit, for the same reason every other closed
  issue does: nothing on this path spawns an agent or touches a worktree it did not already own.
- **What it does**: it ends the cycle, and nothing about the workflow the close ended. It never spawns, never
  adjudicates, never creates or activates a child, and never touches one that already exists. The ending is
  [`late_cancellation.py`](../../orchestrator/workflow/stages/decomposition/late_cancellation.py), and it runs in
  a fixed order.
  1. **The cancellation is persisted first**, ahead of every external call, and the `late_cancellation` record
     rides that write — so there is one per cycle rather than one per cadence, and every gate below reads a record
     that already says the cycle is over. It carries the moment the obligation was taken on and the boundary it
     interrupted (`late_cancelled_phase`), because `late_phase` is about to name the cancellation itself and the
     boundary is what the whole-ledger rule reads. Both are kept from the *first* observation: a reopen and a
     second close re-mark the same cancellation and move neither.
  2. **A held pull request is released, told once, and closed.** This is the one obligation a cancellation
     owns that no other pass ever sees — every path that reaches an umbrella superseded the pull request its work
     was on along the way, so a cancelled cycle is the only shape where one is still open under a "do not merge"
     notice. The hold comes off
     first, so a pull request that ends up closed is not also left wearing one forever; a release that failed on a
     still-open pull request stops the close, since the preserved description is the only copy of what the hold
     replaced. The notice carries a cycle-scoped marker and is proved from the pull request's own thread, and the
     entry is recorded either way — `reconciled`, or `failed` with `pr_reconcile_failed` behind it. It is re-asked
     on **every** visit, including one whose entry already reads `reconciled`, for the reason the ordinary
     supersession is: that entry records what an earlier visit did, and a human can reopen the pull request behind
     it — an owner the sweep is still visiting for a branch it cannot delete would otherwise reach `rejected`, and
     leave the sweep for good, beside a change that is open again under a cancelled cycle. Re-asking costs one
     fetch and one comment listing and repeats nothing; the write and both sinks stay behind a state that actually
     moved, so a settled pull request adds no record per cadence.
  3. **The branch and the ref are `late_cleanup`'s, unchanged** — the same rules, the same `reclaiming` / release /
     `reconciled` order, the same records, and the same bound on them: what reaches the sinks and the pinned
     comment is a state that *moved*, so a remote that goes on refusing one delete costs a request per visit
     rather than a record and a write per visit, while the log goes on naming what is held. A cancellation buys no
     shortcut through any of it: a consumer that is live again keeps the ref whether or not its owner is closed.
     What it does change is which ledger the rule reads. The count written before the first create can only be
     reached by a loop that ran to the end of its manifest, and a cancelled one never will — so the loop that stops
     writes down that its register is **final**, which it may because every barrier that ends it is asked after the
     write recording the child in hand. The ref then goes once every child the split actually cut has ended. A
     **resumed** walk stopped before it reached the first unrecorded index seals nothing: a create is a request and
     the write recording it is another, so a child an earlier attempt made and never recorded would not be on the
     register, and there the ref stays held on the count.
  3b. **A branch a supersession left unrecorded is taken on here.** The transaction settles the held pull request
     and records the branch that PR carried in two writes — the second is the retirement, and retiring ahead of a
     supersession that might not land would let the children loose beside a change still carrying their work. A
     close landing in that window leaves a cycle whose candidate is preserved on the ref, whose held PR is closed,
     and whose branch nothing on the record names; settling around it would retire the owner over a branch the
     remote keeps for good. So a cancellation whose kept boundary is `superseding` resolves that branch and records
     it as owed — but off the **announcement's own receipt**, not off the phase. A park at the supersession is
     resumed from the top of the transaction, which rewrites `snapshotting` and `splitting` over the boundary while
     stepping over the announcement it already made, so a second failed attempt stands at `splitting` with the
     receipt still set and the phase no longer says what was reached. Not before that receipt, since the snapshot
     is created *and proved* ahead of the first child and the branch stops being the only copy there. Only where
     the record names no branch already, in any state. And only once the held PR of step 2 is actually
     **settled** — the boundary is written before the supersession is attempted, so it says the attempt was reached
     and nothing about whether it landed, and inferring the branch while that pull request is still open would
     delete, out from under a change a human can still see, the branch that change is built on. Nothing is lost by
     waiting: the pull request is re-asked on every visit, and the visit that closes it takes the branch on.
  3c. **The held pull request is asked once more, immediately before the terminal.** Step 2 settled it at the
     top of the pass; what stands between that ask and the write below is a branch delete, a ref delete, and a
     fresh read of every recorded consumer — long enough for a human to reopen the change inside them, which leaves
     the record saying `reconciled` and the remote saying open. `rejected` takes the owner off both swept labels,
     so a terminal taken on the record would leave that pull request standing under a cancelled cycle with nothing
     coming back for it. The re-ask is the same idempotent one: a pull request still where the earlier ask left it
     costs a fetch and a comment listing and moves nothing, while one that is open again is closed again and one
     that will not close goes back to `failed` and holds the terminal for the next visit. It is taken only where
     nothing else is owed, since that is the only visit whose terminal is actually due — an owner still holding a
     branch the remote refuses is one the sweep is bringing back anyway, and the ask at the top of that pass is the
     same ask.
  4. **`rejected` last, and only once nothing is owed** — branch, ref, and *every* unreconciled `plan_pr` entry on
     the ledger, which is a wider reading than what the pass acts on: acting takes the hold's own record, since
     releasing one means knowing which pull request this cycle marked, while being owed takes the ledger, because
     an entry left under a number a later write cleared is still an obligation and a `rejected` owner is one
     nothing revisits. It is what a restart counts too, so retiring over one would refuse the fresh cycle that
     terminal is meant to authorize. A recorded `late_plan_pr_number` with no preserved description beside it is
     owed as
     well, and is the one entry no pass can settle: the description that hold displaced is the only copy there
     was, so nothing may put it back or close over it, and the terminal is held until a human repairs the record.
     An opaque resource ledger blocks outright beside all of them.
  4b. **The child receipts are discharged in the same breath.** Each child is recorded `pending` when it is
     created, and nothing has ever moved one: the reclamation does not look at child entries, rightly, because a
     child is a live issue rather than an object to reclaim. But `rejected` authorizes a restart, and a restart
     projects its fresh cycle only over a ledger with nothing unreconciled on it — child entries included,
     correctly, since the projection drops the ledger and may not discharge an obligation by forgetting it. So the
     ending records what is already true: the children exist, this cycle is over, nothing further about them is
     owed. Not one of them is touched on GitHub.

     That label is the one write this path ever makes, and it is what takes the
     issue out of the sweep for good: every label the sweep queries is one it keeps until this write lands, so a
     terminal taken over an unreclaimed remote would leave that object with nothing coming back for it. A refusal
     keeps the label, keeps the issue swept,
     and says on every visit what is still holding it.
- **Consumer state is re-read, never latched**: this pass fetches every recorded consumer fresh, and a consumer
  reopened before the delete lands has a live claim again, so the ref stays. A consumer whose read *fails* also
  keeps its ref, while the branch half — which owes no consumer anything — is still settled on that same visit.
  The scan is taken only where a ref is actually held, so an owner with nothing but a branch left costs no
  per-consumer request.
- **An issue with no recorded generation is left entirely alone**, which is every umbrella the initial decomposer
  ever made: they wear one of the same two swept labels and own no cycle, so there is nothing to cancel and no
  terminal to rewrite.
- **A reopen does not resume the cycle, and does not skip its ending either.** Cancellation is irreversible within
  its cycle, so a human who reopens the issue does not get that cycle back — and every label the issue could be
  wearing names a handler that would act on it rather than settle it. The reopened owner is caught by the
  dispatcher's own pinned-state guard ([above](#the-reuse-guard-every-dispatch-ahead-of-every-handler) shares that
  read), which runs exactly the reconciliation above, reaches no handler, and writes the same `rejected` a closed
  owner earns. It runs the cleanup rather than merely refusing because this sweep visits *closed* issues only: a
  refusal with nothing behind it would freeze the issue until somebody closed it again. `rejected` is what the
  **cycle** earns rather than what a closed issue earns, and it is what an operator removes to authorize a restart,
  so reaching it is the only way back into ordinary work that does not silently resume a cycle a close already
  ended. The issue is left open; closing one a human just reopened is not this pass's to do.
- **The label decides where the ending is written, not whether it is refused.** A cancelled cycle is refused under
  every label, and the terminal is written from the ones the transition graph declares the edge from, plus `ready`
  and `blocked` — the two the cycle's own decomposer writes as its ordinary outcome, which no query would ever come
  back to. Under a label that is neither (`question`), the refusal stands on its own and the cycle stays cancelled
  where it is. Unlabeled is refused too, and there the record decides what the label cannot: an issue an operator has
  taken `rejected` off, one whose workflow label a human stripped mid-cleanup, and one whose terminal write GitHub
  refused all wear the same nothing. A cycle whose terminal is proved applied is not handed it back — that would undo
  the one authorization a restart has — and one carrying no such proof is owed the write. The restart itself is
  answered one guard earlier ([labels-and-state.md](labels-and-state.md#late-generation-state)); what never happens
  is stepping aside, since the pickup path below would greet a cancelled cycle as new.
- **Output**: the cycle cancelled once, obligations settled or retried (with the same `late_cleanup` /
  `late_failure` records the terminal emits, bounded the same way), no consumer written to or commented on, the
  owner moved to `rejected` once nothing is owed, OR a no-op.

## `_handle_implementing` (label `workflow:implementing`)
- **Trigger**: each tick while the label is `workflow:implementing`.
- **Input**: issue + comments + pinned state.
- **Internal flow**: a `retry_cap` park whose sentence was never said is replayed at entry, ahead of every step below
  (`_replay_owed_notice` — see [the retry budget](labels-and-state.md#the-retry-budget)); it says what the park is
  for and writes, and the tick carries on.
  0. **External-merge / closed-issue short-circuit.** `_finalize_if_pr_merged` flips a merged PR to `done`
     (`merge_method="external"`); `_finalize_if_issue_closed` flips a closed issue to `rejected` and emits
     `pr_closed_without_merge` + cleans up the branch only when the linked PR is also closed (an open PR with a
     manually-closed issue is left alone for operator salvage). Both helpers defer without writing state when the PR
     fetch fails so a transient failure cannot mis-label a merged-PR issue. The merge terminal is reached only past
     the plan question, which two records answer. A live `discussion_plan_path` says the recorded PR is the
     `discussion` stage's plan whatever its head is now — the handoff below retires that record durably before anything
     spawns, so nothing here has pushed yet and a head that moved is the humans editing the design they are agreeing to
     (a corrected plan, a base merged into the branch), not work having landed. Past the handoff `discussion_plan_sha`
     answers, and it is the head that PR was on when the handoff took it — snapshotted there in the path record's
     place, so an amendment the humans made is not read as an implementation by the tick after. A recorded PR still on
     that commit is the plan, and one whose head has moved is this stage's
     own push. Neither may finalize as work having landed while it is still the plan. That read has three
     answers, not two — a PR that could not be fetched ends the tick where it happened, unfinalized and unspawned,
     because falling through would ask GitHub the same question a second time and a request that failed once and
     succeeded next would finalize the plan the first answer existed to protect.
  1. Awaiting-human resume: on a new human comment past `last_action_comment_id`, resume the dev session via
     `run_agent(dev_agent, ...)`. A `retry_cap` park is the one awaiting-human state this road never sees: the
     spent-budget bullet below owns the tick before it, because a resume is not a fresh spawn and the daily spawn
     budget does not charge it — the lifetime ledger does, like every other process start — so any reply at all would
     take that park down and hand the issue an unbudgeted session. The full spec persisted
     in `dev_agent` is re-parsed via `_read_dev_session` and
     reused; flipping `DEV_AGENT` in env does not migrate in-flight issues. When parked on `agent_timeout` with **no**
     new comment, first attempt `_try_recover_implementing_timeout_park` (the implementing counterpart to validating's
     transient-park recovery): on a clean worktree whose HEAD advanced past the persisted `pre_implement_sha` **and
     carries commits `<remote>/<base>` does not**, clear the park and hand the recovered commit to the shared
     committed-work seam — the same one a finished run publishes through, so it is measured by the size gate and only
     then reaches `_on_commits`; otherwise stay parked silently. Both readings are taken, because the watermark says
     the checkout MOVED and not what it moved to: the commonest shape of this park is a run killed before its first
     commit, whose branch carries nothing of its own, so any advance of the base fast-forwards the checkout straight
     onto the new tip and the difference appears with no developer having written a line. Published on that reading,
     the issue gets a branch and a pull request with no diff in them. The pre-tick refresh freezes a branch parked
     like this so the rewrite does not happen at all
     ([labels-and-state.md](labels-and-state.md#base-refresh)); the base reading is what answers for a rebase
     it did not perform — an operator's, another process's, or one from before that freeze — and it fails closed, as
     does a watermark that names no commit (the park writes `""` when the pre-agent head could not be read, and every
     readable head differs from that).
     A recovered commit is not exempt from the gate: nobody read the run that made it, and publishing around the
     measurement is exactly how an oversized candidate would reach a branch and a pull request unadjudicated. This
     recovers a clean
     commit a descendant the timeout cleanup raced finishes *after* the park is recorded (the observed `#77` shape:
     commit timestamp landed after the timeout event) without needing a human "push it" comment. A real human comment
     takes precedence and drives the normal resume.
     - **A spent spawn budget** (`implementing/retry_cap.py`'s `_park_owns_the_tick`, the question
       `_handle_parked_continue_command` opens with — so ahead of the classifier below it, and ahead of the drift
       check and the resume, but BEHIND every step of the preflight above: the terminals still finalize a merged
       pull request or a closed issue over a parked one, and a `paused` / `backlog` issue never reaches a handler at
       all). Once the tick does reach it with `awaiting_human` + `park_reason=retry_cap`
       standing, this park owns it: nothing under this stage can pay for a spawn, so the tick returns having
       written nothing and said nothing (the sentence was said when the park was taken, and is replayed at stage
       entry until the thread carries it), reporting the refusal as one `retry_cap` audit record with
       `phase=standing`. Neither the clock reaching the end of the 24h window, nor a comment from outside
       `ALLOWED_ISSUE_AUTHORS`, nor words that ask for nothing lifts it — the first two answer nobody and the third
       is guidance for a developer this issue can no longer pay to run. The one reply that does is a **trusted
       `/orchestrator continue`**, looked for anywhere in the unread batch and taken with whatever else its comment
       carries: a decision that arrives with an explanation is still the decision, the explanation reaches the fresh
       spawn through the implement prompt's own comment context, and a refused tick consumes nothing — so a rule
       wanting the command alone would let one "on it, give me an hour" sit unread above the watermark refusing
       every command written after it. A park that still owes its notice is held ahead of all of that
       (`_park_is_explained`): the delivery moves the response boundary past everything written under the old
       sentence, so until it lands a command on the thread predates the question and buying an attempt with it would
       also clear the notice the human was owed. What the command buys is what `_grant_continuation`
       grants: one attempt (`retry_cap_continued`), a window reopened at that moment, and the park cleared with its
       stage and notice. The batch it was read out of is consumed in the same durable write that lifts the park —
       read again next tick it would buy a second attempt nobody asked for — up to the last TRUSTED comment, so an
       untrusted one above it stays unread for the next tick to filter out again. The
       tick then carries on to the fresh spawn below, which spends exactly that attempt. While the grant is unspent
       that spawn is the ONLY agent run this stage will make: the body-edit resume stands down for it (see the drift
       routing above), since a resume passes no gate and would leave the attempt on the issue with a run already
       made against it — and the grant is durable, so a process that dies before the spawn comes back owing the same
       one. The same write retires the pinned dev session (`_drop_poisoned_dev_session`, keeping `dev_agent`), because
       a fresh attempt is what was bought and nothing downstream can be relied on to make it one: `_spawn_implementer`
       replaces `dev_session_id` only when the run hands an id back, so a run that returns none would leave the
       transcript the cap stopped pinned for the next human reply to resume. The gate asks the same question again
       for **every** spawn a grant pays for (`spawn._charge_fresh_spawn`), because that write is not always the one
       this spawn follows: a process that dies before it comes back to an unparked issue still owing the attempt,
       and the budget is shared, so an issue can reach this spawn carrying a grant taken out on a `decomposing` park
       with a `dev_session_id` from an earlier cycle still on it. Nothing on either road touches the
       candidate, the pull request, or the late generation. `workflow:decomposing` holds the same park the same way,
       against the three roads it has instead of these
       ([its handler section](#_handle_decomposing-label-workflowdecomposing)). See
       [the retry budget](labels-and-state.md#the-retry-budget).
     - **`/orchestrator continue` operator command** (`_handle_parked_continue_command`, run BEFORE the drift check so
       the bare command is never mis-read as requirement drift). On a retryable session-failure park (`park_reason` in
       `_CONTINUE_PARK_REASONS` = `agent_silent` / `agent_timeout`) a content-free continue retries the dev
       intentionally (`_retry_parked_dev_session`): the command watermark is consumed, the session is resumed on a
       neutral retry prompt — NOT the bare command text, so the dev is grounded on its transcript (or, once
       `_resume_dev_with_text` rotates it, a fresh respawn preamble) rather than the nudge — and the result disposes
       through the normal commit / timeout / question paths, with no "issue body changed" notice. A park needing a real
       answer (any other `park_reason`) consumes the command and posts a refusal (`_refuse_parked_continue`) once, then
       stays parked (no per-tick loop). The size gate's own `late_measurement_failed` park is answered one step
       AHEAD of that classifier (`_try_recover_late_measurement_park`), because what failed there is a READING rather
       than a session: a content-free continue re-measures the recorded pair and re-publishes through the same seam,
       and no agent is spawned — the developer that produced the commit finished long ago. A worktree that is gone
       leaves the park exactly where it is rather than measuring something else, and guidance carrying real words
       falls through to the ordinary resume. A comment carrying the command *alongside* genuine guidance falls
       through to the
       normal drift/resume path so the guidance drives the dev (`_continue_command_action` returns `passthrough`). The
       classifier + parser + refusal live in `workflow/engine/messages.py` and are shared with `_handle_fixing` and
       `_handle_documenting`; a bare continue is also dropped from `_compute_user_content_hash` (see above).
  1. **A frozen candidate with no park beside it** (`_holds_unreconciled_candidate`, asked before anything
     spawns). A tick that recorded the `measuring` pair and died before counting or parking it leaves nothing on
     the issue saying the workflow is waiting. On the host that froze it the next tick simply measures again; on
     a rebuilt one the checkout comes back at base, the recorded commit is nowhere in it, and the ordinary flow
     would pay for a SECOND developer over work the first one already finished. So the record is reconciled
     first: the worktree has to be there, both ends of the pair readable in it, and the checkout actually ON the
     recorded candidate — a host without the checkout or without the candidate parks (`late_measurement_failed`)
     asking for the worktree rather than for another run, while a recorded BASE a fetch did not bring back spends one
     of the readings this pair is allowed to lose and stops the tick saying nothing until that bound runs out.
     The head is proved because no developer ran here: unlike a fresh disposition, where
     a head past the record IS a resumed developer's new commit, a moved checkout on this path is one somebody moved,
     and measuring it would answer the size question about a commit nobody froze while discarding the record naming
     the real one. Past all three the tick finishes what the crashed one started, over that exact pair.
  2. Otherwise ensure a per-issue worktree at `<WORKTREES_DIR>/<owner>__<name>/issue-<n>` on branch
     `orchestrator/<owner>__<name>/issue-<n>` (the slug-namespaced branch keeps two RepoSpecs sharing a `target_root`
     from colliding on the same `orchestrator/issue-<n>` ref). Worktrees with unpushed commits are reused (crash
     recovery); otherwise force-removed and recreated from `<spec.remote_name>/<spec.base_branch>`.
  3. If the worktree already has commits (recovered), skip the agent and dispose them as a finished run would be —
     through the committed-work seam, so the size gate measures them before anything is pushed — unless those commits
     are the ones a read-only relabel just certified (`read_only_baseline_sha` still equal to HEAD), which is a branch
     the issue arrived carrying rather than a run to finish, so the implementer spawns normally. That is a
     comparison, so a HEAD that could not be read spends nothing: `_head_sha` reports its own failure as `""`, which
     differs from the certified tip exactly as a checkout the dev has committed on does, and read that way the
     baseline is retired and the design's predecessor republished as the work the discussion just agreed to. A
     baseline stands until something SHOWS the branch has moved off it. The road with no baseline is deliberately
     untouched: there the commits are a previous run's whatever the probe says, and refusing them would buy a second
     developer over an implementation the first one already finished.
  4. Else gate the run on the per-issue retry budget (`MAX_RETRIES_PER_DAY`, default 3); a 24h window opens at the first
     counted spawn. Only fresh spawns count. An exhausted budget parks the issue durably as `retry_cap`, and that
     park is asked before the cap and the window both, so the notice that asked for a human is not answered by the
     clock or by a retuned cap. An issue a continuation has bought attempts for is answered from those attempts and
     from nothing else, so the spawn this step allows on that road is the one the human paid for — and the pinned
     dev session is retired here as it is charged (`_charge_fresh_spawn`), since what the human bought is a fresh
     conversation and this step runs on grants the tick before it did not take out. See
     [the retry budget](labels-and-state.md#the-retry-budget).
  5. Else build the implementer prompt (issue body + recent comments + "commit, do not push"), persist `dev_agent`
     BEFORE invoking `run_agent`, then spawn.
  6. Branch on result:
     - `interrupted` (shutdown sweep killed the run mid-flight) → ignore the partial result and return WITHOUT writing
       pinned state, so durable GitHub state stays exactly as the prior tick left it and the next process retries.
       Precedes every branch below and applies to both the awaiting-human and user-content-change resumes. Never posts a
       HITL question, consumes `awaiting_human`, or advances a watermark.
     - `paused` / `backlog` applied mid-run → same short-circuit as `interrupted`: return WITHOUT writing pinned
       state, so no PR opens, no relabel, no park, no watermark bump. `_paused_during_agent_run` re-reads a FRESHLY
       fetched issue (`gh.get_issue`) because the dispatch-time skip only saw the pre-run labels. Applies to the fresh
       spawn, the awaiting-human resume (including the pre-disposition `_resume_dev_with_text` poisoned-session retry),
       and the user-content-change resume. The committed work stays on the branch and republishes through step 3's
       recovered-worktree path once the label is removed.
     - `timed_out` → dispose on whether the run left a commit, which is two readings and not one
       (`_timeout_left_commits`): HEAD advanced past the pre-agent SHA snapshot **and** the branch carries commits
       `<remote>/<base>` does not. A clean advance that passes both goes through
       the same committed-work seam — the size gate, and `_on_commits` past it — exactly as a normal completion (a
       clean commit produced just before/around the kill is **not**
       stranded behind `awaiting_human`); a dirty one parks via `_on_dirty_worktree`; anything else parks
       (`agent_timeout`) with the durable `park_reason="agent_timeout"` re-set and `pre_implement_sha` persisted for
       step 1's next-tick recovery. Neither reading answers alone. The `pre_implement_sha` watermark is what tells a
       commit produced by THIS run apart from commits already carried on the branch, which `_has_new_commits` cannot
       (it only compares to `<remote>/<base>`, which a branch can arrive at this stage already ahead of). And
       `_has_new_commits` is what says the head moved onto WORK rather than onto the base — an agent that rebases or
       resets mid-run, its own `git pull`, or another process across an hour-long run moves the checkout with nothing
       written, and read as a difference alone that publishes the base branch as an empty PR. Step 1's recovery asks
       exactly the same pair, over the base a rebase between two ticks left.
       Both readings are COMPARISONS, so both ends have to have been read at all, and either missing parks
       (`_attributable_run`, shared with the clean half). `_head_sha` reports its own failure as `""` — the one value
       that cannot be a commit — so an unread end differs from every commit there is, and on a branch that was
       already ahead of base (one a read-only relabel certified, one a size-gate park left a candidate on, one a
       human's guidance resumed a developer over) that difference publishes work the run never made.
       (`_on_commits` clears the spent watermark + stale reason on publish.) Pairs with the hardened
       `processes.terminate_process_group` (SIGKILLs surviving descendants after the leader exits) so a build grandchild
       cannot keep committing into the worktree after the timeout is recorded.
     - new commits + clean tree → the **late size gate** first (`implementing/late_gate.py` and the
       `late_records` / `late_freeze` / `late_evidence` / `late_verdict` / `late_parks` owners under it), the one
       seam
       all three committed dispositions publish through — a run that finished, a timeout that had committed, and a
       branch a crash stranded. With `DECOMPOSE=on` the candidate is proved to be a commit this host holds, the base
       is frozen from what the *remote* says the branch is at, and both are persisted with `late_phase=measuring`
       BEFORE the diff is counted, so a tick that dies over the count comes back to the same pair rather than to one
       re-derived from a branch that has moved. Strictly more than `MAX_ADDED_LINES`
       ([`../configuration.md`](../configuration.md#cadence-and-budgets)) added lines routes the issue to
       `workflow:decomposing` with nothing pushed and no pull request opened; at or below it publishes as below and
       the generation is dropped, leaving `late_retired_cycle_id` so the next candidate cannot answer to the same
       cycle number. Three commits skip the measurement because this workflow already decided about them, each
       named exactly and only by its own record: the one an adjudication accepted (`late_exempt_sha`), the one the
       gate approved and has still to push (`late_approved_sha`), and the one this stage already pushed
       (`implementing_published_sha`). So does every candidate while `DECOMPOSE=off` — except
       one this issue has a recorded generation for *that same commit*, one it owes a push for, and
       one **answering a reading the gate itself recorded**. A generation naming some OTHER candidate is one a
       resumed developer's fresh commit has moved past, and the fresh commit is new work: published untouched with
       the switch off, named against the checkout like every other switched-off push, and the superseded record
       retired rather than left over a commit nothing will publish. The approval holds the switch back for the
       commit it *names* and no other, so the
       switch is asked twice: once at the door, and once past the proof, where a head that is not the approved commit
       is a resumed developer's new work and bypasses as new work does. The publication drops the stale approval on
       its way past, since a debt recorded for a commit nothing will push freezes the branch and parks every later
       tick asking for it back. A tick answering a reading a previous one recorded (a park a human answered, a
       frozen pair a crash stranded) is never new work, whatever the record says, and reading one as new work is the
       switch failing *open*, publishing the very head whose reading somebody asked for. The switch decides what
       ENTERS the gate; it does not answer a question the gate already asked, nor anything already in it.
       That exemption is *narrower* than "no developer ran": a base rebase, a conflict resolution, a divergence
       publish, and a recovery push are each taken with no agent behind them and are each a commit nothing on the
       record asked for, so with the switch off they publish unmeasured like every other fresh candidate. Reading the
       wider fact as the exemption is the switch failing *closed*, measuring the work an install that turned the gate
       off asked to be left alone and routing a pull request nobody grew into an adjudication it never opted into. A
       recorded candidate
       is proved before anything else, and the current head is never a substitute for it: a host that cannot peel
       that object parks under either switch setting, and a recorded base is retried by asking for that exact object
       rather than by re-reading a remote whose branch has moved on. A reconciliation stays bound to that pair for
       the whole tick — it proves the head against the record before it starts and the gate reads the head again, so
       one that differs on the second reading is a checkout something moved mid-tick rather than a run's output, and
       it is refused under either switch setting instead of being measured or pushed — refused before it is asked
       whether it is readable, since a head that moved onto an object this host cannot peel still names one, and a
       name handed on is what the park downstream would record in the recorded pair's place. A reading that could
       not be taken is never a small candidate: it emits `late_failure` carrying `measurement_failed` — for every
       refusal, under a minted identity where no generation exists yet or where the recorded one is not one a sink
       may carry (a damaged identity would otherwise emit nothing, and one naming another issue would file this
       issue's failure over there) — parks `late_measurement_failed`, and keeps the pair it froze for the retry.
       The record names the step as well as the family: a refusal that WAS a reading carries the
       `MeasurementFailure` it stopped at and the line that step wrote, while one that reached no reading — a pinned
       record too damaged to act on, a debt no push can pay — carries the family alone, since those say what they
       are in their own words rather than in the measurement vocabulary.
       The two steps that name the TRANSPORT rather than the work are the exception, and only for as long as the
       bound allows: a base the remote would not answer for (`base_unreadable`) and one a fetch did not bring back
       (`base_absent`) clear themselves, so the first three consecutive misses on one pair write the pair and the
       incremented `late_measurement_miss_count`, emit the same typed `late_failure`, log at WARNING and stop —
       leaving no `awaiting_human`, no `park_reason`, no comment and no recorded step, so the next tick re-enters
       that exact pair with nothing spawned — and only the fourth parks and mentions a human, recording in the same
       write the `MeasurementFailure` that mention names. Every mention this gate makes is made ONCE PER THING THERE
       IS TO SAY, whatever the cause: the post-publication reconciliation re-enters the pair on every poll after a
       park, and a tick that finds the park already standing over that same pair, stopping at the step
       `late_measurement_failure` already names, is held silently — no further miss counted, no second notice, but
       the typed `late_failure` still reaches both sinks, since those polls exist nowhere else, and a base id the
       remote finally names is recorded even there because it is what the next retry asks for. Without that field a
       candidate this host cannot peel, or a diff nothing here can pin, would mention the same people once a poll
       for as long as it took them to clear it. A poll that stops at a DIFFERENT member is not a repeat: it is a
       different next move for whoever is holding the issue and nothing else would ever tell them, so it is
       announced once and takes that field's place in the write the notice rides out on — which makes the poll after
       it a repeat rather than a second announcement. No miss is counted for it either; the bound is spent. Every
       notice a refused reading makes, on either road, names the member and explains it in a line written for the
       operator — which of a remote, a token, a throttled request, a checkout, or a planted attribute file they are
       looking at, and, for the remote read, the fetch and the two diff steps, the `orchestrator.git_plumbing`
       channel their invocation is logged under — with whatever the failing step wrote for itself, scrubbed, carried
       up beside it.
       The hold is keyed on a human still WAITING — the latch, since a resume consumes it and leaves the reason
       standing — and on the pair the park was taken over. A park failing either test is spent: a fresh candidate,
       which is what answering the park with guidance produces, retires it and starts its own bound, in the same
       durable write that records that candidate: nothing says which commit a park was taken over, so a park left
       behind by one would be read as the fresh pair's own by every tick after it. A base that IS reached
       ends the run of misses in the write that records the pair — the count and only it, since reaching the base is
       not the last step a reading can stop at and the member beside the count says what the thread was told. That
       member is dropped where every failure-prone step is behind it: the verdict a reading that HAPPENED settles,
       which clears it and the count together in the write it settles on, so the record an oversized candidate is
       adjudicated from carries no step at all. The park is retired by that same verdict rather than on the way into
       the gate, since entering it is not answering the
       question the park was taken for and a retirement there would leave an issue durably unparked whose next
       reading can miss again. Every other member parks on its FIRST miss, because a second reading of a candidate
       this host does not hold, or of a diff nothing here can pin, comes back with the same answer — and a reading
       retaken past that park is held silently by the same guard, since the answer it comes back with is the one the
       notice already named. Which readings there are to hold is decided by the road the pair was frozen on, and
       this seam is the one that retakes none by itself: a park taken here owns every tick until a trusted bare
       `/orchestrator continue` arrives, and that command clears the latch and the reason before the pair is
       re-measured, so its own miss is answering a human rather than repeating a sentence and is said out loud.
       The silent hold is the post-publication road's, where the reconciliation ahead of every handler re-enters the
       pair once a poll with nobody asked.
       Which is a pair whenever one can be established at all: a revision that resolved and would not peel comes
       back carrying the id it resolved to, and that id is recorded with the park, so the retry asks for that exact
       object and the reconciliation ahead of the next spawn proves it. A revision that would not resolve names
       nothing, and there the park itself is the record — its bare continue is refused rather than answered, since
       what a retry would take is a *first* reading of whatever the checkout points at by then and nothing ties that
       head to this issue; the way on is guidance, which resumes the developer. Either way the park holds the branch
       out of the pre-tick base refresh until it is answered, or a rebase under it would leave both the exact-pair
       retry and the refusal with nothing to be answered from.
       A count already on the record is acted on only once its identity is whole too: the domain's record gate has
       to accept the cycle, generation, and root, and `late_current_issue` has to name this issue, since a reading
       taken over there is not this issue's answer.
     - past the verdict, the same rule covers the publication the gate licensed. The write that approves a candidate
       records it as `late_approved_sha`, and the checkout is proved to be ON that commit before any later tick
       spawns or republishes — the object alone outlives the branch, so a checkout reset on the very host that made
       the commit still holds it — which is what stops a rebuilt or reset one being published, or being handed to a
       second developer because it reads as a branch with nothing on it. It is a floor as well as a proof: a run
       resumed on top of it is judged against `before_sha` too, so an agent that answered with a question rather
       than an implementation parks that question instead of having the commit it was asked about published. And it
       is spent durably BEFORE the relabel to `validating`, because past that label implementing never sees the
       issue again and a stranded approval would freeze the branch for the rest of its life. That same write records
       which commit the push carried (`implementing_published_sha`) and the head it replaced
       (`implementing_published_lease`, empty for an initial publication, which froze none), for the one effect that
       can fail on its own: a relabel GitHub would not take leaves the issue implementing with its branch pushed and
       its pull request open, and the record is what has the next tick reuse that pull request and land the label
       rather than re-decide a published branch. The head rides along because the receipt is never cleared and so
       cannot date itself — read alone it vouches for any pull request somebody later rewound onto the commit it
       names. That commit is decided once, ahead of the push, and is what the push is named against —
       where the gate proved one it is that, and where it did not the checkout names it — so the push, the receipt,
       and the proof taken once the pull request is open are all about the same commit. A checkout that cannot name
       one at all publishes nothing and parks (`late_candidate_moved`): a push named against nothing sends whatever
       the branch has become by the time git runs it, records no receipt, and leaves both proofs around it with no
       commit to hold the checkout to, so every guarantee here is off at once. The pre-push half is durable
       as `late_approved_sha`, and the proof past the pull request is the second half of the moved-checkout refusal:
       the worktree is writable while those requests run, and one that moved is parked rather than handed to review,
       with the publication left standing. Both boundaries ask about the TREE as well as the head, because loose work
       can appear with `HEAD` never moving — so every proof about the commit passes over it, while the checkout the
       handoff passes on is no longer the thing that was measured and nothing past the handoff reads it again. A tree
       that is dirty, or that `git status` could not report on, is parked as `late_candidate_moved` exactly as a
       moved head is: before the push nothing is published, after it the branch and its pull request stand and only
       the label is withheld. See
       [`../workflow/roles.md`](../workflow/roles.md#the-size-gate-a-committed-candidate-passes).
     - new commits + clean tree, past the gate → `_on_commits`: push branch, open PR (or reuse an existing open
       one), comment
       `:sparkles: PR opened: #N`, then set label `workflow:validating` (the docs pass runs only as the final-docs
       handoff after approval). A reused PR is only known to be open on the branch — most sharply, an issue relabeled
       out of `discussion` arrives with its plan PR open on the very branch these commits went to — so one whose body
       does not already name this dev session has that body rewritten to the implementation's (`Resolves #N`, the dev
       session, the agent's closing message); one that does name it is left as it stands, human annotations included.
       Without the rewrite the PR would keep claiming the branch is one Markdown file that changes nothing else, under
       the decomposer's session, and would close no issue when it merged. Persists `pr_number` / `branch` and
       resets `review_round=0` and `retry_count=0` via `_reset_implementing_counters`.
     - new commits + dirty files → `_on_dirty_worktree`: park; refuse to publish a partial branch.
     - new commits + a tree `git status` could not report on → `_on_unreadable_worktree`: park under
       `unreadable_worktree`. An unreadable tree is not a clean one: the list form of that read maps its own failure
       to "no paths", which is the answer a clean tree gives, so the seam that publishes asks the status form and
       refuses on either half of "not provably clean". An index entry marked `assume-unchanged` / `skip-worktree`
       comes back as a path AND withholds the reading, so it takes the dirty park and is named there.
     - no new commits → `_on_question`, which parks on whose words the last message is. A quota notice
       (`_is_session_limit_message`) and a transient provider refusal (`agents/sessions.py`'s
       `is_transient_provider_failure` — `API Error: 529 Overloaded` and its 5xx siblings) are the CLI's rather than
       the agent's, so both park retryably as `agent_silent` with the operator told to reply `/orchestrator continue`;
       any other non-empty message is posted as a real HITL question (`park_reason=None`); an empty one is the
       silent-failure park (`agent_silent`).
- **Output**: one of three. A pushed branch + open PR + label moved to `workflow:validating`; an **unpublished**
  committed candidate held under `workflow:decomposing` for size adjudication, with no branch pushed and no pull
  request opened; or a HITL park — the ordinary question / dirty-tree / unreadable-tree / timeout ones, plus the
  size gate's own
  `late_measurement_failed` (a reading nobody could take, a record too damaged to act on — a missing base where one
  was recorded, a missing ceiling or boundary either way, an identity the late domain's record gate refuses, or a
  `late_current_issue` naming another issue — or a recorded commit this host cannot show; a base the transport could
  not reach reaches it only once the pair has lost the readings the bounded quiet retry allows it) and
  `late_candidate_moved` (the checkout is not the one the gate
  approved — a head somewhere else, a commit not on this host at all, or a tree carrying work no push would
  publish — so nothing was published and nothing was spawned rather than hand review a checkout the gate never saw
  or buy a second developer run for an implementation that is already written; the approved commit is on the record
  as `late_approved_sha` from the write that approved it, and the park clears itself on the tick the checkout is
  back on that commit with a provably clean tree, publishing with no agent). The
  retirement that precedes a publication is held inside the observations owner's retirement window, so a close
  arriving as the record stops naming its cycle ends the cycle rather than being dropped: nothing is pushed, no pull
  request is opened, and the issue is not relabelled.

## `_handle_documenting` (label `workflow:documenting`)
- **Trigger**: each tick while the label is `workflow:documenting`. Set only by the **final-docs handoff** in
  `_handle_validating`'s approval branch (after verify + squash); the docs pass runs exactly once per
  reviewer-approval handoff, between approval and `in_review`. A PR may visit `workflow:documenting` more than once:
  if PR feedback bounces the issue to `workflow:fixing` and the dev pushes a fix, the next approval triggers another
  final-docs pass. Also runs on closed-`workflow:documenting` issues so an externally-merged PR finalizes to `done`.
- **Input**: pinned `pr_number`, `branch`, `dev_agent` / `dev_session_id` (the docs pass reuses the locked dev spec —
  there is no separate `documenting_agent`), plus `docs_checked_sha` / `docs_verdict` / `silent_park_count`.
- **Internal flow**:
  0. **A settled handoff record is ended** (`_ends_the_validating_handoff`), before anything else and in a write of
     its own. `late_collapse_handoff_sha` is what the approval that moved the label here leaves in the place of a
     collapse claim, dropped in a write BEHIND that move — so a record still on the comment when this stage runs is
     that move having landed and that write having failed. This stage is the only owner that can end one: having the
     issue is the proof the move happened, and the label history cannot tell a move that never happened from one
     step 4's drift unwind later reversed. Left standing, the unwind's re-review is answered in `validating` by
     relabelling the unchanged head straight back here. An ordinary tick carries no such record and writes nothing.
  1. **External-merge / closed-issue short-circuit** (identical to `_handle_implementing`).
  2. **`pr_number` missing → park** with `missing_pr_number`. Documenting only runs against an existing PR worktree.
  3. **`/orchestrator continue` refusal** (`_refuse_parked_continue_command`, run BEFORE the drift block). A bare
     continue on a park needing a real answer consumes the command and posts a refusal (`_refuse_parked_continue`) once,
     then stays parked. A retryable session-failure park (`agent_silent` / `agent_timeout`) and a command carrying
     genuine guidance both fall through: because a bare continue no longer shifts `user_content_hash`, the drift block
     below stays silent (no spurious `routing back to validating`) and the retry reruns the FULL docs pass through the
     awaiting-human resume (step 10). The parser + classifier are shared with `_handle_implementing` / `_handle_fixing`;
     documenting has no preserved feedback batch, so only the refusal needs interception here.
  4. **User-content drift → relabel back to `workflow:validating`** without spawning the docs agent. A title/body edit
     (or fresh human comment) during the final-docs hop invalidates the prior approval, so the reviewer must
     re-evaluate before any docs work can land. Housekeeping: post a `:pencil2: routing back to validating` notice,
     advance `last_action_comment_id`, refresh `user_content_hash`, clear park flags, reset `review_round=0`.
     Reconcile the PR worktree (fetch, then probe ahead/behind; on `ahead > 0`, `behind > 0`, or dirty files run `git
     reset --hard <remote>/<branch>` + `git clean -fd`) so no docs work authored against the pre-drift requirements
     survives. `docs_drift_unwind_pending` is set while the cleanup is in progress and cleared only on the relabel
     back to `workflow:validating`, so an operator unpark on a parked cleanup re-enters the drift block instead of
     falling through to a docs spawn.
  5. Awaiting-human + no new comment → early return BEFORE the fetch so a transient `fetch_failed` / `diverged_branch`
     doesn't re-post its park every tick.
  6. Ensure the PR worktree (`_ensure_pr_worktree`, restored from `<remote>/<branch>` so the dev's commits are intact)
     and refresh via `_authed_fetch`. Failure parks with `fetch_failed`.
  7. Divergence reading vs. the just-fetched `<remote>/<branch>`. The ref is resolved once and HEAD is counted
     against that immutable commit, so the counts and the head they were taken against name the same tip — read
     twice, a ref something moves in between leaves the branch proved against one head and the push pinned to
     another, and where the pull request has moved to the second that lease is satisfied and the force-push lands on
     top of it.
     - **unreadable** → park with `unreadable_divergence`, before the spawn and before any push. A ref nothing could
       resolve, a comparison git refused, and a count in a shape nothing can parse all answer `(0, 0)`, which is what
       an in-sync branch answers — collapsed into that, a stale checkout is spawned over and force-pushed, and the
       head that push is pinned to is empty, which has the size gate adopt whatever the pull request has moved to.
     - `behind > 0` → park with `diverged_branch` (force-pushing would clobber the real PR head).
     - `ahead > 0` recovered commits → synthesize an `AgentResult` and skip the agent; the unified branch below pushes
       the recovered docs commit, pinned to the tip this count was taken against rather than to whatever the pull
       request is standing on by then.
     - `(0, 0)` → fall through.
  8. **A pass whose commit the pull request already carries** (`_finished_settled_docs`), asked between the reading
     above and the run below. A `docs_settled_sha` receipt is left by any tick that published and did not finish. The
     size gate in step 12 **held** an oversized docs commit off the pull request and handed the issue to the late
     coordinator, and a settled `single` verdict publishes that commit from there and hands the label back here with
     only the handoff owed. Or the gate **allowed** the push, it landed, and the tick died before this stage could
     record it — the receipt rides the gate's own write either way, which is ahead of everything this stage does with
     a landed push, and it is the write RECORDING the pass that drops it, so a receipt read here is one no handoff
     has been made for. So the receipt is read back, and where the branch is in sync AND the checkout is standing on
     that exact commit this tick stamps `docs_checked_sha` / `docs_verdict="updated"`, announces the handoff, and
     advances to `in_review`, with no agent run and no push.
     Without that receipt the tick would read a branch in sync with its remote as an issue no docs pass has run for
     and spawn a second agent over work that is already published. Ahead of the remote the receipt stands and
     the `ahead > 0` road republishes it through the gate, which is the one road that measures it again; a receipt
     that is not a whole object id, or a head this host cannot peel, likewise leaves it for a tick that can prove it —
     in sync is not the same claim as CARRYING it, since a replacement host rebuilt from a pull request that has moved
     on reads level with its remote too.
  9. Whichever shape runs below, the `docs_verdict` an EARLIER pass left is dropped as this one begins. Every shape
     re-anchors `docs_checked_sha` to the head it is about — the resumed dev that adds nothing to a commit already
     waiting anchors on that very head — so a stale verdict beside it would say a pass has FINISHED for the head this
     one is only starting on — which the in_review merge gate reads as a head this orchestrator has documented, and
     pings as ready for a human to merge, from the moment this pass spawns until it finishes.
  10. Awaiting-human resume: rebuild the FULL docs prompt via `_build_documentation_prompt` (this may be the first time
     the session sees the docs-stage instructions), persist `docs_checked_sha=before_sha` BEFORE the spawn, then
     `_resume_dev_with_text`.
  11. Fresh spawn: snapshot `before_sha`, persist `docs_checked_sha=before_sha` and `dev_agent` BEFORE invoking the
      agent, build the prompt (issue body + recent comments + `DOCS: NO_CHANGE` marker contract), then run.
  12. Branch on result. Every success exit routes to `in_review` via `_advance_after_docs_push` /
      `_advance_after_docs_no_change`, which ratchets `pr_last_comment_id` past any issue-thread reply the resume
      consumed so in_review does not bounce over already-addressed feedback. Both end the same way and the order is
      the crash contract: **stamp, announce, persist, relabel** — one durable write, and the relabel behind it. The
      notice comes before the write because posting one RECORDS it: the comment id lands in
      `orchestrator_comment_ids`, which is what has the watermark walk seed past it and the in_review feedback scan
      drop it rather than resume a dev over an informational post of ours, and there is nothing behind the write to
      carry it. The write comes before the relabel because `in_review` repairs nothing it is handed: its merge gate
      pings only for a head that `docs_checked_sha` names with a `docs_verdict` beside it, so a relabel taken first
      strands the issue on a stage whose handler never looks at either. That same write drops `docs_settled_sha`,
      and it has to: the receipt says a published pass still owes a handoff, and held past this write to cover the
      relabel it outlives the handoff whenever the write that would drop it does not land — leaving a record a later
      `validating` approval at the same head consumes, skipping the docs pass that approval just bought. Two windows
      are left and both fail toward doing the work again: a tick that posted its notice and died over the write
      comes back with nothing on the record saying so, and step 8 announces it a second time; a tick whose relabel
      did not land leaves the record a same-head re-entry leaves, so the next tick runs the pass rather than handing
      off on evidence that could belong to either. Branches:
      - `interrupted` (shutdown sweep killed the run mid-flight) → ignore the partial result and return WITHOUT writing
        pinned state (the pre-spawn `docs_checked_sha` / watermark writes are discarded), so the next process
        re-runs the docs pass. Precedes every branch below. The recovered `ahead > 0` path synthesizes a
        non-interrupted result, so it is unaffected.
      - `paused` / `backlog` applied mid-run → same short-circuit as `interrupted`: `_paused_during_agent_run`
        re-reads a FRESHLY fetched issue after the initial-docs and awaiting-human resumes, and on a hit the handler
        returns WITHOUT pushing, posting the docs notice, advancing to `in_review`, ratcheting watermarks, or writing
        pinned state. The committed docs work stays on the branch and republishes through the `ahead > 0` recovered
        path once the label is removed (the recovered path itself runs no agent, so it observes no live-pause window).
      - `timed_out` → park (`agent_timeout`).
      - dirty worktree → `_on_dirty_worktree`: park.
      - new commit on a clean tree → the **size gate** every push onto an open pull request goes through
        (`implementing/late_push._publishes`, reached from `documenting/publication._push_docs_and_advance`). What it
        counts is what the pull request comes to WITH the docs commit in it, against `MAX_ADDED_LINES`; the push it
        licenses is named to the commit this pass made and pinned to the head the pass was entered on — the tip the
        step 7 fetch read — so a pull request somebody pushed to while the agent was out refuses the push instead of
        being adopted as its lease. What comes back is `held`, `landed`, or neither, and `held` is **not** one
        outcome — it means only "this tick is finished, publish nothing and hand the issue on to nothing", and the
        three ways to earn it differ in everything else:
        - **oversized** → the adjudication hold. Nothing is pushed, no docs verdict is stamped, no `in_review`
          handoff is made, the issue is relabelled `workflow:decomposing`, and the head the pass produced goes down
          as `docs_settled_sha` inside the gate's own routed write, ahead of that relabel (step 8 reads it back).
        - **a reading the gate could not take** → a park, not a relabel. A tree that is not provably clean, a pull
          request nothing could read or one that is closed or merged, a caller-named head that is no whole object id
          or that disagrees with the head the gate reads, a head that moved off what a live record froze, a count
          that never happened, or an approval whose lease cannot be read: each parks `late_measurement_failed` with
          nothing pushed, no label moved, and no `docs_settled_sha` written.
        - **the push landed and the checkout stopped being what went out** → the publication stands and only the
          HANDOFF stops. The branch and its pull request carry the commit, the receipt and the debt are settled, and
          the issue parks (`late_candidate_moved` for a head that moved, `dirty_worktree` for a tree dirtied under
          the push) so the reconciliation ahead of the next handler restores the checkout rather than handing a
          reviewer one nobody read.

        **Landed**: record `docs_checked_sha=after_sha`, `docs_verdict="updated"`, reset `silent_park_count=0`,
        drop `docs_settled_sha`, post `:books: documenting pass: pushed docs commit.`, persist, and advance — once.
        **Neither** (allowed, and the push itself failed): park
        (`push_failed`), with the commit that is owed a publication and the head to pin it against left on the record
        for the retry.
      - no commit + `DOCS: NO_CHANGE` verdict: when `ahead > 0` the recovered commit goes through that same gate and
        earns the same answers — the verdict certifies the local tree and says nothing about what the remote carries;
        otherwise persist `docs_verdict="no_change"`, post `:books: no docs changes required.`, and advance without
        pushing.
      - no commit + unknown verdict → `_on_question`: park.
- **Output**: label moved to `in_review` (success), OR `workflow:validating` (drift unwind), OR
  `workflow:decomposing` (a docs commit the size gate held), OR terminal `done` / `rejected` (short-circuit), OR a
  HITL park.

The docs pass is deliberately a thin dev-session rerun on the existing PR worktree rather than a separate role: there is
no `documenting_agent` pin and no separate retry budget. The dev session resumes on its locked `(backend, args)` spec,
so `DEV_AGENT` flips made mid-flight do not retarget the docs pass either.

## The size gate on a published pull request (every push onto an open PR)

Every push onto a pull request the remote already carries goes through the same late size gate
(`implementing/late_push._publishes`, over the `late_overflow` entry, the `late_publication` answer, and the
`late_gate` owners that answer for the implementing seam too). There are nine
such pushes and no others:

- the shared dev-fix publication `validating/dev_fix._publish_dev_fix` (the reviewer's `CHANGES_REQUESTED` loop, the
  awaiting-human resume, and the body-edit drift resume an open PR takes);
- the fixing handler's no-feedback bounce `fixing/handler._publish_stranded_fix`;
- the two `validating/recovery.py` retries that finish a deferred push or a commit a timeout killed the disposition
  before it saw, both through `_publish_recovered_fix`;
- the conflict resolution `conflicts/outcomes._finalize_conflict_resolution`;
- the recovered-commit publication `conflicts/divergence._push_recovered_commits`, which ships a resolution an
  earlier tick committed and never pushed;
- the clean-rebase publication `conflicts/publication._publish_clean_rebase` — the last of these is also the only
  seam outside the squash that hands the gate a rewrite's before-state, since it is the only one that ran the
  replay it is publishing — those three, plus the body-edit resume
  through the shared dev-fix seam, are what
  [`workflow:resolving_conflict`'s content updates](#content-updates-onto-the-pull-request-this-stage-already-has)
  are made of;
- the base-sync auto rebase `git/base_sync/publication._publish_auto_rebase` and the two roads of its own crash
  recovery — `git/base_sync/recovery._retry_recovery_push` for a push that never went out, and
  `_settle_published_recovery` for the leased no-op that receipts one that did — all of which reach the gate through
  `base_sync/publication._gated_publication()` so the sync layer keeps its call-time hop upward;
- and the final documentation pass `documenting/publication._push_docs_and_advance`.

One more seam pushes without measuring, and it skips the reading for a reason and nothing else beside it.
`decomposition/late_verdict_push`'s `_publishes_approved` ships a commit a human's adjudication already accepted:
the checkout is re-proved, the push is named and leased from the record, and the debt is spent exactly as it is here.
`git/publication/squash` is not one of them: the squash-on-approval goes through the whole gate, through
`late_rewrite`. What it publishes is a NEW object — a squash collapses the approved commits into one commit that did
not exist when any earlier push was measured — so that commit is the candidate, proved, frozen against the base the
remote names now, counted, and either pushed or held. Measuring the head it replaces would gate one commit and
publish another. The count it earns is ordinarily the one the last gated push already answered, because the tree is
the same tree; ordinarily is not always, since the BASE moves, and this is the last push before a human is asked to
merge. A **routed** candidate is deliberately not rolled back: the squashed commit is what a settled verdict
publishes from the branch, so restoring the pre-squash head would leave the record naming a commit this host no
longer has — and the approval handoff stops without parking, since the gate owns the issue from there.

One squash is not counted at all, and it is the one the exemption would otherwise punish. Where the head being
rewritten is the exact commit an adjudication accepted, the squash hands the gate its own before-state — the head
it replaced, the merge base both sides are read over, and the publication it was entered on — and `late_transfer`
may carry the exemption onto the object it produced. Only over the whole of the evidence: a semantic record whose
exempt commit is the one being rewritten and which proves itself when re-fingerprinted over its own recorded pair,
no authorization this build cannot read already standing for that exemption, a publication this call itself froze
and the issue still records, a provably clean checkout on the squash, an issue re-read open, unpaused and still on the
stage the rewrite was entered from, and a rewritten
contribution that fingerprints to the same digest. The PERMISSION is durable before the push, in ONE write that
also records the debt that push is owed — split in two, a crash between them leaves a one-commit branch the next
squash reports success on without pushing. The exemption does not move there: that rotation belongs to the receipt
of the landed push, which `late_rotation` stages into the push tail's own settlement, so a verdict is never left on
a commit no remote carries — and only where the permit itself proved out on that tick, since a refusal sends the
rewrite to the ordinary gate, which publishes it whenever the count is under the ceiling. What that receipt leaves
on both observability streams is one bounded `late_transfer`
record naming both pairs, the pull request, the rewrite kind, and which reading proved the publication — the leased
force-push that moved it, or the leased no-op a recovery finds it already standing on.
A digest the standing permission already recorded is held to the reading the permit just took, since a grant that
carried on would write its own answer over evidence nobody checked. Refused, nothing changes and the squash is
measured exactly as above. And the permission is droppable in exactly one window — a force-push the remote refuses
resets the branch back onto the commit the exemption never left, so the rollback takes the permission back and
nothing else, while past the receipt the pull request carries the rewritten commit and there is nothing to take
back. The squash is not the only rewrite decided on those terms: the per-tick base refresh publishes a clean rebase
of the same branch once this stage has handed the issue on, and it hands the same gate the same evidence — assembled
afresh by its own crash recovery where the tick that made the rewrite died before recording it
([`labels-and-state.md#base-refresh`](labels-and-state.md#base-refresh)).

The conflict stage's clean rebase is the third rewrite an exemption may ride, and it reaches it from the other end.
That refresh does not drive `workflow:resolving_conflict`, so the replay a branch which has stopped merging cleanly
needs is this stage's own — and so is the account of what it replaced. So
`conflicts/publication._publish_clean_rebase` reads the pre-rebase head and the fork point that head's contribution
was read over BEFORE the replay destroys both, and hands them to the gate through `conflicts/evidence`, which builds
the record and decides nothing. That head is also the head the force-push is leased against, which is where this
differs from the squash: there the collapsed head and the lease are two facts. The two contributions are read over
two DIFFERENT fork points, because moving the base is the whole of what a rebase does, and `late_transfer` grants
the permit only over everything above — including that the two fingerprint alike.

The replay also writes itself DOWN, because the tick that runs one is not always the tick that publishes it. The
head it is about to replace, that head's fork point, and the pull request it is being made against go onto the
pinned comment before the rebase runs — the `conflict_replay_*` group in
[`labels-and-state.md`](labels-and-state.md#pinned-state) — and the commit it produced is stamped on before the size
gate is entered. That record is what a crash between the replay and the gate leaves behind, and the tick that finds
it reads it twice. A replay moves the branch off the head it replayed, so the checkout comes back ahead of the pull
request AND behind it — the shape `_guard_diverged_worktree` parks, since a stale branch carrying somebody else's
commit reads the same. The record naming that head, that commit and that pull request is what lets it past, leasing
the force-push to the pre-rebase head; then `conflicts/divergence._push_recovered_commits` reads it again for the
evidence it hands the gate. It is read only where it is about the publication and the commit in hand: the pull
request it names has to be the one the issue still records, the head it names has to be the head that push is leased
against, and the commit it names has to be the one the checkout is standing on. The publication is on the record
rather than read live because `pr_number` can be repointed in between, and a replay offered as a rewrite of some
other open pull request standing on the same head would satisfy every check the permit makes.

Nothing else this stage publishes presents evidence, and the reason is that nothing else can say what it is
publishing. Every other push here carries a commit somebody ELSE made — a resolution an agent authored over
conflicted files, the unpushed FIX commits the `fixing` dead-lock reroute sends over, a commit made on top of a
replay — and no reading off the branch tells those from a replay. Being on base tells them apart least of all: that
reroute fires on an on-base unpushed commit as readily as on a stale-base one, which is exactly why the record
rather than a probe is what the recovery turns on. Past the grant the permission takes over: `late_transfer` falls
back to it when a caller presents nothing and re-asks the whole permit over it. The dev-fix publications, the
reviewer's fix loop, and the documentation pass are the same rule one stage over. All of them go through the
ordinary cumulative gate — and a replay that changed a single covered byte joins them, since it fingerprints to a
different contribution and earns the fresh late adjudication any oversized candidate is owed.

A candidate whose count never came back keeps the rewrite too, and for the same reason read one step earlier: the
freeze is durable and the diff is not, so a reading that fails leaves a live generation naming the **squash** with no
number on it — and the reconciliation ahead of the next handler answers that pair by measuring the checkout it was
frozen on. Restored, the record names a commit the branch no longer has and every later tick refuses it as a
candidate that moved, so the measurement is never retried. The rule the two share is one: the branch may go back only
where nothing durable is left pointing at what is on it.

A **refused** one is that, and is told apart before anything is restored. The entry read that runs before
the reset cannot cover the window the reset and the commit sit in: a human closing the pull request, or somebody
pushing to it, is visible only to the gate's own second reading — and closing one does not move its branch, so no
lease and nothing local would notice. There nothing was measured, nothing was pushed, and nothing was recorded — an
entry that could not prove itself deliberately persists no generation — and leaving the commit on the branch is a
fail-open: a one-commit branch takes the nothing-to-squash road on the next
tick and reports **success** without measuring or publishing anything, so reviewer-approved work reaches the merge
button neither counted nor on the remote. So the branch goes back to the commits the reviewer approved and the retry
squashes, measures, and publishes them afresh. It still reports `held` — the gate has already parked with the notice
its own reading earned, and a squash-failed park on top of that would describe a failure that did not happen. Two
other holds keep the rewrite for the same reason the recorded ones do: a push that LANDED and only held the handoff
(the receipt names the squash, so the remote carries it), and a checkout something committed over (a reset would
destroy work nobody here can account for). The debt counts as a record too, and it has to: a transfer whose grant
landed and whose push the remote took leaves the **approval** naming the squash while the receipt still names the
head that squash was pushed over. A reset there would take the checkout off an object the remote already carries.

The transfer's own write is handled where it happens rather than allowed out: a refused grant puts its staged
payload back and falls through to the ordinary reading, so a lost write costs the permit and not the tick.

The squashed commit is checked on **both sides** of that gate. The gate proves the checkout for itself, and a first
generation has no record to prove it against, so something committing over the worktree between the squash and the
freeze would be measured and published in its place while the caller went on to record the id it made. Refused
before, nothing is measured and nothing goes out; refused after, it is asked of the commit the gate actually decided
about, which is the only reading no window sits inside. Neither rolls back — whatever moved the checkout made a
commit nobody here can account for. A push that was allowed and then FAILED does roll back, and drops the approval
it abandons in the same breath: the gate records the squashed commit as one still owed a publication before the
push, and the reset leaves that commit only in the reflog, so a debt naming it would stop every later tick for a
publication that is never coming.

Every one of those resets takes the **ref and the index and leaves the working tree alone**, which is the difference
between restoring a branch and destroying work. A squash is a collapse rather than an edit — the commit it makes has
the same tree as the head it replaces — so on a checkout nobody touched, taking the working tree too would land in
exactly the same place. The two part where somebody wrote to the worktree between the squash and the reading that
refused it, which is the gate's first refusal: a tree that is not *provably* clean. Taking the working tree there
would throw that edit away to undo a squash it had nothing to do with, and it is the one repair a human cannot get
back. Left where it is, it survives the restore as the uncommitted change it was, and the retry refuses on the same
tree — the planning probes stop on a dirty worktree — rather than collapsing it into a squash nobody asked to carry
it.

The entry is asked twice, and the first time is not redundant. One refusal there is a hole no lease covers:
**closing a pull request does not move its branch**, so a `--force-with-lease` succeeds against a publication nobody
can merge. Asked before the reset that rewrites the branch locally, a closed, merged, or unreadable pull request, a
dirty tree, or a head that moved costs a refusal with the reviewer-approved commits exactly where they were — and
the approval handoff parks and stays in `validating`, which is what it already does for every other squash
failure.

A rebase may not happen inside that window either. `late_collapse_*` freezes the branch out of the pre-tick base
refresh on the same terms the size gate's own records do, and on the strictest reading of the three — the key being
on the comment at all, `null` included — because that is exactly what the squash's own reader counts as a claim it
must refuse. A rebase there replaces the collapse with a commit carrying the base advance too, so the tree proof
below stops answering and the pull request is left on the history the record says was collapsed with the rebase
already force-pushed over it. The freeze ends when the record does.

A squash says what it is about to do before it does it, and that is what closes the window neither reading covers:
the process itself dying. The head being collapsed, the base it is collapsed over, and how many commits go in are
written to the pinned comment between the entry and the reset (`late_collapse_head`, `late_collapse_base_sha`,
`late_collapse_count` — see
[`labels-and-state.md`](labels-and-state.md)), because past the reset none of the three can be read
back off anything: the head is off the branch, the base is not derivable from the object that replaced it, and the
count is gone with the commits it counted. That count is **walked** rather than taken from the commit subjects
beside it: `git commit --allow-empty-message` makes a commit that contributes no subject and one commit, so a count
derived from the subjects is short by however many of those a branch carries — which would record three commits as
two and have the recovery refuse a collapse it really made as miscounted, and read a branch of two as the single
commit that takes the nothing-to-squash road. A write GitHub refuses stops the squash rather than running it
unrecorded — the approved commits stay where they are and the next tick tries again.

That write is a **request**, so the head and the tree are proved once more when it comes back. The worktree is
writable for the whole of it, and the reset behind it is `--soft`: the commit that follows takes the INDEX, so a
change staged in that window would be collapsed into the squash and force-pushed onto the pull request as work a
reviewer approved. Both halves refuse, the record of a rewrite that never happened goes with the refusal, and the
notice says which of the two moved — a tree that went dirty leaves the approved commits exactly where a human will
look for them, a head that moved has not been shown to.

The tick that comes back reads that record before it reads the commit count, and the order is the whole point: a
collapsed branch and a branch with nothing to collapse both carry one commit. It also proves the record before it
compares it to anything, the road that DROPS the record included — a head edited onto the commit a finished
collapse left reads as a reset that never landed, so a shortcut taken for one would drop the record and hand on a
branch of one commit, which is the nothing-to-squash road reporting success over a remote still carrying the history
the record names. Past that proof, exactly one branch may be dropped over: the one the record still describes
exactly, standing on the head it names over the commits it counted, which is the tick that died before the reset
ever ran. That drop is safe because the branch is the one the record was written over — the ordinary squash
collapses exactly the commits an approval was given for, and it cannot report success without pushing them, since it
goes through the entry, the rewrite, and the push and refuses if any of them will not have it.

Every other shape refuses. A head that matches over a different number of commits is a branch something rewrote
while the record went on naming its old tip. A branch carrying **nothing** over its base is the shape the ordinary
squash could not be trusted with: there is no collapse left to finish and no history left to squash, while the
remote still carries every commit the record names. And a branch that MOVED off the recorded head is refused
whichever way it went, because nothing here can say who moved it — this recovery owns the tick from the moment a
record goes down, ahead of every route that could resume a developer, so work on top of the recorded head is work
nobody in this workflow made and squashing afresh would force-push it onto the pull request as history a reviewer
approved. The two ways it moved are still told apart in the notice, since what an operator does next differs: a
recorded head still reachable from the branch has the approved commits under whatever was committed over them, and
one the branch replaced has them only in the reflog.

The rest are **resumed** through the same leased publication the squash itself would have made — entered on the head
the record names, handed the pair the record holds as the transfer evidence no plan is left to supply, and finished
with the count only the record still has. A collapse that landed locally is measured and pushed; one the gate
already approved publishes on that approval; one an outstanding transfer permission licensed is re-asked in full and
publishes unmeasured; one a receipt says this issue's own push already put on the pull request is entered on the
*rewritten* commit instead, so it is the leased no-op that settles the receipt, the debt, and the exemption rather
than a second reading of work a human already ruled on; and a settled receipt whose handoff never finished is that
same state one step on, finished with the notice and the relabel it was owed.

No road above is taken over a tree this host cannot **prove** clean, the one that hands the branch back to the
ordinary squash included. The planning probes refuse on what git *named*, so a status that established nothing reads
to them as a clean tree; an install with `DECOMPOSE=off` reads no pull request, so the entry behind the rewrite
proves no tree either. Between those two there is nothing else standing between an unreadable worktree and a
force-push, which is why the proof is owed before anything is classified rather than only before the road that
publishes.

None of it runs on the record's **shape** either, and neither does any road above it. A whole-looking record is one
somebody could have written, not one this repository ever produced, so four things it claims are proved against the
objects before any of them are compared to the branch: both recorded ends peel
to commits this host really holds, the recorded base really is a commit the recorded head was built on — a walk
between two histories that never met reports a number like any other, so the count is no ancestry proof — the history
between them really is the number of commits the record counts, and the commit on the branch carries both the tree
the recorded head left *and* that base as its one parent, which is what a squash produces exactly and by
construction. The parent is not decoration: the same tree re-parented onto a base that has since advanced is a commit
that *reverts* whatever that base added, and a tree comparison alone would push it onto the pull request under an
exemption a human granted a different change. A record that fails any of them
leaves the branch untouched and refuses, because every other refusal here knows what the branch is standing on and
can put it back while this one is the answer to not knowing. Anything else refuses on the terms every squash refuses
on — a pull request a human closed, a remote somebody moved off both heads the collapse accounts for, a tree that
stopped being provably clean — and the branch goes back only where nothing durable names what is on it. A record
this build cannot read **whole** refuses outright rather than being waved past, since the branch behind such a claim
is exactly the one commit that reads as nothing to squash.

A resumed publication that does not go out is rolled back like any other, with one exception: where the pull request
already carries the commit, that push sends nothing, so a request that fails is a transport failure over work the
remote has. Reset there, the checkout would come off a commit the pull request carries, the count the notice is still
owed would go with the record, and the next tick would find a remote that moved for reasons nothing on the comment
explains. Two readings say the remote is there and the **entry** is the stronger, since it is a reading of that pull
request taken this tick: a tip it froze equal to the commit about to be pushed, which it admits only where a durable
record accounts for it — so it covers the crash between a push and its receipt, where the receipt is precisely what
is missing. The receipt dated to this attempt is asked beside it for the road that read no remote at all. Neither can
fire on a fresh squash, whose entry was frozen before the commit existed; and the approval the gate writes before a
push, and the permission a transfer holds, are records a reset is *supposed* to drop, so neither is asked.

The record outlives the push, and the **handoff** is what ends it. The count on it is what the
`:package: squashed N commits to 1` notice is worded from and nothing else on the issue has one, so a notice that
was owed and did not post leaves the record standing and the label where it is — the next tick republishes the
commit the remote already carries as the leased no-op it is and words the notice again. The write that ends it
lands **before** the relabel, because past the label the issue belongs to `documenting`, a stage that never runs
this recovery: a tick dying between the two would strand a claim nothing there could answer and lose the watermarks
the same write carries.

That write does not leave the boundary empty, though, because the relabel is a second call and can fail on its own.
What it ends is the **claim**; what it leaves in its place is `late_collapse_handoff_sha`, the commit the move is
owed over. An issue left on `validating` with nothing on the comment is one the next tick runs a second reviewer on,
over a branch already approved, squashed, and published — so the recovery route reads that record ahead of the
reviewer and moves the label instead, then drops it in a write of its own behind the label. It is spent only while
the pull request is still standing on the commit it names: anything that moved the publication on — a docs pass that
pushed, a fix round, a rebase — has moved the work past the round the record was about, so it is dropped and the
tick goes to the reviewer rather than sending unread work to `documenting`. Being no claim of an outstanding
rewrite, it freezes nothing and refuses nothing — and it is held to the shape every other recorded end is, a whole
object id, because what it is spent on is a comparison against the head the pull request stands on and an issue
with no pull request to read has nothing else between such a value and a label moved past the reviewer.

A squash failure names which of **four** places it left the branch, so the park notice does not send an operator
looking at HEAD for approved commits that are only in the reflog — or to a reflog entry for commits that never left
the branch. The reading is exact rather than a default: an outstanding record read whole whose head is the head the
checkout is on is the rewrite that did not happen, and the approved commits are where a human squashing by hand
will look. A branch that moved off a recorded head this host really holds is two shapes, and the *ancestry* tells
them apart — a recorded head still reachable from HEAD is BURIED, with nothing rewritten and the approved commits
under whatever was committed on top of them, and one the branch replaced is the collapse, whose reflog entry the
notice names. Anything else — a record this build cannot read whole, a recorded head no object here answers to, a
checkout that would not report its own head, or the record-write race, which drops the record as it refuses — is
UNKNOWN, and the notice says so rather than claiming any place at all.

What the gate measures is what the pull request would **come to**: the count is
three-dot from the base the *remote* names to the candidate commit, exactly as it is before the first publication, so
it is the whole pull request rather than the diff this one push adds. Without it a branch could be grown past
`MAX_ADDED_LINES` one small fix at a time, which is the outcome the gate exists to prevent.

**The commit the caller named is the commit the gate decides about.** Every seam that reads a head for itself names
it — the docs pass, the squash, both conflict resolutions, the crash-recovered conflict push, the auto rebase, and the
base-sync crash recovery behind it — and the gate proves the checkout again, because a caller's word is not a proof.
Between those two reads the worktree is writable, so a commit landing there is a *different candidate*: measured,
pushed, and recorded by the gate while the caller goes on to stamp the id IT read as what it published — in the notice
it posts, the audit event it emits, and the round it records. So the caller names it, and a checkout standing anywhere
else parks `late_measurement_failed` — before anything is persisted and before anything is pushed, since a refusal
after the freeze leaves a record about the wrong commit and one after the push leaves the wrong commit on the pull
request.

The approval a crash left owed a push names its commit too, off the record rather than off a read: a debt is a claim
about ONE commit, and the checkout is proved to be standing on it before the gate is entered. Read once and named,
that proof and the gate's own reading are about the same approval — a commit landing between them is refused rather
than published under a decision taken about another one, with the debt dropped as paid. Empty for the seams that
publish a checkout they did not just write (the no-feedback bounce, the reconciliation answering a recorded pair),
where the head this gate proves is the whole of the answer.

**The head a round began at is named too, and it is the PUBLICATION's rather than the checkout's.** A fix or docs
round opens with the branch in sync with its pull request — the reviewer just read that head — so the head the run
started on is the head the publication was standing on, and it is what the round hands the gate. Left for the gate to
read afterwards instead, a push somebody else landed while the agent was out becomes the lease: the candidate was
built on the head the branch used to be on, so the force-push puts it there and takes the other push with it. Named up
front, the two readings of that one fact disagree and nothing is measured or pushed at all. The timed-out recovery
names the anchor the killed run left for the same reason, and the seams publishing a commit an earlier tick stranded
name the remote tip their own proof was taken against — the probe fetches the branch and compares HEAD to that tip, so
the tip is what their push replaces, and a head somebody landed between the probe and the push disagrees with it
rather than being adopted as the lease and force-overwritten by work proved against the head it used to be on. A tip
nothing could read is no head either, and refuses there rather than publishing against one.

**What the gate hands back is spent on the push.** Not merely its permission: the commit it measured, and the head the
entry froze. The push is named against the first, so a checkout another tick, an operator, or a stray descendant moved
between the reading and the push publishes the measured commit rather than whatever it became. It is leased against
the second (`--force-with-lease=refs/heads/<branch>:<sha>`), so a pull request somebody pushed to inside that same
window rejects the push instead of being adopted as the lease and silently overwritten by work measured against the
head it used to be on.

Two of the three frozen facts are the **caller's** where it has them, and both because a fact this gate would
re-read is a fact that can have moved since the caller acted on it. The *head* is the caller's: the conflict and
base-sync publications each read the remote themselves and pin their push to what they read, so freezing anything
else would leave the immediate push refusing a head that moved while a settled adjudication — which re-pins from
the record — pinned to the head that moved and overwrote it. It is **checked** against the head this gate reads
rather than substituted for it: the two are readings of one fact, the tip of the branch the push is going onto, so a
disagreement is somebody else's push landing mid-tick and refuses. Preferring either would freeze a head that is not
what the branch would be pushed onto — and an oversized candidate would then be persisted and routed to the
adjudication on evidence already overtaken. A caller-named head that is not a whole object id refuses for the same
reason one step earlier, rather than falling back to the read: a caller that established a head made its own
decision on it, and a fallback would pin the push to a fact that decision was never taken over.

The *stage* is the caller's on the one route that relabels remotely and then publishes in the same tick (the
reviewer's `CHANGES_REQUESTED`, which flips to `fixing` before the dev spawn): PyGithub does not refresh a fetched
issue's labels, so reading them back would freeze the state the issue has left and a settled verdict would continue
there. Whatever the caller names is still checked, and against the five states that publish onto a pull request the
remote already carries rather than against the transition graph. Where the switch kept the candidate out of the gate
no entry was frozen, so this owner read no pull request and has no head of its own — but the push is neither unnamed
nor unleased. The COMMIT is named off the checkout, because the switch keeps candidates out of the measurement and
not out of a push that knows what it is publishing. The LEASE is whatever the CALLER established: the conflict and
base-sync publications each read the remote for themselves, and dropping that would make `DECOMPOSE=off` the setting
that turns a force-with-lease into a blind force-push. Only a caller that established none leaves the push to lease
against git's own `ls-remote`. Both steps behind the push are claims about that one object id:
the receipt that records what reached the remote, and the proof that the checkout is still standing on it. Handed an
empty name they read a checkout that never moved as one that did, so a landed push would record an empty receipt and
then park the issue for a head sitting exactly where it was left.

**What the caller established is applied before the switch is asked, not after.** *Answering a recorded reading* is
one of the three states `DECOMPOSE=off` has nothing left to say about, so such a call is entered, named, and leased
whatever the switch says. Asked over a subject the caller's terms have not been applied to, the switch would read one
as new work and hand back a push with no commit to name and no head to pin — the two races the naming and the lease
exist to close. That is the shape a retry lands in: an entry that refused persists no generation, deliberately, so a
tick taken after the switch was turned off has nothing on the pinned comment to tell it from new work and only the
caller's own terms say which it is.

That claim is carried separately from *no developer ran*, which is the wider fact beside it and decides something
else: whether a head that is not the recorded candidate is a resumed developer's fresh output or a checkout something
moved. Every seam that answers a recorded reading also has no agent behind it, and several that have no agent behind
them answer nothing — a base rebase, a conflict resolution, a divergence publish, a recovery push — so collapsing
the two makes the switch measure exactly the fresh work it exists to leave alone.

**The lease outlives the record that froze it.** The write that approves a candidate retires the generation, and the
head it froze goes with it — while the push that approval licenses has not run yet. So the head is carried on the
approval as `late_approved_lease`, and it is what every later push for that commit is pinned to: the retry after a
failed push, which skips the measurement because the commit is already approved, and the ordinary implementing
publication a settled `single` verdict hands the candidate back to. Without it both would read the pull request's
CURRENT head and adopt it as the lease, force-overwriting whoever pushed in between with work measured against the
head it used to be on. It is dropped by the same write that drops the approval — the push that lands, an approval
superseded, or a hold that routes the issue to adjudication instead.

**A settled `single` verdict publishes, then continues at the stage it came from.** The checkout is re-proved first
— provably clean, and standing on the accepted commit — because an adjudication is a human reading a diff over hours
with the worktree writable the whole time, and a verdict settled from a recorded answer has no run behind it for the
read-only proof to run against. Naming the accepted id would put the right commit on the remote either way, and that
is the danger: every stage past this one works from the checkout, so one left on an unmeasured descendant reaches
review, a squash, and a merge with nobody having read it.

It is proved **again** on the road out, and on both roads: a push is a request, and the worktree stays writable
across it, across the pull-request read behind it, and across the whole stretch a retry finishing an interrupted
settlement has left it unwatched. So the reading that decides whether the accepted commit may be handed on is taken
last, after the push — including the leased no-op a pull request already carrying the commit still makes. Failed,
the publication stands and the handoff stops: the branch has the accepted commit either way, the generation stays
live, the label stays on the adjudication (`late_pr_unreconciled`), and a tick taken once the worktree is back on
that commit finds the pull request already standing on it and finishes from there — nothing sent a second time,
no agent re-run.

The push belongs to the
settlement because the settlement is the last tick holding the evidence: the verdict was taken against one pull
request standing on one head, the reconciliation a moment earlier proved both are still what they were, and the
retirement behind it takes the record that said so away. So the branch is put where the verdict said it may go —
named against the accepted commit and leased against the frozen head — and only then is the label handed on, to
`late_source_stage` rather than to `workflow:implementing`. That stage is the only owner of the completion the
candidate still owes (the docs watermark and its `in_review` handoff, a conflict round, another reviewer look), and
two of the five have no publication seam a resumed tick would even reach. A push that does not land parks with the
label still on the adjudication: the exemption and the approval are already durable, so the retry asks for the same
commit against the same head.

The window between that push and the label is the one the record alone cannot answer, and it has its own
recognition. A tick that dies in it comes back to a live generation whose pull request is standing on the **accepted
candidate** rather than on the frozen head — which is this settlement's own push, not somebody else's movement. Read
as movement it would refuse the very publication this verdict made, forever, and park the issue
`late_pr_unreconciled`; recognized, the retry finishes the label and the retirement it never reached, records no debt
for a push that already happened, and pushes nothing a second time.

What qualifies that head is a **durable record of the push**, not the commit on its own: `late_approved_sha`, written
with the exemption in the write immediately ahead of the push, or `implementing_published_sha` read with
`implementing_published_lease`, the pair the push itself writes in the same write that drops the approval. One of the
two is on the comment for every crash past that write.
On a **fresh** pass neither is, and nothing of this workflow's has touched the remote yet — so a pull request that
has left the frozen head for the accepted candidate got there because something else put it there, an agent that
pushed its own commit being the plain case, and it refuses with every other moved head. Taking the commit alone as
proof would hand a stage a publication nobody proved and release a candidate the adjudication never pushed. The
receipt is read with its head for the same reason it is at the gate: it is never cleared, so an accepted candidate
published in an earlier round is one it goes on naming, and a pull request rewound onto that commit would otherwise
read as this settlement's push having landed. The head it replaced has to be the head this verdict was measured over.

**A settled `single` verdict proves its publication before it hands the candidate back.** A pre-publication verdict
searches for the pull request its commit is on and drops a recorded pointer that turns out settled, because losing it
costs nothing: the publication opens the pull request the work needs. A post-publication verdict knows which pull
request the reading was about, so it checks rather than searches — the pull request must still be open and still be
standing on the head the entry froze — and a check that fails **parks** (`late_pr_unreconciled`) instead of dropping
what it could not confirm. Dropping the number there would push onto a branch whose pull request a human settled and
open a second one for a change that was adjudicated against the first; publishing over a head that moved would
publish on a reading the branch has already overtaken.

The entry is what a call taken past publication has and one taken before it does not, and all three of its facts are
frozen before any effect because a later tick could re-derive none of them: the **stage** the gate is taking the issue
out of (gone the moment `workflow:decomposing` replaces it), the **pull request** the work already has (which is what
the cycle-marked hold then goes on, recorded under `late_plan_pr_number` beside its own head and the description it
displaced), and the **head** that pull request is standing on (which the next push to the branch moves). They are
written as `late_post_publication`, `late_source_stage`, `late_published_pr_number`, and `late_published_sha` beside
the frozen pair, so a record on the pinned comment says which side of publication it
was entered on and an analysis groups on the field rather than on its absence
([`labels-and-state.md`](labels-and-state.md#late-generation-state)). The stage is checked against the five that
publish onto a pull request the remote already carries rather than against "has an edge to `workflow:decomposing`":
`workflow:ready`, `workflow:blocked`, and `workflow:umbrella` each own that edge for reasons of their own and have no
pull request behind them, and `workflow:implementing`'s own push is what *opens* the pull request — so a group frozen
from one of them would send a later reconciliation to measure and push a candidate no post-publication stage ever
committed. The record refuses to be entered on such a stage, and one hand-edited onto the comment reads back as no
publication context at all.

- **At or below the ceiling** → the ordinary push onto the branch, unchanged. The generation is dropped ahead of it,
  and the `late_approved_sha` the retirement recorded is spent by the push that pays it — a debt left standing would
  freeze this branch out of the pre-tick base refresh with nothing coming back to drop it.
- **Unmeasured but published** → the same debt, for the same window. A candidate an adjudication exempted, or one a
  fresh commit superseded with the switch off, froze no generation of its own, so between the gate letting it through
  and the push that carries it there is committed work on the branch and nothing on the issue naming it. So
  `late_approved_sha`, its lease, and `late_spends` go down before the push, and the reconciliation ahead of every
  handler pays them: without that a tick dying in the window comes back to an issue that has published nothing,
  resumes a developer over the head the pull request already carries, and hands the gate a candidate whose two
  readings of that publication no longer agree. Recorded only where the push will MOVE the publication — one that
  finds the pull request already standing on the commit has nothing to receive, and a debt written there would be
  paid by a republication closing a round the tick that really published it already closed — and never over a debt
  the issue already carries for that commit, whose lease was frozen by the tick that granted it. The head it is
  recorded against is the one the push is pinned to, which is the entry's where there is one and the CALLER's where
  the switch kept the candidate out of the gate: nothing froze a publication there, but the push still moves one, so
  the window is the same and `DECOMPOSE=off` decides the measurement rather than the account of what a push put where.
- **Strictly past it** → nothing is pushed. The pull request stays on the head it was standing on, the measurement and
  the entry are made durable, a notice naming the pull request and that head goes on the issue, and the label moves to
  `workflow:decomposing` — from whichever of `workflow:validating` / `in_review` / `workflow:fixing` /
  `workflow:resolving_conflict` the fix loop was reached under, which is why each owns that edge.
- **Seven refusals**, each a push the gate would otherwise wave through on evidence nobody took, or a stage run over
  a candidate nobody read. A frozen pair whose **checkout is not on this host** stops the tick outright rather than
  letting the stage carry on: the commit is on a host this one is not, so the refusal owes a human — announced once,
  since a checkout that stays gone must not put a fresh notice on the thread every poll. The next five are a tree
  that is not
  *provably* clean (a `git status` that established nothing names no paths, which is what a clean tree names too — a
  dirty one still parks naming its paths one step earlier), a pull request nothing could read, one that is closed or
  merged (nowhere for the push to land), a head the caller named that is not a whole object id or that disagrees with
  the head this gate reads (the two are readings of one fact, so a disagreement is somebody else's push landing
  mid-tick), and a head that has moved off what a live record froze (the frozen pair no longer says what the pull
  request would come to; the record is left naming the head it froze rather than re-entered over the one that
  landed). The disagreement has one carve-out and it takes a **durable record of a push**, never a matching commit: a
  tip named by `late_approved_sha`, by a live generation's candidate, or by `implementing_published_sha` read
  *together with* `implementing_published_lease` — the head that receipt replaced — is this issue's own push having
  landed and is finished rather than refused. The caller's candidate on its own is not evidence: on a fresh attempt
  no push of this workflow's has run, so a tip that merely happens to BE that commit says an agent pushed it, and
  forgiven there the gate would measure and route the very candidate it is holding back. Nor is the receipt on its
  own, which is never cleared and would read a pull request rewound onto a commit published rounds ago as this tick's
  push arriving — and where the checkout is standing on that same commit, every local fact agrees and none of them is
  about this round. The head the push was PINNED to is what dates the receipt, and a rewind cannot supply the one a
  caller froze. Each parks `late_measurement_failed` with nothing
  pushed and no label moved, and the typed failure reaches both sinks under the stage the reading was taken in. The
  seventh is an approval whose **lease cannot be read** — absent, or not a whole object id. The lease is the whole of
  what keeps the push it licenses off a pull request somebody moved, and the one fallback available here is the head
  read NOW, which is exactly the move it exists to catch. So it parks with the rest rather than pinning to the
  present.
- **A reading the gate did take and could not finish is answered one step earlier, and on two of its steps a human is
  not asked at all.** Those seven refuse a record or an entry; this is the diff itself failing, and it goes through
  the same `late_parks` owner the implementing seam's readings do, so what a thread, a stream, and a base refresh see
  here is what they see there: the reason is `late_measurement_failed`, the record is one `late_failure` carrying
  `measurement_failed` with the `MeasurementFailure` step and the line that step wrote beside it, and a notice is an
  ordinary comment carrying the same hidden `<!--orchestrator-comment-->` marker every park's does — so a tick held
  silently posts nothing, leaves no marker, and moves no comment watermark, while an announced one is read back as
  this orchestrator's own exactly as every other notice is. `base_unreadable` and `base_absent` name the TRANSPORT
  between this host and the base rather than the work, and clear themselves, so the first three consecutive misses on
  one pair write the pair and the incremented `late_measurement_miss_count`, emit that record, log at WARNING and
  stop — leaving no
  `awaiting_human`, no `park_reason`, no comment and no step on the pinned record — and only the fourth parks and
  mentions a human, recording the step that mention names in the same write as the count. Every other member parks on
  its first miss, because a candidate this host does not hold or a diff nothing here can pin answers a second reading
  as it answered the first.
- **A park here bounds the mentions rather than the readings.** The frozen-pair reading below runs ahead of every
  handler on all five of these stages and is not gated on the park, so a parked pair is re-measured once a poll — the
  recorded base asked for as that exact object where one was named, and the remote asked again only where the failure
  left none — and a transport that comes back settles the park with nothing said on the thread. What that costs the
  issue is bounded to one sentence per thing there is to say: a reading stopping at the step `late_measurement_failure`
  already names repeats a sentence the human cannot answer any faster, so the tick is held silently — the typed
  `late_failure` still reaching both sinks, since those polls exist nowhere else, and a base id the remote finally
  names still recorded, since it is what the next retry asks for — while one stopping at a *different* member is a
  different next move nothing else would report, so it is announced once and takes that field's place. No miss is
  counted for either: the bound is spent. The refusals on this page that announce once are keyed on the standing park
  and nothing else — a missing checkout, a reading stranded on another stage, and a record nothing can parse are each
  a wall this process cannot walk back from, so any tick finding the reason already there is held. Only the reading's
  own guard asks more, because a reading is the one of them that can come back: the latch as well as the reason, the
  pair the park was taken over, and the step its notice named. A base that IS reached puts the count back to zero and
  leaves the member alone, since reaching the base is not the last step a reading can stop at; the verdict a reading
  that HAPPENED settles clears both, and retires the park with them.
- **A hold closes the caller's own bookkeeping.** The gate holding a candidate is not a park — the commit is on the
  branch and a `single` verdict publishes it from there — so what the caller's tick was in the middle of is finished
  even though its tail never ran, and no later tick of that stage can do it: a settled adjudication publishes before
  handing the issue back, so the resumed stage finds nothing left to push. Each caller therefore says up front what
  its hold owes, as pinned fields written inside the routed hold's own durable write, *ahead* of the relabel; applied
  afterwards they would be lost to any crash in exactly the window the relabel opens. The fix loop spends the reviewer
  round the rejected head superseded. The docs pass leaves `docs_settled_sha`, the head it produced, because the pass
  itself is over and only the `in_review` handoff is still owed. A conflict resolution leaves
  `conflict_settled_outcome` / `conflict_settled_sha`, because the resumed tick reads a published resolution as a
  branch already standing on its base — the no-op flip, which emits `base_up_to_date`, resolves nothing, and stamps no
  `last_conflict_resolved_at`. Each receipt is read back only when the branch is in sync with its remote AND standing
  on the commit the receipt names, since a verdict that parked or a label a human moved leaves the same receipt over
  a commit still on disk; ahead of the remote it stands and the ordinary recovered-commit road carries the commit
  back through the gate. In sync is not the same claim as CARRYING it — a replacement host rebuilds the checkout from
  a pull request that has moved on and gets a branch level with its remote and standing on somebody else's head — so
  the head is proved against the checkout rather than inferred from the counters, and that proof carries the remote
  with it: the caller fetched the branch before counting and refuses a checkout behind it. A receipt that is not a
  whole object id, or a head this host cannot peel, leaves the receipt exactly where it is for a tick that can prove
  it. Spending is asked
  of the measurement rather than of the label, because a reading nobody could take also stops the tick with a
  generation on the pinned comment — and that one IS a park, with the developer's work still pending and nothing of
  its caller's spent.
- **What a hold owes is durable, because the tick that owes it may not be.** The freeze is durable and the count that
  follows it is not, so the same crash the reconciliation exists for lands between them — and that tick has no run
  behind it to re-derive a reviewer round, a cleared fix batch, or a stage tail from. So `late_spends` goes down in
  the same write as the pair, as `[[field, value], ...]`, and the reading ahead of the next handler restores it
  before it re-enters the gate. Written only while the pair still awaits its count, which is exactly the window it
  pays for: a record carrying a number was answered by a routed hold that spent this on the way past, and rewriting
  it there would leave a spent claim for a later reader to apply twice. It sits inside the generation's own key
  group, with the one exception the approval makes: the write that approves a small candidate retires the pair
  before the push it licenses runs, and puts these back inside it, so a push that misses leaves the next tick both
  the commit to publish and what publishing it closes. Restored, an oversized retry routes having closed exactly
  what its caller would have; a landed push closes them in the write that carries its **receipt**.
- **Every landed push closes what its route owed, in the receipt's own write.** Not only the reconciliation's. Past
  that write the approval is gone and so is the generation it was granted under, while the caller still has a relabel
  and a write of its own to make — so a process dying in that window would come back to a published commit, a label
  already moved on, and a round frozen at what the tick before the push had, with nothing left on the comment saying
  one was owed. The routes hand in the value they computed BEFORE the push and re-apply that same frozen pair once
  the call returns, which is a no-op where the gate already wrote it and the count where a push nothing could NAME
  never reached that write. Re-reading a counter there instead would count one round twice, which is why the value is
  read once per route and carried. And the write is skipped where the pinned comment already says all of it — a
  retry over a publication the remote is already standing on, whose round the tick that landed it closed in this
  very write. A push that MOVED the publication is not that retry, and there the receipt alone proves nothing: it is
  never cleared, so a pull request somebody rewound and this tick pushes BACK to a commit published rounds ago
  arrives with the receipt naming it and no debt beside it while the round behind this push is still uncounted and
  its fix batch still pending — and the only write carrying any of that would be the caller's own a tick's work
  later. So a moved publication settles whenever a pair the route owes is not already the value on the comment, and
  a push that had nothing to send does not: the routes that read their owed value off the counter would compute a
  higher one here and count the same round twice.
- **`DECOMPOSE=off`** is asked ahead of all of it, so an install running that way neither reads the pull request nor
  parks over one. As at the implementing seam the switch decides only what ENTERS the gate: a record naming the commit
  in hand, or a commit an approval owes a push for, is measured either way — while a record naming some other
  candidate is one this commit supersedes, so it is retired and the fresh commit publishes unmeasured. The squash asks
  it for itself, because `reconciling` cannot answer it there — that seam sets the flag to say no developer ran, and
  the gate reads it as *answering a reading the gate recorded*, which a squash never is: the commit it publishes is
  one it makes itself. Nor does the switch reach the naming. A caller that named its candidate is still proved against
  the checkout, because the proof is local and what it buys is the one comparison the naming exists for — the switch
  keeps candidates out of the measurement, not out of a push that knows which commit it is sending.

**The freeze is a resumable step, not a window.** The pair goes down with `late_phase=measuring` before the diff is
counted, so a tick that dies in between leaves a record naming both commits with no number on it — and nothing on the
stage it was entered on would go back for that by itself. So the dispatcher asks for it, in `_pinned_state_refuses`
beside its other late-domain guards and ahead of every handler: a generation carrying a whole publication group, no
count, and a `late_source_stage` equal to the label the issue is wearing — and one of the five that publish onto an
existing pull request, since a group naming any other state is no context to measure from — is measured *first*. Small
retires the record PUBLISHES it -- named against the commit that was measured and pinned to the head the pair froze --
and the handler runs behind that push; past the ceiling routes the issue to the adjudication and the handler does not
run; a refusal parks, durably, since nothing runs behind it to write the flags, and so does a push that was allowed
and did not land. The publication is the reconciliation's own because nothing goes back for it: the reading is settled
and the record is gone, so a handler run behind an unpublished candidate spawns a reviewer over a pull request that
never received it — and an approval past that finds one commit on the branch, squashes nothing, and hands an unpushed
head to the docs pass, which reads it as recovered work and skips the pass it was relabelled for.

**An approval with no generation behind it is the same window one step on.** The write that approves a candidate
retires its generation in the same breath, deliberately and before the push, so a tick that dies past that write
leaves nothing for the frozen-pair reading to answer — only `late_approved_sha` naming a commit the pull request
never received. So the dispatcher pays it too, ahead of every handler, under the id the gate decided about and the
`late_approved_lease` it decided against: both live only on the approval by then. The **lease** is what says the
approval was taken on the published side at all, so an implementing-seam one — whose push opens the pull request and
reads the remote for itself — is left to the publication that owns it, and an issue under `workflow:decomposing` is
left to the settlement that is still reconciling it. It is paid only from a checkout still standing on that commit,
because an approval is a claim about ONE commit — and a checkout that is absent, unreadable, or standing elsewhere
**parks** rather than standing down. The debt says a commit the pull request does not carry was measured and allowed
to join it, so a handler run behind any of those works from a publication the approved work is not on: the reviewer
votes on a head nobody adjudicated, the merge gate offers a human that head, and the docs pass commits on top of it.
Announced once, since an operator has to put the checkout back before anything changes.

A branch some owner deliberately moved OFF the approved commit never reaches that refusal, because an approval whose
commit was abandoned is superseded and the owner doing the abandoning drops it: the auto rebase's reset — which puts
the branch back on the pre-rebase SHA when its own push is refused, leaving the approved commit only in the reflog —
clears the approval with the recovery anchor.

The same answer is asked at the no-feedback bounce, which reaches a missing checkout on its own: a publication that
finds no worktree has always simply not published, and with a pair frozen and never counted that is not enough. No
developer ran on that tick, so it is taken as a **reconciliation** — a head that is not the recorded candidate is a
checkout something moved rather than a run's output, and it is refused rather than measured.

**A record that CLAIMS a reading it cannot produce stops the tick too, ahead of both.** Every field in this domain
is read fail-closed, and for the readings the gate itself takes that is the whole answer: a value it cannot use is a
value it does not have. Ahead of the HANDLER it is only half of one — a publication group missing its stage, its
pull request, or its head parses as no group, and an approval missing the lease it is spent with parses as no
approval, so both of the questions above answer "nothing owed" and the stage runs. So the raw fields are read first,
on the five stages that publish onto a pull request the remote already carries: a marked group that cannot name all
three, or an approval that cannot produce its pair, parks `late_measurement_failed` with nothing pushed and nothing
discarded. None of the pieces is recoverable from anywhere else, so the refusal owes a human — announced once, since
a fresh notice every poll is a mention nobody can answer any faster. Those five are named off the transition graph's
own set rather than derived from it: `workflow:implementing` has an edge to the adjudication too and is **not** one
of them, because its approval carries no pull-request head by design — its push is the one that opens the pull
request — so a crash between the two leaves exactly the shape this would otherwise call damaged and park instead of
finishing. `workflow:decomposing` is excluded because the settlement there holds evidence this would be reading half
of.

**A record read off its own stage stops the tick.** The reading was taken under one publication and one stage, and
both are terms of it, so a pair frozen on `fixing` and read while the issue wears `workflow:validating` may not
simply be re-entered here — it would be measured against a publication it was never taken on. Waving the handler
through instead is worse: the reading is unresolved and the commit it named is unpushed, so `validating` would hand a
reviewer a head the pull request never received, and the roads that publish would push a candidate nobody read. The
label was moved by something outside the gate and only a human can say whether it goes back or the record is dropped,
so the tick stops with nothing pushed, nothing discarded, and one notice on the thread rather than one per poll.

**A settled split's retained publication group is not an outstanding reading.** A candidate the adjudication turned
into children owes no count, and the record says so by carrying none: the split's retirement drops the measurement
on purpose — one still answering "oversized" pins `workflow:decomposing` and would put the umbrella label back on
every tick — and keeps the publication group, because the umbrella re-asks it in front of every child it releases
and every branch it deletes. Read as a pair somebody froze, that record is the shape above with the label already
moved: the group names the stage the gate was entered from and the issue is on `workflow:umbrella` by design, so the
stranded-reading refusal would hold every tick in front of the walk that releases the children — the one way a split
can leave its own children permanently unreleased. So the reading is asked of the record's own settlement first
(`LateGeneration.split_has_settled`): a `late_phase` of `splitting`, `superseding`, or `cleaning_up`, or a non-empty
`late_split_children` register, and the tick goes to the label's own handler. `snapshotting` is not one of them —
that boundary cuts the ref and creates no child, and a record standing there still carries the reading that sent it
to the adjudication. Neither is the measurement re-added to the settled record: `_adjudication_is_live` and the
`workflow:decomposing` relabel guard are keyed on it. A `late_measurement_failed` park an earlier tick left on such a
record is retired in the same pass, since nothing about it is a human's to answer and the reason is what holds the
branch out of the pre-tick base refresh.

**A push that landed leaves a receipt where it landed.** `implementing_published_sha` is written in the same durable
write that drops the approval, at the moment the push returns, on every seam this gate stands in front of. The window
after it is a whole tick's worth of relabels and comments, any of which can fail — and a tick coming back into it
finds a live branch, a pull request already carrying the work, and a label that still says the stage never finished.
Without the receipt the candidate is measured again against a base that has moved, and an answer past the ceiling
would route a pull request that ALREADY has the work to the adjudication, which is the one outcome this gate exists
to prevent. Recognized, the commit is neither re-read nor re-pushed and the tick finishes what it was in the middle
of.

**But the receipt is a local note, and the REMOTE is what it is evidence about.** So it is only honoured while the
publication it names is still standing: both the reading it skips and the push it skips ask that the head this tick
froze is that same commit. A receipt naming `C` beside a pull request somebody has since moved to `F` records a
publication that is over — read as one still standing, the tick would push nothing and hand a reviewer a head the
pull request does not have. Refused, `C` goes back through the ordinary road, measured against the base as it is now
and pushed leased against what was frozen.

**The head the pull request is standing on settles a debt no write got to.** The receipt and the approval it replaces
are one write, and a process can die on it: the branch is on the remote and the pinned comment still says the commit
is owed, leased to a head the remote has moved off. Nothing measures that commit again — the approval is exactly what
keeps it out of the gate — so nothing else would ever drop it, and `late_approved_sha` would freeze this branch out
of the pre-tick base refresh for the rest of the issue's life. What answers it is the entry this call already froze:
a pull request standing ON the candidate says the push it licenses was made, whatever any record says. So there is
nothing to push, the debt is settled, the receipt is written, and the tick carries on. Asked ahead of the lease
requirement, because a push nobody is making needs no head to pin — a lease that died with the write that should
have spent it must not park an issue for a publication that already happened.

**The checkout is proved again on the far side of the push.** The pre-push proof is a fact about a moment that has
passed by the time git returns: the push is a request, the worktree is writable while it runs, and a descendant an
agent or a cleanup left — or an unstaged edit beside it — is enough. What went out is the commit that was named, so
the branch and its pull request are right; what is wrong is the CHECKOUT, and the checkout is what every stage behind
this gate reads. A reviewer treats a head ahead of the pushed branch as unpushed work, the squash rewrites what is on
it, the docs pass commits on top. So the publication stands and the HANDOFF stops: the caller is told the tick is
finished and relabels nothing, announces nothing, and spends no round, while the issue parks `late_candidate_moved`.
It is the same pair of questions the initial publication asks past its own push, borrowed from that owner rather than
worded twice.

**And it is asked AHEAD of the settlement, so its answer rides the receipt's own write.** What a failed proof records
is a whole approval, both halves the commit that just landed — that being the head the pull request stands on now —
and it goes down with the receipt rather than one write behind it. Settled the other way round, a process dying
between the two comes back to a published branch, a paid debt, and nothing on the record owing the checkout a proof:
the stage below reads a dirty worktree as no stranded work and relabels to `workflow:validating`, handing a reviewer
a checkout nobody read. The window that remains is the one BEFORE that write, and it is recoverable rather than
silent — every push that moves its publication records the debt for it beforehand, so a crash there leaves an
approval the reconciliation ahead of the next handler pays as a leased no-op and then re-proves.

## `_handle_validating` (label `workflow:validating`)
- **Trigger**: each tick while label is `workflow:validating`. Set by `_handle_implementing` after `_on_commits` opens
  the PR, by `_handle_documenting`'s drift unwind, and by `_handle_fixing` / `_handle_in_review` /
  `_handle_resolving_conflict` on their pushed exits.
- **Input**: PR #, branch, `dev_agent` / `dev_session_id`, `review_round`.
- **Internal flow**:
  0. **External-merge / closed-issue short-circuit** (same chain as implementing / documenting). The reviewer is not
     spawned on either short-circuit.
  1. A squash this issue began and did not finish (`late_collapse_*` on the pinned comment) is answered here,
     ahead of every route that could point an agent at the branch — the drift resume, the awaiting-human path, and
     the reviewer spawn (`_recovers_a_recorded_collapse`) — over the same tail the approval road runs. Asked only
     from that road it would be asked on no tick whose reviewer times out, crashes, or votes `CHANGES_REQUESTED`:
     an already-landed collapse would never get its notice, its watermarks, or its relabel, a record nothing can
     read would reach `workflow:fixing` without the park it owes, and a body edit would resume the dev on a branch
     standing on a commit nobody accounted for. It owns the park it takes, too. Its own refusals park under a
     durable `park_reason="squash_failed"`, and it retries them on every tick without saying anything again: what
     the notice asks for — the branch reconciled, or the pinned comment repaired — is proved by the recovery
     getting further, and the park's own writer stays quiet while that reason stands. A park the **size gate**
     worded behind it is held rather than re-entered, since the gate posts a fresh notice for every reading it
     cannot take; the human's reply then clears that park and is spent on the recovery rather than on a dev resumed
     over a branch mid-rewrite. A recovery that finishes clears the park it found, so nothing carries an
     `awaiting_human` into `documenting`. An issue with nothing recorded costs one lookup on the pinned comment;
     one carrying only the `late_collapse_handoff_sha` a finished handoff left moves the label that handoff never
     got to move (and drops the record behind it), or drops it unspent where the pull request has since moved off
     the commit it names.
  2. Awaiting-human path: resume on the dev's locked spec; on a successful pushed fix, bump `review_round` and stay on
     `workflow:validating`. A transient park (`_VALIDATING_TRANSIENT_PARK_REASONS`) with NO new comment goes to
     `_try_recover_validating_transient_park` instead, which retries silently and, on `cleared` / `pushed`, posts the
     **Recovery follow-up** described below before clearing the park. Its two git-touching retries — the deferred push
     and the commit a timeout killed the disposition before it saw — publish through the same
     [size gate](#the-size-gate-on-a-published-pull-request-every-push-onto-an-open-pr) the shared dev-fix publication
     passes, which is why it has a fourth answer: `held`, meaning the gate took the candidate and the tick is over.
     The caller then posts no follow-up, clears no park, and moves no label — the gate has already parked on a reading
     nobody could take, or handed the issue to `workflow:decomposing`, and written its own state, so a follow-up would
     announce a recovery that did not happen and a relabel would move the issue off the state the gate just set.
     Exception: on a `review_cap` park the human reply does NOT wake the dev — the operator must post
     `/orchestrator add-review-rounds N` on its own line (honored only from an allowlisted author when
     `ALLOWED_ISSUE_AUTHORS` is set — an outsider's command is filtered out before the parse), which resets
     `review_round` to `max(0, MAX_REVIEW_ROUNDS - N)`, clears the park, and falls through to spawn the reviewer this
     same tick. Values at or above the configured maximum grant one full review budget rather than extending the
     budget past it. A second exception: a bare `/orchestrator continue` on a session-failure dev park (`agent_silent` /
     `agent_timeout`) is intercepted (`_continue_command_action`) and retries the dev on the neutral
     `_CONTINUE_RETRY_PROMPT` — NOT the literal command, which the dev has no context for — while
     `_handle_dev_fix_result` still publishes any stranded commit; a bare continue on a park needing a real answer
     refuses (`_refuse_parked_continue`) and stays parked. A command carrying real guidance, or a normal reply,
     resumes the dev on that text as before. (Shared with `implementing` / `documenting` / `resolving_conflict`; see
     the drift-detection section for the bare-continue hash exclusion.)
  3. If `review_round >= MAX_REVIEW_ROUNDS` (default 3), park (`review_cap`). The park comment surfaces the
     `/orchestrator add-review-rounds N` escape hatch.
  4. Otherwise persist `config.REVIEW_AGENT_SPEC` to `review_agent` (traceability only — the reviewer is spawned fresh
     each round with no resume), then run the reviewer with the read-only prompt (must end with `VERDICT: APPROVED` or
     `VERDICT: CHANGES_REQUESTED`). A mid-run `paused` / `backlog` re-check (`_paused_during_agent_run`) right after the
     reviewer returns short-circuits BEFORE the usage fold, session record, verdict parse, verify gate, squash, or
     relabel, so the next tick re-spawns a fresh reviewer from durable state.
  5. Parse the last `VERDICT:` marker (`_parse_review_verdict`):
     - **approved** → in order: (1) run the local verify gate (`_run_verify_commands(wt, config.VERIFY_COMMANDS,
       config.VERIFY_TIMEOUT)`); a non-ok result parks via `_park_verify_failure` with a typed `park_reason`
       (`verify_failed` / `verify_timeout` / `verify_dirty` / `verify_head_changed`) and the approval / squash /
       handoff do NOT fire (see
       [`configuration.md#local-verification-gate`](../configuration.md#local-verification-gate)); (2) post
       `:white_check_mark: codex review approved.`; (3) when `SQUASH_ON_APPROVAL` is on (default), call
       `_squash_and_force_push` (subject reuses the first commit when it carries a reusable `<prefix>:` form —
       Conventional **or** repo-local such as `event:`/`career:` — otherwise `<inferred-prefix>: <issue title>`, where
       the prefix is inferred from recent base-branch history via `_infer_subject_prefix` and falls back to
       `fix:`/`feat:` only when no repo-local prefix dominates; pushed with `--force-with-lease`). That call answers
       a squash an earlier tick did not finish first, from the record that squash wrote before it ran, so a
       collapsed-but-unpublished branch is resumed rather than reported as having nothing to squash — and it does
       so whatever `SQUASH_ON_APPROVAL` says, since the switch decides whether a NEW collapse is made and one
       already on the branch has to be finished either way. A branch the recovery hands BACK after dropping a
       record is entered on the publication before it is handed on, since no rewrite follows that drop to read the
       pull request — and that reading is taken whatever `DECOMPOSE` says too, because with no push behind it
       there is no lease to answer a remote somebody moved. The checkout is proved again once the reading comes
       back, since the read is a request and a commit landing in that window is work no reviewer saw on a branch
       this road reports as standing where it planned. On squash / force-push failure, park awaiting human
       under a durable
       `park_reason="squash_failed"` and stay on `workflow:validating`. The notice names which of four places
       that left the branch: the approved
       commits are still on it — the ordinary failure, which aborted before anything destructive or restored what it
       rewound, including a record whose reset never ran — or a collapse this tick could not finish is standing
       there instead, with the approved history reachable only from the head the record names, or the branch grew
       PAST that head and the approved commits are under the work on top of them, or none of it has been shown.
       The record, the checkout's own head, the recorded head as an OBJECT, and the ancestry between the two are
       all read, since an outstanding record is not proof the rewrite happened, a recorded head this host does not
       hold is a reflog entry nobody could look in, and one still reachable from HEAD was never rewritten at all.
       (4) On success,
       if `squashed_count > 1` post `:package: squashed N commits to 1`, seed the in_review watermarks (inside the
       `gh.get_pr()` try so a snapshot failure leaves them untouched), then end the collapse record and persist —
       leaving `late_collapse_handoff_sha` in its place — and only then relabel to `workflow:documenting`, dropping
       that record in a write of its own behind the label. A relabel that does not land is not raised past the
       handoff: everything it owed is durable, and step 1 moves the label on the next tick instead of a second
       reviewer being run over a branch already published.
     - **unknown** (no marker) → park, split by whose failure it was
       (`_reviewer_no_verdict_park`). An empty last message with a non-zero exit (a crash), or a message opening with
       a transient provider refusal (`is_transient_provider_failure` — `API Error: 529 Overloaded` and its 5xx
       siblings), is tagged `reviewer_failed` so the next tick's transient-recovery branch re-spawns the reviewer;
       real reviewer text that merely omitted the marker stays `reviewer_no_verdict` for human adjudication.
     - **changes_requested** → post the feedback to the PR, then flip the label to `workflow:fixing` BEFORE spawning
       the dev so the active job is observably "fixing reviewer-requested changes". Resume the dev with the fix
       prompt; on a new commit + clean tree the fix passes the
       [size gate on a published pull request](#the-size-gate-on-a-published-pull-request-every-push-onto-an-open-pr)
       before it is pushed, and on a push, bump `review_round`, and flip back to
       `workflow:validating`. A
       no-commit run that finds a stranded unpushed fix on a clean HEAD (see `_handle_fixing` step 8) publishes it the
       same way. The dev spawn records `stage="fixing"` for analytics. On any park (timeout, no-commit, dirty,
       push-fail) the label STAYS `workflow:fixing` with `awaiting_human=True` and `_handle_fixing` owns the
       awaiting-human cycle thereafter. An `interrupted` dev resume is ignored: the handler returns WITHOUT writing
       the post-spawn state (no resume-budget charge, no watermark, no park), so the pre-spawn `workflow:fixing` flip
       stands and the next tick re-runs the cycle; any commit the killed run left is republished later via the
       stranded-fix tail, not this run.
  5. `paused` / `backlog` applied mid-run → each of the three dev resumes (the drift resume, the awaiting-human
     resume, and the CHANGES_REQUESTED fix resume) re-checks a FRESHLY fetched issue via `_paused_during_agent_run`.
     On a hit the handler returns WITHOUT running its result handler (`_post_user_content_change_result` /
     `_handle_dev_fix_result`), so no comment posts, no push, no `review_round` bump, no relabel, and no pinned-state
     write. The committed work stays on the branch; the CHANGES_REQUESTED path leaves the pre-spawn `workflow:fixing`
     flip standing and `_handle_fixing` owns the issue once the label is removed — its no-feedback exit (step 6 there)
     is what publishes the discarded run's commit, since the reviewer comment that started the round is filtered out
     of every later rescan.
- **Output**: label moved to `workflow:documenting` (approval after verify + squash) OR `workflow:fixing`
  (CHANGES_REQUESTED) OR `workflow:decomposing` (the size gate held a fix or a transient-park recovery push) OR no
  label change with `review_round` bumped (awaiting-human resume, drift, transient-park recovery push) OR a HITL park.

## `_handle_in_review` (label `in_review`)
- **Trigger**: each tick while label is `in_review`. Set by `_handle_documenting` on the final-docs hop. Also runs on
  closed-`in_review` issues for external-merge finalization.
- **Input**: pinned `pr_number`, `branch`, `dev_agent` / `dev_session_id`, and three watermarks (`pr_last_comment_id`,
  `pr_last_review_comment_id`, `pr_last_review_summary_id`) — one per id namespace GitHub uses for PR feedback. Mixing
  any two namespaces under one watermark would silently drop or replay one side.
- **Internal flow**:
  1. If `pr_number` is missing → park awaiting human.
  2. Read the PR via `gh.get_pr` and delegate the terminal arcs to the shared `_drain_review_pr_terminals` helper (also
     called by `_handle_fixing` and `_handle_resolving_conflict`). The orchestrator never merges from here, so any
     `merged` state observed was produced externally. Branch on `gh.pr_state(pr)`:
     - `merged` → stamp `merged_at`, set label `done`, write pinned state, emit `pr_merged`
       (`merge_method="external"`), close the issue, `_cleanup_terminal_branch`.
     - `closed` → stamp `closed_without_merge_at`, set label `rejected`, emit `pr_closed_without_merge`, close,
       cleanup.
     - `open` BUT the issue was closed manually → set label `rejected` WITHOUT branch cleanup so the operator can
       salvage the still-open PR.
     - `open` with an open issue → fall through.
  3. **Fresh PR feedback (including any human CI-fix request) → route to `workflow:fixing`.** Read four sources
     independently, one per id namespace: issue thread, PR conversation (shares IssueComment id space), inline review
     comments, PR review summaries (filtered to non-empty `CHANGES_REQUESTED` / `COMMENTED`). If any source is newer
     than its watermark, record `pending_fix_at` + per-namespace `pending_fix_*_max_id` bookmarks (and the full
     `pending_fix_*_ids` batch lists) and flip to `workflow:fixing`. The handler does NOT honor
     `IN_REVIEW_DEBOUNCE_SECONDS` here or spawn the dev — `fixing` owns debouncing, the dev resume, and the DIRECT
     bounce back to `workflow:validating`. Watermarks are NOT advanced on this route so `fixing` can re-discover the
     triggering comments.
  4. **User-content drift → relabel back to `workflow:validating`.** Reached when no fresh PR-side ID surfaced a
     comment but `_detect_user_content_change` still reports a hash change (a title/body edit, or an edit to an
     existing issue-thread comment whose id is already below the watermark). Capture unread PR-conversation comments
     past `pr_last_comment_id` BEFORE posting the notice (the shared id space could otherwise leap past one). Resume
     the locked dev session with `_build_user_content_change_prompt` (quoting issue body + recent comments + the
     captured PR-conversation comments). Both successful outcomes — pushed fix AND `ACK: <reason>` no-commit reply —
     reset `review_round=0` and bounce directly back to `workflow:validating`. A no-commit response without the `ACK:`
     marker parks via `_on_question`. An `interrupted` resume short-circuits via `_ignore_if_interrupted` BEFORE
     `_post_user_content_change_result` and the watermark bump, returning WITHOUT writing pinned state so the drift
     stays unconsumed for the next process to retry. A mid-run `paused` / `backlog` (`pause_guard=True`)
     short-circuits the same way, right after the interrupted check.
  5. **Manual-merge HITL path** (only reached with no fresh PR feedback AND no drift):
     - `pr_is_mergeable` is `None` → try next tick.
     - `False` → park with `unmergeable`; HITL ping mentioning every `HITL_HANDLE`, bump watermarks past the park
       comment.
     - `True` → check `gh.pr_has_changes_requested(pr, head_sha=head_sha)` (a standing human CHANGES_REQUESTED on the
       current head vetoes the ping). The ping requires either `docs_checked_sha == pr.head.sha` with `docs_verdict` set
       OR `gh.pr_is_approved(pr, head_sha=pr.head.sha)` (a human/bot APPROVED review on the current head). When the
       gate passes, post a one-shot `:bell:` ping de-duplicated by `ready_ping_sha`. The ping is NOT a
       park: `awaiting_human` stays false so subsequent ticks still react to new comments / an external merge.
       Unlike park branches, the ready ping does NOT call `_bump_in_review_watermarks` (the bump reads
       `gh.latest_comment_id(issue)`, which could
       include a concurrent human comment).
  6. Every park inside this handler bumps the watermarks past the orchestrator's own park comment, so the next tick does
     not see it as fresh PR feedback.
- **Output**: label moved to `done` / `rejected` (terminal), OR `workflow:fixing` (fresh PR feedback), OR
  `workflow:validating` (drift; pushed fix OR ACK no-commit; both reset `review_round=0`), OR a HITL park
  (unmergeable, missing pr_number, drift-resume failure), OR a HITL ping (no relabel), OR a no-op tick.

**Recovery follow-up.** Both callers of `_try_recover_validating_transient_park` — the `workflow:validating`
awaiting-human branch and the `workflow:fixing` parked branch — post one short issue comment on a `cleared` /
`pushed` outcome, before the pinned write that clears the park, so the HITL mention that filed the park is not the
thread's last word after the system has healed itself. The wording is chosen by
`_recovery_followup_comment(gh, issue, state, park_reason, outcome)` from the (reason, outcome) pair: the failed push
retried, the timed-out run's commit pushed, the timed-out run having left nothing to publish, or the reviewer being
re-spawned. It carries no @mention (closing the loop must not notify a second time), and it is skipped entirely when
pinned state carries no `last_action_comment_id` — no mention was ever posted, so there is nothing to retire — or
when the pair has no wording. A `stuck` outcome posts nothing at all, so a still-failing retry stays silent poll
after poll.

Exactly one lands per park episode, and the receipt for that is the thread rather than pinned state. The post and
the write that clears the park are two operations, so a process that dies between them leaves GitHub holding a
comment no local record names — any receipt written beside the clear would die with it. So every follow-up carries
`_RECOVERY_FOLLOWUP_MARKER` (`<!--orchestrator-recovery-followup-->`), and `_episode_already_announced` looks for it
among the comments past `last_action_comment_id` before wording a new one. That watermark is the park's own mention
id, which scopes the search to this episode: a later park stamps a higher one, so an older follow-up sitting below it
cannot silence the next recovery. A forged marker costs its author the notification they would have been spared
anyway.

The late size gate's `late_owner_unreadable` park heals the same way and by the same rules, from its own owner
(`late_owner.py`) and under its own marker (`<!--orchestrator-late-owner-recovery-->`), so a follow-up from one
mode's episode cannot silence the other's. Two things differ. Its retry hangs off a durable
`late_owner_check_pending` on the generation rather than off the park, since the routes it has to survive skip the
park entirely; and its follow-up is posted *before* the write that clears the park rather than after, so the crash
window loses the write instead of the sentence — which the thread-marker check then makes free to repeat. A park
whose own notice GitHub refused (`late_park_notice` still owed) heals silently: it told nobody anything, so there is
no alarming last word to retire and a follow-up would be the first thing the episode said. What that park is and why
its retry re-reads rather than re-running anything is in
[`../workflow/roles.md`](../workflow/roles.md#the-owner-read-a-finished-run-has-to-pass).

The same failure window is why `_AwaitingValidation.build` drops the orchestrator's own comments — by recorded id
AND by `_ORCH_COMMENT_MARKER`, the pair `_rescan_fixing_feedback` already uses. Every awaiting-human decision helper
reads a non-empty batch as "a human replied", and a follow-up whose id-recording write never landed is still ours;
the marker is what says so when the id ledger cannot.

`_park_awaiting_human` posts on the issue (not the PR) so the HITL ping appears alongside the rest of orchestrator
state. The PR comment that triggers a route to `workflow:fixing` is the human signal; awaiting-human is reserved for
*unrecoverable* states (unmergeable / missing pr_number).

## `_handle_fixing` (label `workflow:fixing`)
- **Trigger**: each tick while label is `workflow:fixing`. Two routes set this label:
  - `_handle_in_review` when fresh PR feedback (any of the four surfaces, including a human CI-fix request) arrives —
    records `pending_fix_at` + per-namespace `pending_fix_*_max_id` bookmarks and the full `pending_fix_*_ids` batch
    lists.
  - `_handle_validating` on a `CHANGES_REQUESTED` verdict, flipped BEFORE the dev spawn. This route does NOT set
    `pending_fix_at`; it records `pending_fix_reviewer_comment_id` (the id of the reviewer-feedback PR comment) as its
    lone replay anchor. The dev runs inline and on a pushed fix validating flips the label back itself (clearing the
    anchor). Only the parked outcomes leave the fixing handler to own the awaiting-human cycle.

  Also runs on closed-`workflow:fixing` issues so an externally-merged PR finalizes to `done`.
- **Input**: pinned `pr_number`, `branch`, `dev_agent` / `dev_session_id`, `pending_fix_at` + per-namespace bookmarks
  (in_review route only), the three in_review watermarks (left behind so the rescan can re-discover the triggering
  feedback), `IN_REVIEW_DEBOUNCE_SECONDS`.
- **Internal flow**:
  1. PR-state terminals mirror `_handle_in_review` (shared `_drain_review_pr_terminals`). `_handle_fixing` catches its
     own `gh.get_pr` exceptions and hands `pr=None` to the helper, which is a no-op.
  2. Closed issue with no resolvable PR → no-op.
  3. Open issue with no `pr_number` (manual relabel) → park (`missing_pr_number`).
  4. Rescan unread feedback from the three watermarks across all four surfaces. Orchestrator comments are filtered by
     recorded id AND the hidden `<!--orchestrator-comment-->` body marker.
  5. If `awaiting_human`, first handle the **`/orchestrator continue` operator command** (`_handle_continue_command`).
     It is matched as an EXACT LINE (`^\s*/orchestrator continue\s*$`), so a comment carrying the command line AND real
     guidance still counts as the command; the command is handled on BOTH routes so a session-limit / session-failure
     park (`agent_silent` / `agent_timeout`) is never resumed on the bare command text. Two dev final messages are
     parked `agent_silent` by `_on_question` rather than as a real `park_reason=None` question, because neither is the
     agent's own words: a recognized Claude session/usage-limit notice (`_is_session_limit_message`), and a transient
     provider refusal such as `API Error: 529 Overloaded` (`agents/sessions.py`'s
     `is_transient_provider_failure`, which prefers the terminal result event's `is_error` flag and otherwise requires
     a non-zero exit beside the prefix, so a successful answer that merely quotes the error stays an answer). A quota
     reset and a provider that came back are therefore retried here rather than refused as needing human guidance.
     The helper returns one of three
     actions: **replay** — an eligible session-failure park **with a reconstructable batch** (the in_review route's
     `pending_fix_*` bookmarks, or the validating route's `pending_fix_reviewer_comment_id` anchor): drop the poisoned
     dev session (`_drop_poisoned_dev_session` — so the retry re-grounds a fresh session on the committed branch), clear
     the park, and **replay the preserved feedback batch** (`_reconstruct_pending_fix_batch`) carrying the fresh
     feedback that says something (`_carried_fresh_feedback`) — any guidance posted with or beside the command,
     verbatim — but NEVER the bare command itself: a replay renders what it is handed as PR feedback to implement, so
     the command line would read as work to do. Dropping it strands nothing, because the resume tail advances the
     watermarks past all of the fresh feedback rather than past what it replayed. Resume the fresh dev on that batch,
     skipping the debounce; **refuse** — a content-free continue (every fresh comment is a bare
     command) on a park it cannot retry (an unsafe park needing real human guidance, both `park_reason=None`; or an
     eligible reason with **no reconstructable batch**, e.g. a validating-route park whose reviewer anchor was never
     recorded or has since been deleted): the command comment is consumed (watermark advanced past it so the refusal
     does not re-fire) and a note is posted, and the issue stays parked; **passthrough** — the command arrived alongside
     genuine guidance on a park with no replayable batch, so it falls through to the normal resume below and that
     guidance drives the dev.

     Otherwise, when the rescan finds nothing new, branch on `park_reason` AND the route discriminator `pending_fix_at`:
     - **Transient reason** (`push_failed` / `agent_timeout` / `reviewer_timeout` / `reviewer_failed` — the
       `_VALIDATING_TRANSIENT_PARK_REASONS` set) **and `pending_fix_at` unset (validating route)** → call
       `_try_recover_validating_transient_park`. On `cleared` or `pushed`, post the recovery follow-up (see the
       **Recovery follow-up** note above), clear park, clear `pending_fix_*`, flip back to `workflow:validating`
       (the helper bumps `review_round` on `pushed`). This closes the loop for `_handle_validating`'s
       CHANGES_REQUESTED route. On `stuck`, fall through to the worktree-drift check below. On `held` — the size gate
       took the candidate the retry was about — the tick stops outright: no follow-up, no clear, no drift reroute, and
       no relabel, because the gate has already parked the issue or moved it to `workflow:decomposing` and written its
       own state.
     - **Any other awaiting-human shape** (transient reason on the in_review route, non-transient reason like a real
       agent question, dirty-worktree park, or silent-crash park) → return silently and keep waiting for a human
       reply. We cannot distinguish "agent has a real question" from "agent reported nothing to change" by inspection
       (both surface through `_on_question` with `park_reason=None`), so auto-routing either would silently bypass the
       HITL contract.

     **Worktree-drift dead-lock breaker** (`_reconcile_parked_fixing`). Reached only from the
     stuck-validating-route-transient branch above: the self-recovery could not clear the condition, and the
     underlying cause may be a base advance that landed mid-park (the per-tick base sync deliberately stands down on
     every `awaiting_human` park — `_sync_pr_worktree_to_base` returns at its `awaiting_human` gate — so nobody else
     will sync this worktree). On a clean worktree the breaker routes to `workflow:resolving_conflict` — seeding
     `conflict_round` when absent, clearing the park, posting a PR notice, emitting `conflict_round`
     `action="entered"` (`stage="fixing"`) — in either of two shapes, both reconciled by the conflict handler, which
     owns rebasing AND publishing a PR branch:
       - **behind `<remote>/<base>`** (a local `rev-list HEAD..<remote>/<base>`) → needs a rebase;
       - **already on base but local HEAD ≠ the live `pr.head.sha`** (a rebase a prior run ran but never pushed) →
         needs a force-publish (see `_handle_resolving_conflict` below).

     The routing decision is cheap — no extra fetch, since `pr` was already fetched this tick. With no drift (the
     worktree is in sync with the PR head), or a dirty worktree, the park is left intact and the issue keeps
     awaiting a human. An operator who wants to freeze this reconciliation applies `paused`, which hard-skips the
     issue at dispatch so the breaker never runs. The `pending_fix_*` bookmarks and in_review watermarks are left
     untouched so the eventual in_review re-entry still re-discovers the feedback.
  6. If no unread feedback at all (watermarks already cover the bookmarks), publish any **stranded fix** first —
     `_stranded_fix_unpushed` against the worktree the issue already has on disk, i.e. a commit an earlier run left
     unpushed (a dev run whose outcome the live-pause guard discarded, a run killed before its push) — through the
     same [size gate](#the-size-gate-on-a-published-pull-request-every-push-onto-an-open-pr)
     the shared dev-fix publication passes, since this is the second seam a candidate reaches a
     published pull request through and a
     bounce that pushed unmeasured would be the way past a ceiling every other route holds to. On a
     successful push adjust `review_round` per the same route discriminator the pushed-fix exit uses (`pending_fix_at`
     read BEFORE the clear: in_review route resets to 0, validating route bumps by 1). Then clear `pending_fix_*` and
     bounce back to `workflow:validating`. A worktree that is not on disk, a probe refusal (dirty tree, failed fetch,
     a remote that moved), or a failed push bounces without pushing and without touching the round — the commit stays
     on the branch for a later push to carry. A candidate the gate HELD stops the bounce outright: the issue is on
     `workflow:decomposing` by then, and relabeling over it would publish the very question the gate just opened.
     This exit is the validating route's LAST chance at that commit: the
     reviewer feedback that started the round is orchestrator-authored, so the step-3 rescan filters it out and no
     later tick re-runs the dev on it.
  7. **Quiet window**: compute the newest `created_at` (or `submitted_at` for review summaries); if younger than
     `IN_REVIEW_DEBOUNCE_SECONDS`, return.
  8. **Resume**: build a `_build_pr_comment_followup` prompt over ALL unread surfaces, resume the locked dev via
     `_resume_dev_with_text` (`pause_guard=True`), refresh `user_content_hash` (so any issue-thread comment we just fed
     to the dev doesn't re-fire validating's drift check). An `interrupted` resume is ignored entirely BEFORE the ACK
     fast path, the stranded-fix check, and the watermark advance below: the handler returns WITHOUT writing pinned
     state, so no watermark advances, `awaiting_human` is untouched, and the next tick re-discovers the same feedback. A
     mid-run `paused` / `backlog` short-circuits the same way, right after the interrupted check. Otherwise, a
     no-commit reply first checks for a **stranded fix** (`_stranded_fix_unpushed`): when the worktree is clean and HEAD
     is strictly ahead of the fetched remote PR branch (a fix committed by an earlier parked run whose publish was
     blocked — e.g. a dirty-park whose stray files were cleaned up afterwards), the handler publishes it through the
     normal push tail and treats the run as a pushed fix — this outranks the ACK fast path on both routes, so an acked
     stranded fix is published rather than relabeled. **ACK fast path** (in_review route only, no stranded fix): if the
     dev makes no commit but ends its message with the `ACK: <reason>` marker (the prompt instructs it to emit this when
     the comments name no actionable change — a vague "continue" / "ok"), clear `pending_fix_*`, post the ack as an
     FYI, and relabel straight to **`in_review`** without parking. Otherwise apply the same `_handle_dev_fix_result`
     disposition as the validating fix-loop. Any other unmarked no-commit reply falls through to `_on_question` and
     parks awaiting human — a no-ACK reply may be a real dev question, and we cannot tell by inspection (a dirty tree,
     failed fetch, or a remote that moved past the local view also falls back to this park rather than pushing blind).
  9. **Watermark advance**: regardless of dev outcome, `_advance_consumed_watermarks` advances each of the three
     watermarks ONLY to the max id consumed on that surface — tighter than a broad bump so a concurrent human comment
     that landed mid-handler survives to the next tick.
  10. **On a pushed fix**: clear `pending_fix_*`, adjust `review_round` per the route discriminator (in_review route
      resets to 0 — the previous approval was for the prior head; validating route bumps by 1 — same review cycle),
      flip DIRECTLY back to `workflow:validating`. Docs do not run on this exit.
- **Output**: terminal `done` / `rejected`, OR label flipped to `workflow:validating` (pushed fix OR no-new-feedback
  bounce), OR label flipped to `workflow:resolving_conflict` (stuck validating-route transient park while the worktree
  is out of sync with the PR — behind base or an unpushed local rebase), OR label flipped to `workflow:decomposing`
  (the size gate held a fix, a stranded-fix bounce, or a transient-park recovery push), OR label flipped to
  `in_review` (in_review route, ACK fast path on this tick only), OR a HITL park, OR a no-op (quiet-window wait,
  missing-PR park already set).

## `_handle_resolving_conflict` (label `workflow:resolving_conflict`)
- **Trigger**: each tick while label is `workflow:resolving_conflict` (set by an operator relabel, by
  `_refresh_base_and_worktrees` when the auto rebase actually left conflicted files — a merely-behind-base PR rebase +
  push lands directly on `workflow:validating` — or by `_handle_fixing`'s worktree-drift dead-lock breaker when a
  validating-route transient `workflow:fixing` park whose self-recovery returned `"stuck"` is found out of sync with
  the PR head). Also runs on closed-`workflow:resolving_conflict` issues for terminal handling.
- **Input**: pinned `pr_number`, `branch`, `dev_agent` / `dev_session_id`, `conflict_round`. `MAX_CONFLICT_ROUNDS` from
  config.
- **Internal flow**:
  1. If `pr_number` is missing → park.
  2. Read the PR and hand it to the shared `_drain_review_pr_terminals` helper. `resolving_conflict` rebases the PR
     branch onto `<remote>/<base>` — it never merges, so any `merged` state was produced externally. Branch on
     `pr_state`: `merged` → `done` + close + cleanup; `closed` → `rejected` + close + cleanup; `open` → fall
     through.
  3. If the issue itself was closed manually while the PR is still open, flip to `rejected` without branch cleanup
     (operator may salvage). The closed-issue sweep does not surface `rejected`, so the operator must clean up the
     worktree / branch by hand if the PR later closes.
  4. **The two dev resumes** — a body edit mid-rebase, and a human reply on a park — are decided *inside* the
     reconciliation below (step 8), not in front of it. Both start an agent whose commit this stage force-pushes, so
     neither may run over a checkout nobody has placed against the remote. On a branch the remote has moved **past**,
     the push drops the commits that moved it and **no lease catches that** — the tip it is pinned to is the tip the
     resume itself read, so git has nothing to refuse. On a branch **ahead** of its remote, the head the round began
     at is a local commit the remote has never seen; leased against it, the resume's own commit is refused by the size
     gate as somebody else's movement, with the edit that prompted it already consumed. So an ahead branch ships its
     recovered commits first (step 8) and the human waits a tick. Every road out of this stage therefore runs behind
     one reading of the branch. What an *ahead* branch costs differs by resume, though, and only the **body edit**
     defers: it leases against the head the round began at, read off this checkout, while the reply's publication
     freezes the pull request's own head before the agent runs — so an unpublished commit under it changes nothing
     and the resolution goes out carrying it. Deferring the reply too would push a commit the reply was never fed to
     as finished work, which is exactly what an `agent_timeout` park over a clean commit leaves. And the edit *falls
     through* to the reply rather than ending the tick, because the drift hash covers the thread as well as the body:
     **every reply moves it**, so on a parked issue a reply arrives looking like an edit, and ending the tick there
     would drop it into the recovered push with the pre-reply commit shipped and the reply neither fed to anybody nor
     consumed. The one thing that does stop a reply on an ahead branch is a settled round still owed, since the
     recovered push is what pays it and they share the one receipt slot. The body edit is asked first, because it
     changes what
     "resolved" means and the reply may answer a question the edit has already overtaken; a pushed answer hands back
     to `workflow:validating`, and a bare acknowledgement stays here without parking so a harmless clarification does
     not stall the rebase. The reply
     path uses the same `_post_conflict_resolution_result` helper as the fresh path, and a bare `/orchestrator
     continue` on it is intercepted like `validating`'s: a session-failure park (`agent_silent` / `agent_timeout`)
     retries the dev on the neutral `_CONTINUE_RETRY_PROMPT` instead of the literal command, a park needing a real
     answer refuses, and an auto-rebase park is left to the refresh retry-unpark (`_continue_command_action` /
     `_refuse_parked_continue`). A park left by a *reading* rather than a question is not answered here at all — see
     the transient-park note below.
  5. Ensure the PR worktree, refresh the refs, and read the divergence (steps 6–8 below). The **cap check** comes
     after all of it, immediately in front of the rebase in step 10: what `MAX_CONFLICT_ROUNDS` refuses is another
     *attempt*, and everything step 8 does is work already done that this stage still owes an effect for — a round a
     settlement published, commits an earlier tick never pushed, a human whose edit or reply is waiting. Refused with
     the attempts, none of those ends the loop; they strand, since nothing else pays a receipt, publishes a stranded
     commit, or answers a person. Once step 8 is through and `conflict_round >= MAX_CONFLICT_ROUNDS`, park. Escape:
     (a) operator relabels off `workflow:resolving_conflict`, or (b) a new issue comment unparks via the resume
     branch, which step 8 reaches before the cap.
  6. Ensure the PR worktree via `_ensure_pr_worktree` (restores from `<remote>/<branch>` when THIS tick's fetch of it
     landed, NOT base — `_ensure_worktree` would discard the PR's commits — and never from a remote-tracking ref a
     failed fetch left behind, which resolves perfectly well while naming whatever was last seen; and from
     `<remote>/<base>` only when the remote itself says the branch is gone, which is a merged PR whose branch GitHub
     deleted seen from a host without the local ref: naming a ref nobody has would fail the `worktree add` on this
     tick and every one after it, and what
     that branch carried is in the base by then).
  7. Refresh `<remote>/<branch>` over `_authed_fetch` so a stale local ref doesn't mis-classify a "remote moved"
     situation as in-sync.
  8. Compare HEAD to the freshly-fetched `<remote>/<branch>`, through the one reading that resolves that ref once
     and counts against the commit it named. A reading that did not happen parks (`unreadable_divergence`) rather
     than answering `(0, 0)`: no rebase runs, no agent is spawned, and nothing is pushed over a branch nothing
     compared.
     - `behind > 0` (worktree diverged) → normally park (`diverged_branch`) since force-pushing could clobber the real
       PR head. **Exception — already-rebased-but-unpushed:** when the worktree is also `ahead > 0` AND already sits
       on top of base (`_already_rebased_onto_base` re-fetches base and checks `HEAD..<remote>/<base>` is empty) AND the
       stale remote head is one the orchestrator itself produced (`_pr_head_orchestrator_produced`:
       `pr.head.sha == docs_checked_sha` — the only key production code persists for an orchestrator-pushed head,
       written by `_handle_documenting`'s success exits), the "behind" commits are the orchestrator's own superseded
       pre-rebase commits — there is nothing external to lose, so fall through to the `ahead > 0` push and
       force-publish instead of parking. PR heads from earlier in the lifecycle (the initial implementing push, an
       intermediate fixing push) are not currently recorded anywhere in pinned state, so the exception declines those by
       design. If either guard fails (not on base, or an unrecognized head that might carry a direct push), keep the
       `diverged_branch` park.
     - `ahead > 0` (recovered unpushed commits, or the already-rebased fall-through above) → dirty-tree check, then
       push the recovered work and flip to `workflow:validating` with `review_round=0`, `conflict_round += 1`. The
       push is **pinned to the tip this comparison was taken against**, read from the same `<remote>/<branch>` ref the
       fetch a step earlier put there and carried on the sync record beside the counts. "Ahead and not behind" is a
       claim about that one commit and it is the whole of what licenses the force-push: left unnamed, the gate reads
       the pull request for itself and a foreign push landing in between becomes both the head it freezes and the
       lease — so commits an interrupted tick left would be measured, published over somebody else's work, and handed
       to the reviewer as a resolved round. The already-rebased exception outranks it with the head it validated as
       orchestrator-produced, which is a stronger claim about the same fact; where NEITHER names a head the push
       refuses (`unpinnable_recovery`) rather than letting git take its own reading at push time.
       The commit the recovered push leaves the branch on is read *before* the push and **named** to the gate on
       every road, and a reading that failed parks `unreadable_head`. Naming it is what makes the push and the record
       of it one decision: the gate proves the checkout independently and the worktree is writable in between, so an
       unnamed push publishes whatever landed in that window — under a lease proved against the head the branch used
       to be on — while nothing on this road ever read it. Where the push also leaves the branch **on its base** it
       finishes a round of its own, and the same id is what that round is recorded under, in the audit event and in
       the `conflict_settled_outcome` / `conflict_settled_sha` receipt a size-gate hold leaves behind. That receipt
       goes down in the push's own durable write, so a crash between it and the tail would come back to
       `("recovered_push", "")` — a pair no later tick can prove, on a branch that is in sync by then, so the round a
       push really landed is reported as the no-op flip instead. Only what the push *owes* turns on the behind-base
       reading: still behind, it records nothing and the rebase behind it owns the round.
     - `(0, 0)` → fall through.
  9. Read the **pre-rebase HEAD**, and park `unreadable_head` when nothing could. It is not bookkeeping: it is the
     head both exits of this round lease their force-push against, and the size gate reads "no head" as a caller that
     established none — pinning the push to whatever the pull request is standing on when *it* looks, which is after
     the rebase or after an agent that was out for minutes. A commit somebody else landed in that window would become
     the lease and be force-overwritten by work never proved against it. Refused here nothing is rebased and no dev
     session is resumed, so the checkout is left exactly as it was found. The body-edit resume reads the same head
     for the same reason, and refuses *before* it refreshes `user_content_hash` or marks the drift comments consumed,
     so the edit is still there for the next tick to detect.
  10. Refresh `<remote>/<base>` and run `git rebase <remote>/<base>` under `_git_hardened` (drops global / system
      config, disables hooks / fsmonitor / credential helpers / commit signing / autostash — the agent owns the
      worktree and could otherwise plant a hook to execute attacker code mid-rebase).
  11. **Clean rebase succeeded**: a PROVED clean tree first — a status read that established nothing names no
      paths, exactly as a tree with nothing in it does, so only a reading that happened AND named nothing gets past
      it, and either failure parks `dirty_worktree`. Then the post-rebase HEAD, proved rather than assumed: one that
      would not resolve parks `unreadable_head` rather than reading as "already up-to-date", which would hand the
      round back with nothing having established whether the rebase left a rewritten commit the PR never received.
      If HEAD did not move (already up-to-date), skip the push and flip to `workflow:validating` (`review_round=0`,
      `conflict_round += 1`). Counting no-ops against the cap surfaces a perpetually-unmergeable-due-to-branch-
      protection PR within `MAX_CONFLICT_ROUNDS` ticks. If HEAD moved, force-with-lease push and flip to
      `workflow:validating`.
  12. **Conflicted rebase**: build a conflict-resolution prompt via `_build_conflict_resolution_prompt`, resume the dev
      with it (`pause_guard=True`), then run `_post_conflict_resolution_result`.
  13. `_post_conflict_resolution_result`: `interrupted` (shutdown sweep killed the run mid-flight) → ignore the
      partial result and return WITHOUT writing pinned state, leaving durable state retryable (this is the one branch
      that does not write; it precedes all others); timeout / unfinished rebase / no commit / dirty / push fail →
      park; success → force-with-lease push, increment `conflict_round`, reset `review_round=0`, flip to
      `workflow:validating`. Fresh-rebase pushes pin the lease to the pre-rebase PR head; awaiting-human resume
      pushes pin it to the head the pull request was standing on when the tick fetched it, which is read BEFORE the
      session resumes — the local `before_sha` may be an intermediate SHA on a worktree that is mid-rebase or already
      ahead of its publication, and a head left for the size gate to read after the agent returns is whatever landed
      while it was out, so the force-push would adopt a concurrent update as its own lease. On BOTH
      resume paths (fresh conflict and awaiting-human), a mid-run `paused` / `backlog` returns in the handler BEFORE
      `_post_conflict_resolution_result` runs, so the resolved commit stays on the branch and no push / relabel /
      write happens until the label is removed.
- **Output**: label moved to `workflow:validating` (any pushed resolution OR no-op rebase), OR
  `workflow:decomposing` (a content update the size gate held), OR no label change (drift
  ACK / `_on_question` park: rebase still unfinished), OR `done` / `rejected` (terminal), OR a HITL park.

The rebase path deliberately rewrites the PR branch to keep history linear after other issue PRs land. Every pushed
rebase resets `review_round`, so the reviewer must re-approve the rewritten head before the in_review ready-ping gate
can fire.

### Content updates onto the pull request this stage already has

Every commit this stage publishes joins a pull request the remote already carries, so all four of its changed-head
publications pass the [size gate on a published pull
request](#the-size-gate-on-a-published-pull-request-every-push-onto-an-open-pr) before anything reaches the remote —
`conflicts/publication._publish_clean_rebase` for a rebase that produced a new head,
`conflicts/outcomes._finalize_conflict_resolution` for a resolution an agent wrote (both the fresh conflict and the
awaiting-human resume behind it), `conflicts/divergence._push_recovered_commits` for commits a crashed tick never
pushed, and the body-edit resume in `conflicts/resume.py` through the shared dev-fix publication. One of the four
also hands the gate what it REPLACED, built by `conflicts/evidence`: the rebase is history this stage replayed
itself, so an adjudicated change may be recognized in the object that replaced it rather than measured past the same
ceiling twice. The other three hand in nothing, because none of them can say what the commit it is publishing is.
What the gate counts is what the pull request would **come to** — three-dot from the base the *remote* names to the
candidate — so the ceiling is cumulative and a two-line resolution onto an already-large branch is held exactly as a
large one is. Growing a branch past `MAX_ADDED_LINES` one small conflict round at a time is the outcome that
measurement exists to prevent.

- **The lease stays this stage's.** Each of the four reads a head for itself and pins its force-push to it — the
  pre-rebase head for the fresh rebase and the resolution behind it, the tip the pull request was fetched at for the
  awaiting-human resume, the tip the ahead/behind comparison was taken against for the recovered push — and the gate
  *checks* that head against the one it reads rather than substituting for it. Two readings of one fact that disagree
  are a pull request somebody moved mid-tick, and the call refuses instead of freezing a tip the branch would not be
  pushed onto. `DECOMPOSE=off` turns the measurement off and neither the naming nor the lease with it.
- **A round is counted only after a push.** `conflict_round`, the `last_conflict_resolved_at` stamp, and the
  `conflict_round` audit event all live on the pushed-round tail (`_hand_resolved_round_to_validating`), so a held
  candidate and a failed push each leave the counter alone — spending one for a push that never happened brings
  `MAX_CONFLICT_ROUNDS` forward by a round nobody ran. The single exception is the no-op flip, which counts a round
  *because* nothing was published; see below.
- **A hold ends the tick here.** The commit stays on the branch, the issue is on `workflow:decomposing`, and neither
  the hand back to `workflow:validating` nor the rebase behind a held recovered push is this tick's to make. What the
  round would have been is written inside the gate's own durable write, ahead of the relabel, as
  `conflict_settled_outcome` / `conflict_settled_sha` — `base_rebased_clean`, `agent_resolved`, `recovered_push`, or
  `drift_resolved`, with the head it produced. The resumed tick cannot re-derive either: a settled `single` verdict
  publishes the accepted commit, so the branch the label comes back to already carries its base, which is the no-op
  flip's own reading. A recovered push that leaves the branch still *behind* base records nothing — it is the preamble
  to a rebase that owns the round and leaves its own receipt.
- **One receipt slot, so one outstanding round.** `conflict_settled_outcome` / `conflict_settled_sha` is a single
  pair, and every content update that can be held writes into it — so a tick that starts a *new* resume while a
  receipt is still standing would record its own outcome over the round a settlement already published, and the
  earlier one would never be counted. Ordering is what keeps them apart, all of it inside
  `_reconciled_before_the_rebase`: `_finished_settled_round` is asked before either dev resume, and a checkout that is
  *ahead* of its remote — the one shape where the receipt cannot be paid on the spot — defers both resumes to the
  recovered push below, which pays it. Either way the edit or the reply is left unconsumed for a tick that can act on
  it. Nothing is lost by waiting: a standing receipt says this stage's last resolution is already on the pull request,
  so there is no in-flight resolution for the dev to reconsider. A receipt that cannot name both ends is no receipt —
  `_settled_round_owed` declines it, and the ordinary road clears it by reaching a tail of its own.
- **The cap guards the rebase, and nothing above it.** `MAX_CONFLICT_ROUNDS` refuses another *attempt* — the rebase
  and the dev run behind it — so it is asked once the reconciliation is done and immediately in front of
  `_rebase_and_dispose`. Everything the reconciliation does is work already done that this stage still owes an effect
  for, and refusing those does not end the loop, it strands them: nothing else pays a receipt, publishes a commit an
  earlier tick made, or answers a person. A body edit on a spent counter is still resolved, so a hold there records a
  receipt at the ceiling; and where that receipt sits on an *ahead* branch, the recovered push is the only road that
  pays it. Counting it takes `conflict_round` one past the ceiling, which is correct: the round was spent on a push
  that really landed, and the cap fires on the next attempt. Whichever tail finally pays a round also clears the park
  it ran under, so `workflow:validating` is never handed an issue that reads as waiting on somebody.
- **A tree nobody read is not a clean one.** A `git status` that established nothing names no paths, and so does a
  tree with nothing in it — so every probe reporting the paths alone answers the same for both, and taken as clean a
  checkout carrying uncommitted edits is published as a commit that silently omits them. The size gate proves the
  tree for itself, but only as part of freezing an entry, which an install running `DECOMPOSE=off` never does: there
  the push goes out and a proof taken afterwards can park without taking the remote update back. So this stage takes
  the reading itself, ahead of the effect, on both roads that end in a publication from the checkout — the recovered
  push requires a *provably clean* tree, and either dev resume requires one that at least **read**. A merely dirty
  tree is not this: that is the park a reply exists to unstick, and the dev is resumed over it to clean it up.
- **A park is not always a person.** The refusals this stage takes over a reading that *did not happen* —
  `fetch_failed`, `unreadable_divergence`, `unreadable_head`, `unreadable_worktree`, `unpinnable_recovery` — name
  nothing a reply could answer: what clears them is the same reading taken again. Their reason is recorded durably
  (`park_reason`, re-set after `_park_awaiting_human` clears it) and a tick that finds one standing carries on with
  its ordinary work rather than consuming itself as an awaiting-human resume — which is what would otherwise leave a
  repaired checkout parked for good, with the thing the notice asked for already done. Because those retries run every
  poll, each is announced **once** — both fetches included, the base ref's as much as the pull request branch's — and
  "once" means once per *reason*: an issue already parked for an agent question that then becomes externally diverged
  is told about the divergence, since that is new and it is what now blocks the reply it was waiting to give.
  Diverged is not transient, though, and a transient refusal taken over a park somebody **owes an answer to** records
  nothing at all: the standing reason is what the next tick reads, so a fetch that failed for one poll while an
  agent's question waited would otherwise hand the tick behind it a branch that reads as nobody waiting, and it would
  rebase, push, count the round, and hand `workflow:validating` work the human was asked about — with the reply
  swallowed too, since re-parking ratchets the consumed-comment watermark past everything on the thread.
  `_park_conflict` and the awaiting-human resume ask one predicate for "is this park a person's", so the two
  cannot drift apart.
- **What it refuses is a handoff, not a round.** The settlement is asked over a branch **in sync** with its remote and
  standing on the head the receipt names. Behind the remote it declines and the divergence guard behind it parks
  `diverged_branch` — and there the round is not what fails. A remote standing on a *descendant* of the settled commit
  still carries it, so the round did land; what cannot be handed on is this **checkout**. The tail hands
  `workflow:validating` the worktree as it stands, and `_ensure_worktree` behind the reviewer *reuses* a checkout
  rather than fast-forwarding it to the tip — so waving it through counts the round correctly and then shows a human
  a verdict taken over the commit the pull request has already moved past. The receipt keeps standing instead, the
  park asks for the branch to be reconciled, and the same reading settles the round on the tick after that. Ahead of
  the remote it declines too, and there the recovered-commit push carries the commit back through the gate.
- **`single` and `split` settle through the shared protocols, and they do not settle alike.** Nothing about the
  adjudication is this stage's: the hold, the verdict, and what each earns belong to `workflow:decomposing`. A
  **`single`** publishes the accepted commit and settles at the stage the record names, which puts
  `workflow:resolving_conflict` back — and `_finished_settled_round` is the whole of what this stage then does with
  the answer. Asked before the rebase, over a branch in sync with its remote AND standing on the head the receipt
  names, it runs the tail its own tick never reached and drops the receipt. Ahead of the remote the receipt stands
  and the recovered-commit push carries the commit back through the gate, which is the one road that measures it
  again. A **`split`** never comes back here: it snapshots the candidate on an immutable ref, supersedes and closes
  the pull request the conflict round was being fought over, and retires the generation in the write that hands the
  parent to `workflow:umbrella` with its children released — so the receipt is left standing on an issue this stage
  will not run again, and the round it names is one no `conflict_round` ever counts.
- **A reading nobody could take parks, and the retry costs no agent.** A failed count leaves the pair frozen with its
  publication group and no number on it; the reconciliation ahead of the next handler
  (`implementing/late_reconcile._reconciles_published_work`) re-measures *that recorded pair* — asked for by id, not
  re-derived from a branch the base refresh has moved under — and publishes, routes, or parks it without rebasing and
  without resuming the developer who already finished. A publication that MOVED under a live record (somebody pushed
  to the pull request, or the issue was repointed at another one) is refused with the record left exactly as it
  stands, since re-entering it would stamp this tick's reading over the evidence the count was actually taken on.
- **Neither no-publish exit enters the gate.** A rebase that left HEAD where it found it has nothing to push, so
  nothing is read, frozen, or counted — and it still bumps `conflict_round`, which is what surfaces a pull request
  blocked by branch protection rather than by content within `MAX_CONFLICT_ROUNDS` ticks. A body edit the dev answers
  with `ACK:` and no commit is the same: no measurement, no round, and no park.
