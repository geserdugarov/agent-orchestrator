# Workflow state machine

This file documents the label-based state machine that drives every GitHub issue from pickup to terminal. It is split out of [`architecture.md`](architecture.md), which keeps the high-level overview, module map, and process / agent / push / event-log details.

The sections below cover:

- [Workflow labels](#workflow-labels) — the label set and what each one means.
- [Per-tick flow (`workflow.tick`)](#per-tick-flow-workflowtick) — how a single tick fans out across repos, partitions issues by label, and dispatches handlers; and the per-issue pinned-state schema the handlers read and write.
- [Stage handlers](#stage-handlers) — the per-stage internal flow, the user-content drift hook, and the transitions each handler may produce.
- [State transition (label lifecycle)](#state-transition-label-lifecycle) — the compact label-lifecycle reference diagram.

## Workflow labels

An issue should have at most one workflow label at a time. Non-workflow labels such as `bug` or `enhancement` are preserved; orchestrator label writes only swap labels from its own workflow set. Label names are part of the public contract because live GitHub issues carry them, so renaming or repurposing one is a migration.

The orchestrator also creates the non-workflow control label `hold_base_sync`; while present on an issue, it pauses per-tick base sync, the `in_review` mergeable / unmergeable handling (HITL ping and unmergeable park), and `resolving_conflict` base rebases until the label is removed.

A second control label `backlog` is created for postponed work. While present on an issue, every per-tick handler skips it before the workflow label is even read, so the orchestrator does not pick up, decompose, or otherwise advance the issue. Removing the label hands control back to the state machine on the next tick — typically applied at issue creation to queue work that should sit until a human is ready.

| Label | Meaning |
|---|---|
| _(none)_ | Open issue not yet picked up by the orchestrator. |
| `decomposing` | The decomposer is deciding whether the issue is single-context or should become child issues. |
| `ready` | The issue is decomposed and has no unresolved blockers. |
| `blocked` | The issue is waiting on child issues or dependency edges. |
| `umbrella` | Parent issue with no implementation of its own; closes to `done` when all children resolve. |
| `implementing` | The dev agent is producing commits in a per-issue worktree. |
| `documenting` | The dev session runs the single docs pass on the existing PR worktree, reached only via the **final-docs handoff** in `_handle_validating`'s approval branch (after verify + squash). Advances to `in_review` after a pushed docs commit OR an explicit `DOCS: NO_CHANGE` verdict against a remote-clean head. Every pre-approval push (implementing, validating/fixing, in_review drift, resolving_conflict) hands straight back to `validating` instead — the docs pass runs exactly once per reviewer-approval handoff. |
| `validating` | The reviewer agent is checking the diff and may bounce fixes back to the dev agent. |
| `in_review` | A PR is open and ready for human review and manual merge. The orchestrator never merges from here -- humans drive the merge. A mergeable PR whose current head completed the reviewer-approved final-docs handoff (or carries a real GitHub APPROVED review), with no standing human CHANGES_REQUESTED, earns a one-shot HITL ping per head SHA; an unmergeable PR parks awaiting human attention. |
| `fixing` | The dev fix-loop is active. Entered from two routes: `_handle_in_review` on unread in-review feedback (any of the four PR comment surfaces) or a human CI-fix request, AND `_handle_validating` on a `CHANGES_REQUESTED` verdict (the relabel lands BEFORE the dev spawn so the dev-fix subphase is observably under `fixing`, not `validating`). A successful fix — and a rescan finding no unread feedback — both bounce DIRECTLY back to `validating` so the reviewer re-approves before the in_review ready-ping gate can pass. Docs are deferred to the final-docs handoff after reviewer approval. |
| `resolving_conflict` | The orchestrator is rebasing a PR branch onto `<remote>/<base>` (the remote name comes from `spec.remote_name`, default `origin`). Reached via an operator relabel or the per-tick base-sync detour. |
| `question` | Operator-applied read-only Q&A label: the orchestrator runs the decomposer agent in the per-issue worktree, posts the answer to the issue thread, and waits on a human reply or a manual close. No PR is opened on this label. |
| `done` | Terminal success; the PR merged, an umbrella parent resolved after all children reached `done`, or a `question` issue was closed by the operator. |
| `rejected` | Terminal rejection; the PR or issue was closed without merge. |

## Per-tick flow (`workflow.tick`)

Each tick fans out across every configured repo (`config.default_repo_specs()` returns one `RepoSpec` per `REPOS` line) and dispatches per-issue handlers through a long-lived `IssueScheduler` capped by `MAX_PARALLEL_ISSUES_GLOBAL` / `MAX_PARALLEL_ISSUES_PER_REPO`. See [`architecture.md#per-tick-flow-workflowtick`](architecture.md#per-tick-flow-workflowtick) for the multi-repo dispatch, `ThreadPoolExecutor`, scheduler lifecycle, and `_run_tick` shutdown / reap details — the rest of this section focuses on the state-machine-relevant parts: label classification, the base-sync detour, the closed-issue sweep, and the pinned-state JSON schema.

The dispatch loop classifies each pollable issue by workflow label before submitting it:

- **Family-aware labels** — `decomposing`, `blocked`, `umbrella`, and unlabeled (pickup) issues — read and write cross-issue state (parent ↔ child). Two of these running at once could race a parent's child-state write against the child's own handler on a sibling thread. The dispatch loop folds every family-aware issue this tick into ONE bucket submit per repo that drains its issues sequentially on a single worker thread, so the umbrella parent always gets its turn within the same tick (a stale `blocked` child cannot take the slot and starve the parent that would relabel it to `ready`).

  Each per-issue iteration inside the bucket wraps `_process_issue` in `scheduler.track_active(spec.slug, n)` so `is_active(repo, n)` keeps reporting True for the family issue currently being processed — without that, the base-refresh skip contract would observe only the bucket's sentinel key and race the agent on the actual issue.

  When every family-aware issue this tick carries the `umbrella` label, the bucket submit is marked `cap_exempt=True` and runs on a dedicated executor pool: umbrella handling is a pure dep-graph walk so it does not consume a `MAX_PARALLEL_ISSUES_PER_REPO` / `MAX_PARALLEL_ISSUES_GLOBAL` slot. The family mutex still applies, so a follow-up tick that picks up a non-umbrella family issue serializes against the in-flight umbrella bucket.
- **Fan-out labels** — `ready`, `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, and the operator-applied `question` — only touch their own per-issue pinned state and worktree. They fan out concurrently up to the per-repo and global caps. A family worker can overlap with fan-out workers on the same repo: the family mutex only excludes other family-aware handlers. `question` is deliberately kept out of `_FAMILY_AWARE_LABELS` so a Q&A issue does not serialize behind an in-flight decomposer / blocked / umbrella tick.

The duplicate-active gate keys on `(repo_slug, issue_number)`: an in-flight handler that straddles polling passes is reported active to the next poll's submit, which is rejected with `reason=duplicate_active` and re-enumerated next tick. The `_refresh_base_and_worktrees` pre-tick refresh consults `scheduler.is_active(spec.slug, issue_number)` per worktree and skips any active issue's worktree, so a base advance cannot rebase a pre-PR worktree under a running agent or relabel a PR-having worktree mid-handler.

Only issue numbers cross the thread boundary — each scheduler worker calls `gh._for_worker_thread()` to mint a fresh `GitHubClient` and refetches its Issue against that client (PyGithub's per-request state is not documented as thread-safe across a shared `Requester`). A lazy-load failure on one issue's labels is logged and that issue is conservatively routed into the family bucket where the per-issue try/except picks up any sustained failure.

Inside `workflow.tick(gh, spec)`, before any issue is dispatched the tick runs `_refresh_base_and_worktrees(gh, spec)`: a single `git fetch <spec.remote_name> <spec.base_branch>` in `spec.target_root` (the remote name defaults to `origin` but is overridable per `REPOS` entry via the fourth pipe-separated field, so a `REPOS=...|private|2` row fetches from `private/<base>`), then per-issue dispatch on each existing worktree under `<WORKTREES_DIR>/<owner>__<name>/issue-*`. The per-stage `_ensure_*_worktree` helpers only fetch base on (re)creation, so a worktree that survives across ticks would otherwise stay anchored at whatever `<remote>/<base>` looked like when it was first added.

Two paths depending on whether a PR already exists for the issue:

- **Pre-PR worktrees** (no `pr_number` in pinned state) get a clean-tree `git rebase <remote>/<base>` directly — there is no remote to push to, so the local branch can be kept linear without publishing a rewrite.
- **PR-having worktrees** in `validating` / `documenting` / `in_review` / `fixing` are detoured to `resolving_conflict` instead (via `_route_pr_worktree_to_resolving_conflict`: post a PR notice, seed `conflict_round` only when absent, flip the label) so the existing `_handle_resolving_conflict` handler does rebase + force-with-lease push + relabel-to-`validating` (the same target as the base-up-to-date no-op) in one consistent flow. `documenting` is included so a sibling-PR merge during the brief final-docs hop does not leave the docs commit on a stale base; the handler itself only checks ahead/behind vs. the PR branch.

Applying `hold_base_sync` to an issue skips both paths for that issue; removing the label lets the next tick perform the accumulated base sync once. The `question` workflow label skips base sync unconditionally for the same read-only reason `_handle_question` already tears down its own worktree on every safe exit — merging `<remote>/<base>` into a question worktree would either accrete commits on a read-only branch or mask the inspection state of an unsafe park (`question_commits` / `question_dirty` / `question_timeout`).

A local-only rebase on a pushed branch would otherwise diverge local HEAD from `pr.head.sha` and break the validating reviewer (it reads local HEAD, so it would review a SHA that isn't on the PR) and `_squash_and_force_push`'s `--force-with-lease=<original_head>` (the lease compares against the un-rebased remote tip). `_handle_resolving_conflict` just does rebase + push + relabel.

The detour deliberately does NOT call `_bump_in_review_watermarks` (the `_handle_in_review` analog runs that AFTER scanning new comments — running it here, before any handler scans, would silently mark unread human "do not merge" / fix-request comments as consumed and they would never reach the dev). The orchestrator's own PR notice is filtered out via `orchestrator_comment_ids` on the next in_review scan, so leaving the watermark alone is safe.

The detour also skips when `awaiting_human=True` because `_handle_resolving_conflict`'s awaiting-human branch returns early without rebasing unless a new human comment arrived; relabeling here would just hide the existing park behind a `resolving_conflict` label without progress, including the documented in_review unmergeable-park case.

Before relabeling, the detour fetches `gh.get_pr(pr_number)` and skips when `pr_state != "open"`: a just-merged PR advances `<remote>/<base>`, so the still-validating / still-documenting / still-in_review / still-fixing worktree pointed at the now-stale branch is naturally behind base; without this gate the refresh would post an "auto-resolution" notice and relabel to `resolving_conflict` on a PR the next handler call would finalize to `done` (or `rejected` for a closed-without-merge PR).

A `gh.get_pr` failure is treated as "leave alone" so the handler retries from a stable label rather than racing a half-known PR state. Issues already labeled `resolving_conflict` are also skipped (the handler runs this tick anyway).

Rebase is used across both paths to keep issue branches linear after sibling PRs land. Dirty worktrees (in-flight agent edits, crash-recovered trees) are skipped, and on a pre-PR content conflict the rebase is aborted so the worktree stays on its pre-rebase SHA. For PR branches, every pushed rebase resets `review_round`, so the reviewer must approve the rewritten head before the in_review ready-ping gate can pass. Failures are logged and swallowed; keeping every issue moving matters more than perfect base sync.

Then `gh.list_pollable_issues()` yields all open non-PR issues plus closed non-PR issues still labeled with one of the seven sweep labels: `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, or `question`. The pre-PR labels (`decomposing` / `blocked` / `umbrella` from the family-aware set, plus the fan-out `ready` label) are deliberately NOT swept closed — they exist before any agent-authored PR can be merged externally, so a closed issue at one of those stages is treated as a hard human stop until an operator relabels. The closed-issue sweep over the seven labels is what makes external manual merges and operator closes finalize cleanly:

- Closed `in_review` / `fixing` / `resolving_conflict` — a human-merged PR with a `Resolves #N` footer auto-closes issue N before the orchestrator can flip the label; without the sweep those handlers would never run on the closed issue.
- Closed `implementing` / `documenting` / `validating` — the same external-merge race when the human merges before the orchestrator reaches `in_review`. Each handler's entry-time `_finalize_if_pr_merged` flips the label to `done` instead of leaving the issue stuck at an in-flight stage (an umbrella parent would otherwise aggregate on the stale child label forever).
- Closed `question` — a human closing the issue is the terminal signal `_handle_question` consumes to finalize to `done` and clean up the per-issue worktree/branch.

For every yielded issue the dispatcher reads its workflow label and routes to the matching handler. `done` and `rejected` are terminal no-ops; the operator-applied `question` label is an out-of-lifecycle branch with no automatic stage transitions (see [`_handle_question`](#_handle_question-label-question)). Every handler receives the active `RepoSpec`, so `git worktree add`, `git fetch <spec.remote_name> <spec.base_branch>`, push-token resolution (`config._resolve_github_token(spec.slug)`), and PR-base selection all flow from the spec.

Per-issue durable state lives in a single **"pinned" comment** on the issue (`<!--orchestrator-state {...json...}-->`). The keys it holds:

- `dev_agent` + `dev_session_id` — the dev session's locked spec and session id (see [`workflow.md#in-flight-session-lock--pinned-full-spec-until-the-session-ends`](workflow.md#in-flight-session-lock--pinned-full-spec-until-the-session-ends) for the raw-full-spec pinning and the `_parse_agent_spec` re-parse on every resume).
- `review_agent` — the spec the most recent reviewer spawn used. Reviewer is fresh per round so this is traceability only, not a lock.
- `decomposer_agent` + `decomposer_session_id` — parents only; same lock-on-first-spawn semantics as `dev_agent`.
- `question_agent` + `question_session_id` — `question`-stage issues only; same lock-on-first-spawn semantics as `dev_agent`. Seeded from `DECOMPOSE_AGENT` on the first spawn so a multi-turn Q&A keeps the same backend + args. `last_question_at` stamps the most recent spawn; `question_closed_at` stamps the terminal flip to `done` when the operator closes the issue.
- `children` — parents only; child issue numbers, used by `_handle_blocked`.
- `dep_graph` — parents only; `{child_idx_str: [child_idx, ...]}` because GitHub has no first-class blocks-issue relation.
- `decomposed_at`, `pickup_comment_id`.
- `user_content_hash` — SHA-256 over title + body + non-orchestrator comments; updated whenever the orchestrator reacts to a human edit so future ticks have a stable baseline.
- `branch`, `pr_number`, `review_round`.
- `retry_window_start` + `retry_count` — per-issue 24h fresh-spawn budget; shared between implementing and decomposing.
- `awaiting_human`, `last_action_comment_id`, `park_reason` — generic HITL park triple. `_park_awaiting_human` always sets `awaiting_human=True`, ratchets `last_action_comment_id`, and **clears pinned `park_reason` to `None`** — its own `reason=` argument is recorded only in the emitted `park_awaiting_human` audit event. A handler that needs `park_reason` to survive into the next tick (so the awaiting-human resume can gate on the cause) explicitly re-sets it via `state.set("park_reason", "<tag>")` after the park call; everything else leaves the field cleared and the typed tag lives only in the audit event.

  **Durable pinned `park_reason` values** (re-set after `_park_awaiting_human`, so subsequent ticks read them):
  - `agent_silent` — persisted by `_on_question`'s empty-output branch (which also increments `silent_park_count`). Fires from every stage that calls `_on_question`: directly from `_handle_implementing` (fresh / awaiting-human / inline drift), `_handle_documenting`, and `_handle_implementing`'s drift (which handles drift inline and calls `_on_question` directly, not via the shared helper); and indirectly from `_handle_validating` / `_handle_fixing` (via `_handle_dev_fix_result`), `_handle_in_review` / `_handle_validating` / `_handle_resolving_conflict` drift paths (via `_post_user_content_change_result`), and `_handle_resolving_conflict`'s conflict-resolution funnel (via `_post_conflict_resolution_result`). The non-empty branch of `_on_question` instead clears pinned `park_reason` and emits `agent_question` as audit-event-only.
  - `agent_timeout` / `push_failed` — persisted by the two dev-resume funnels that share a helper, `_handle_dev_fix_result` (validating fix loops, inherited by `_handle_fixing`) and `_post_user_content_change_result` (drift paths in `_handle_validating`, `_handle_in_review`, and `_handle_resolving_conflict`). Also persisted by `_handle_documenting`'s own inline branches. `_handle_resolving_conflict`'s conflict-resolution funnel `_post_conflict_resolution_result` emits the same tags but does NOT persist them (audit-only) — so the drift path and the conflict path differ within the same stage.
  - `_handle_implementing` — `question_unsafe_relabel`.
  - `_handle_documenting` — `fetch_failed`, `worktree_reset_failed`, `diverged_branch` (in addition to the shared `agent_timeout` / `push_failed` above).
  - `_handle_validating` — `review_cap`, `reviewer_timeout`, `reviewer_failed` (the silent-crash branch only — the non-silent `reviewer_no_verdict` branch leaves `park_reason` cleared), `verify_failed`, `verify_timeout`, `verify_dirty`, `verify_head_changed` (all via `_park_verify_failure`).
  - `_handle_in_review` — `unmergeable`.
  - `_handle_question` — `question_timeout`, `question_commits`, `question_dirty`, `question_silent`, `question_answer` (all via `_park_question`).

  **Audit-event-only `reason` tags** (emitted in `park_awaiting_human` but NOT persisted — pinned `park_reason` stays cleared):
  - `_handle_decomposing` — `decomposer_timeout`, `decomposer_dirty`, `decomposer_invalid_manifest`, `decomposer_question`, `decomposer_silent`, `decomposition_crash`, `child_create_failed`, `child_seed_failed`, `retry_cap`.
  - `_handle_implementing` — `agent_timeout`, `agent_question`, `dirty_worktree`, `push_failed`, `retry_cap`, `stale_recovered_work` (the drift-with-recovered-commits branch). Implementing's drift inline path does NOT use `_post_user_content_change_result`, so its `agent_timeout` is audit-only.
  - `_handle_blocked` / `_handle_umbrella` — `blocked_no_children`, `umbrella_no_children`, `child_rejected`, `child_manually_closed`.
  - `_handle_documenting` — `missing_pr_number`, `dirty_worktree`, `agent_question`.
  - `_handle_validating` — `squash_failed`, `reviewer_no_verdict` (non-silent branch).
  - `_handle_in_review` — `missing_pr_number`.
  - `_handle_fixing` — `missing_pr_number`.
  - `_handle_resolving_conflict` — `missing_pr_number`, `conflict_cap`, `fetch_failed`, `diverged_branch`, `dirty_worktree`, `rebase_failed_no_files`, `rebase_in_progress`, `agent_question`, plus `agent_timeout` / `push_failed` *when emitted by `_post_conflict_resolution_result`* (the drift path's same-named tags are durable — see above).

  The same string tag can appear in both lists with different stage origins (`agent_timeout` and `push_failed` are durable when emitted by `_post_user_content_change_result` or `_handle_dev_fix_result`, but audit-only when emitted by `_handle_implementing`'s inline drift or by `_post_conflict_resolution_result`).
- `docs_checked_sha` + `docs_verdict` — set by `_handle_documenting`'s success exits (`docs_verdict` ∈ `{"updated", "no_change"}`). Together they form the in_review ready-ping gate: the ping fires only when `docs_checked_sha == pr.head.sha` AND `docs_verdict` is set, OR a real GitHub APPROVED review covers the current head. Stale handoffs on older commits do not count.
- `ready_ping_sha` — head SHA the in_review handler already posted the one-shot `:bell:` HITL ping for. De-dupes the ping so a long-lived ready PR doesn't spam the handles on every poll, but a new commit (which shifts `pr.head.sha`) triggers a fresh ping after another final-docs handoff or current-head GitHub approval.
- `silent_park_count` — shared dev-session silent-park counter, incremented by `_on_question`'s empty-output branch whenever the agent produces no `last_message`. Bumped from every stage that calls `_on_question` (implementing, documenting, validating/fixing fix loops via `_handle_dev_fix_result`, the implementing/validating/in_review/resolving_conflict drift paths via `_post_user_content_change_result`, and resolving_conflict's post-agent funnel via `_post_conflict_resolution_result`); reset to 0 on any branch that proves the session is alive — a fresh commit lands, an explicit `ACK:` drift acknowledgement is posted, or one of the documenting success exits. Crossing the configured threshold drops the recorded dev session id on the next resume so a poisoned session can be re-spawned from scratch.
- `pr_last_comment_id` — in_review high-watermark across the issue thread + PR conversation comments, which share the IssueComment id space. Seeded at validating's approval branch (before the `documenting` hop, before the `in_review` handoff) so the orchestrator's own automated comments don't replay as fresh feedback, and bumped past any park comment so an HITL ping doesn't replay either. The approval comment and squash comment ride through the final-docs hop untouched; the watermark itself may be **ratcheted** by `_handle_documenting`'s final-docs success exits (via `_ratchet_in_review_watermark_for_final_docs`) past any issue-thread reply the awaiting-human resume consumed, so the next in_review tick does not replay it as fresh PR feedback. The ratchet reuses `_latest_pr_comment_ids` so an unread PR-conversation comment whose id falls below the consumed-through threshold is preserved (the seed walk applies `consumed_through` to the issue-thread surface only).
- `pr_last_review_comment_id` — separate watermark for inline PR review comments, which live in their own id space.
- `pr_last_review_summary_id` — separate watermark in the PullRequestReview id space, distinct from both IssueComment and PullRequestComment ids.

  The watermark *only* advances from review IDs that survived `gh.pr_reviews_after`'s state/body filter — non-empty `CHANGES_REQUESTED` or `COMMENTED` — so `APPROVED`, `DISMISSED`, `PENDING`, and empty-body reviews **never** bump it. `_bump_in_review_watermarks` mirrors the same filter and advances strictly from the filtered list.

  This is safe because the same filter runs on every scan, so an `APPROVED` review id sitting above the watermark is harmlessly re-skipped each tick rather than re-forwarded.
- `docs_drift_unwind_pending` — sentinel set by `_handle_documenting`'s drift block when user-content drift fires mid-final-docs-hop (a title/body edit OR a new human-authored comment that bumps `_compute_user_content_hash`), marking that the issue owes a reconcile + relabel back to `validating`. Persists across every parked cleanup-failure path inside the drift block, so an operator unpark or a fresh human comment re-enters the drift block on the next documenting tick and retries the cleanup. Cleared only on the success path that relabels to `validating`. Without this sentinel, an operator unpark on a parked drift cleanup would fall through to the normal docs-spawn / recovered-commit shortcut and advance to `in_review` against the pre-drift requirements, skipping the required reviewer re-review.
- `pending_fix_at` — ISO timestamp recorded by `_handle_in_review` when it routes fresh PR feedback to `fixing`. Surfaces to operators that the issue is in the `fixing` quiet window or actively being fixed; cleared on a pushed fix (which then bounces directly back to `validating`) or when the rescan finds no unread feedback and bounces directly back to `validating`.
- `pending_fix_issue_max_id` / `pending_fix_review_max_id` / `pending_fix_review_summary_max_id` — per-namespace bookmarks for the PR-feedback ids that triggered the `fixing` route. They are hints for `_handle_fixing` and forensics, NOT watermarks — the in_review watermarks are deliberately left behind so the fixing rescan can re-discover the triggering comments and build the dev-resume prompt. Cleared alongside `pending_fix_at` on the same exits.
- `merged_at` / `closed_without_merge_at` — terminal stamps.
- etc. (see `github.PINNED_STATE_MARKER` / `PINNED_STATE_RE` and `read_pinned_state` / `write_pinned_state`).

The legacy `codex_session_id` key (written before `dev_agent` existed) is still honored on read by `_read_dev_session`: it round-trips to `spec="codex"` with no args so a session pinned by an older orchestrator version keeps running on codex even after `DEV_AGENT` is flipped.

## Stage handlers

### `_handle_pickup` (no label → `decomposing` or `implementing`)
- **Trigger**: open issue with no workflow label.
- **Input**: issue title/body/comments; `config.DECOMPOSE` (default on); `config.ALLOWED_ISSUE_AUTHORS` (default empty → allow all).
- **Action**: when `ALLOWED_ISSUE_AUTHORS` is set, an issue authored by anyone outside the list is silently skipped (log only); otherwise post a "picking this up" comment, anchor `pickup_comment_id`, snapshot `user_content_hash` over title + body + non-orchestrator comments so future ticks can detect a human edit mid-flight, then route:
  - `DECOMPOSE=on` → label `decomposing`, fall into `_handle_decomposing`.
  - `DECOMPOSE=off` → label `implementing`, fall into `_handle_implementing`.

### User-content drift detection
The drift-sensitive handlers — `_handle_decomposing`, `_handle_ready`, `_handle_blocked`, `_handle_umbrella`, `_handle_implementing`, `_handle_validating`, `_handle_documenting`, `_handle_in_review`, and `_handle_resolving_conflict` — run `_detect_user_content_change` somewhere in their flow: it calls `_compute_user_content_hash(issue, orchestrator_comment_ids)` and compares the result to the stored `user_content_hash`. The hash covers the issue title, body, and every human-authored *issue-thread* comment body (PR-conversation comments are not in the hash). Most handlers check at entry; `_handle_in_review` is the exception — it runs the four-surface fresh-feedback ID watermark scan FIRST and routes any unread human comment past those watermarks to `fixing`, so the drift check that follows reacts only to content changes the ID scan didn't catch: title/body edits, and edits to an existing issue-thread comment body whose id is already below the watermark (the fixing route is ID-only, so it misses comment edits). Edits to existing PR-conversation comment bodies fall in a gap: the drift hash doesn't include them and the fixing watermarks don't see them either.

`_handle_fixing` and `_handle_question` deliberately do NOT run this drift check. `_handle_fixing` instead refreshes `user_content_hash` to the post-resume value once it has consumed the PR-side feedback that triggered the route, so the next drift-sensitive tick treats those just-consumed comments as the new baseline rather than re-firing the drift path against them. `_handle_question` runs its own conversation flow and does not interact with the drift state at all.

Non-human content is filtered four ways:

- pinned-state comments by `PINNED_STATE_MARKER`;
- orchestrator-posted comments by `_ORCH_COMMENT_MARKER` embedded in the body (every `_post_issue_comment` / `_post_pr_comment` wraps the body via `_with_orch_marker`; the marker is an HTML comment, invisible in rendered Markdown, and survives id-cap eviction on long-lived issues);
- legacy orchestrator comments by id from `orchestrator_comment_ids` (covers comments posted before the marker was introduced, until their id is evicted from the bounded cap);
- third-party Bot/App accounts (Dependabot, Renovate, CI bots) by GitHub's `user.type == "Bot"` structural flag (a periodic dependency-bump comment would otherwise re-trigger drift on every tick it posts).

Author-login matching is intentionally avoided because the orchestrator PAT is often shared with a human reviewer's GitHub account; the `user.type` flag is a structural account property and does not conflict with that constraint. So the hash drifts on body edits AND on new human comments (acceptance criteria added mid-flight).

`_detect_user_content_change` durably persists the baseline on its FIRST encounter via `gh.write_pinned_state` so an early-return tick (awaiting-human-with-no-new-comments, child-waiting-on-deps, debounce) cannot silently absorb a later edit as the new baseline. On drift the action depends on where in the lifecycle the issue is:
  - `decomposing` → handled INLINE at the top of `_handle_decomposing` (not via `_route_drift_to_decomposing`, which would no-op since the label is already `decomposing`): drop `decomposer_session_id`, wipe the manifest tracking (`children`, `dep_graph`, `expected_children_count`, `umbrella` flag), clear park flags, post a `:pencil2: issue content changed` notice on the issue, then FALL THROUGH in the same tick — the wiped manifest causes the half-finished recovery branch to no-op, and the awaiting-human / fresh-spawn branch below runs the decomposer against the updated body on this tick (no extra label hop, no extra tick of latency).
  - `ready` / `blocked` / `umbrella` (no implementation has started yet, label is NOT already `decomposing`) → route back to `decomposing` via `_route_drift_to_decomposing`: same state-wipe + notice as above, plus an explicit label flip to `decomposing` so the next tick re-enters `_handle_decomposing`.

    Crucially, `decomposer_agent` is PRESERVED across this transition: lock-on-first-spawn means the recorded role spec stays locked for the rest of the issue's lifecycle, even across drift events, so a mid-flight `DECOMPOSE_AGENT` env flip cannot retarget an in-flight issue at a different backend (the fresh spawn picks up the recorded spec via `_read_decomposer_session`).

    For parents with previously-tracked children (in-flight in `blocked`, all-done after the `blocked` -> `ready` transition, or any state for `umbrella`), the child issue numbers are listed in the notice as ORPHANED — the orchestrator no longer tracks them, so the operator must close any that no longer apply.

    Wiping the manifest tracking is what stops `_handle_decomposing`'s half-finished recovery branch from firing on the next tick (it keys on `expected_children_count is not None OR children non-empty`); without it a `ready` parent whose children all finished would loop back to `blocked` without ever re-running the decomposer.

    This is deliberately destructive over "park awaiting human" because silently absorbing a child edit would let `_handle_ready` later see the new baseline as already consumed and skip the re-decomposer even when the edited child now needs splitting; and an edited umbrella with done children would close to `done` against the stale manifest.
  - `implementing` / `validating` / `in_review` / `resolving_conflict` (a dev session exists and possibly a PR) → on drift the handler:
    1. posts a `:pencil2: issue body changed; resuming dev session` notice — on the issue for implementing/validating, on the PR conversation for in_review and resolving_conflict;
    2. advances `last_action_comment_id` past every visible issue-thread comment via `_mark_drift_comments_consumed`, and bumps the in_review watermarks via `_bump_in_review_watermarks` in the in_review case;
    3. resumes the locked dev session with `_build_user_content_change_prompt`, which quotes the updated title, body, AND the current conversation so a new acceptance criterion posted as a comment is surfaced to the dev;
    4. routes the result through `_post_user_content_change_result`.

    Result routing in `_post_user_content_change_result`:

    - a clean pushed fix hands straight back to `validating` from every stage that runs the drift resume — `in_review`, `resolving_conflict`, and `validating` (the last bumps `review_round`). All three deliberately skip the `documenting` hop -- the single docs pass is deferred to the post-approval hop, so the reviewer re-runs against the new branch directly. From `implementing` the drift path runs the `_on_commits` path to open/push the PR (which also relabels straight to `validating` now);
    - a no-commit reply is treated as an ack ONLY when it carries the explicit `ACK: <reason>` marker the resume prompt instructs the dev to emit when the existing work already satisfies the edit.

      The dev's justification is posted on the issue as an FYI and the handler does NOT park awaiting_human, so a harmless clarification doesn't stall the issue;
    - any other no-commit response (a real clarification question, an ambiguous comment, an empty message) falls back to `_on_question` and parks awaiting human.

      Without the explicit marker requirement, a clarification question would be silently swallowed as "existing work satisfies" and the issue would advance with `awaiting_human=False`, stranding the question.

    The watermark advance is what prevents the validating → in_review handoff from later replaying the same human comment via `_seed_watermark_past_self` and triggering a duplicate dev resume.

    Per-stage specifics:

    - For the `in_review` drift specifically, BOTH the "pushed" and "ack" outcomes reset `review_round` -- a content drift invalidates the prior reviewer approval (it was for the old requirements), so the next round must run on the updated body/comments. Both outcomes also share the same destination: a DIRECT bounce back to `validating`. Docs do not run here -- the single docs pass is deferred to the final-docs handoff after reviewer approval.

      The in_review drift also captures unread PR-conversation comments past `pr_last_comment_id` BEFORE posting the orchestrator's notice and includes them in the dev's followup prompt — issue thread and PR conversation share the IssueComment id space, so an unread PR comment whose id falls between the prior watermark and the issue-thread max would otherwise be silently consumed by `_bump_in_review_watermarks` (which advances the shared watermark based on `latest_comment_id(issue)`) and never forwarded.
    - For the `resolving_conflict` drift specifically, ONLY the "pushed" outcome relabels back to `validating` (with `review_round=0` and `conflict_round` bumped). The "ack" and "parked" outcomes leave the issue still labeled `resolving_conflict` — the rebase work is unfinished, so the next tick re-enters `_handle_resolving_conflict` to keep trying. This is unlike `in_review` drift (where both "pushed" and "ack" bounce to `validating`); the asymmetry is intentional because a content drift mid-rebase doesn't satisfy the rebase itself.
    - For `implementing` specifically, the drift path only resumes the dev session when a `dev_session_id` is already recorded.

      When there is NO dev session but the worktree carries recovered unpushed commits, the handler parks awaiting human rather than falling through to the recovered-worktree shortcut — those commits were authored before the edit and pushing them would publish a PR no agent ever read against the new requirements.

      When there is no dev session AND no recovered commits AND the issue is `awaiting_human` (manual relabel, drift on a freshly-picked-up issue parked before its first spawn), the handler explicitly clears the park flags so the fresh-spawn branch fires this tick with the full implement prompt (which quotes the current `issue.body` and the conversation via `_recent_comments_text`).

      Without this clear, the awaiting-human branch would route to `_resume_developer_on_human_reply` and either return without writing the new hash (looping the drift) or fresh-spawn with only the new-comment text instead of the body-and-conversation context.
    - For `validating` specifically, drift handling DEFERS to the awaiting-human branch when `park_reason` is reviewer-side (`reviewer_timeout` / `reviewer_failed`): a human "retry" comment after a reviewer failure must re-spawn the REVIEWER, not the dev (the failure produced no review output for the dev to act on, and the reviewer naturally re-reads the updated body/comments via `_build_review_prompt`).

      The new baseline is still persisted in the defer branch so the next tick doesn't loop.
    - For `decomposing` specifically, the drift check is the FIRST thing the handler does (before half-finished recovery), and it wipes the manifest tracking (children, dep_graph, expected_children_count, umbrella flag) so the recovery branch is bypassed and the fresh-spawn path re-derives against the new body — without this ordering, a crash-window edit would finalize to `blocked` / `umbrella` against a stale manifest.

      The "don't re-decompose mid-implementation" rule is enforced here: re-decomposing would discard the dev's already-pushed work.

The hash is re-persisted on every reaction so a single edit triggers exactly one re-route, not a loop.

### `_handle_decomposing` (label `decomposing`)
- **Trigger**: each tick while the label is `decomposing`.
- **Input**: issue + comments + pinned state (`decomposer_agent`/`decomposer_session_id`, retry-budget keys).
- **Internal flow** (the handler runs these in order — pre-flight checks first, then the spawn flow):
  1. **User-content drift check** (inline). If `_detect_user_content_change` returns a new hash, post the `:pencil2: issue content changed` notice (listing any previously-tracked children as ORPHANED), drop `decomposer_session_id`, wipe `children` / `dep_graph` / `expected_children_count` / `umbrella`, clear park flags, then **fall through in the same tick**. The wiped manifest causes step 2 to no-op, and step 4 spawns the decomposer against the updated body without an extra label hop.
  2. **Half-finished decomposition recovery.** If `expected_children_count` is set OR `children` is non-empty (markers of a prior tick that crashed mid-split), the handler cannot safely respawn the decomposer (re-running would emit a different manifest and create duplicate children alongside the orphans on GitHub):
     - When `expected_children_count` is set and `len(children) < expected_children_count`, park with `reason="decomposition_crash"` and return.
     - Otherwise, repair any child whose pinned `parent_number` was never seeded, then finalize the parent to `umbrella` (when the persisted `umbrella` flag is true) or `blocked`. The matching handler activates children on the next tick.
     - If already `awaiting_human` (the parent was parked mid-creation), hold without writing state and require manual intervention.
  3. **DECOMPOSE kill switch.** If `config.DECOMPOSE` is off when this handler runs (operator restarted with the kill switch flipped while the issue was already labeled `decomposing` or parked there), bail before any decomposer spawn: post a routing comment, clear the decomposer-side `awaiting_human` / `park_reason`, ratchet `last_action_comment_id` past every visible comment, flip the label to `implementing`, and fall into `_handle_implementing`. Step 2 runs first and is unaffected — abandoning orphan children on GitHub just because new decompositions are now disabled is not what a kill switch should do.
  4. **Awaiting-human resume OR fresh spawn.**
     - If `awaiting_human`: re-check for new human comments since `last_action_comment_id`; if any, resume the decomposer session via `run_agent(decomposer_agent, ...)` with that text. If no new comments, return (keeping the worktree on disk if the prior park was a read-only-violation `keep_worktree` case). See [`workflow.md`](workflow.md#in-flight-session-lock--pinned-full-spec-until-the-session-ends) for spec-format / lock-on-first-spawn details.
     - Otherwise: gate on the **per-issue retry budget** (shared with `implementing`). If exhausted, park awaiting human and return. Then ensure a per-issue worktree (read-only — the decomposer never commits, but the agent wants `git ls-files` / `wc -l` context), resolve the spec via `_read_decomposer_session(state)` (falling back to current config only for the first-ever spawn), persist the raw full spec to `decomposer_agent` BEFORE invoking `run_agent`, build the prompt (issue body + recent comments + sizing rule of thumb + manifest schema), then spawn via `run_agent(decomposer_backend, prompt, wt, extra_args=decomposer_args)`. On a new session id, also persist `decomposer_session_id`.
  5. **Read-only check**: if the worktree now has new commits or dirty files, park awaiting human and KEEP the worktree so the operator can inspect. The decomposer is supposed to be read-only; without this guard, `_handle_implementing`'s recovery path would later see leftover commits and push decomposer-authored work as implementation.
  6. **Parse the manifest** from `result.last_message` via `_parse_manifest` (regex captures the fenced ` ```orchestrator-manifest ` block; structural validation rejects unknown decisions, bad child shape, self-deps, cycles, and >10 children):
     - **invalid manifest** → park with the parse error and the agent's last message quoted.
     - **no fenced block** → treat as a question; park with the message quoted (mirrors `_on_question`).
     - **decision == "single"** → post a one-line "fits in one context" comment with the rationale, set label `ready`, stamp `decomposed_at`. `_handle_ready` picks it up next tick.
     - **decision == "split"** → crash-safe creation in three phases (docs stay current without a synthetic docs-update child; `_handle_validating`'s approval branch runs the single final-docs pass on the squashed head):
       1. For each child call `gh.create_child_issue(...)` with label `blocked` regardless of dependencies, and seed the child's pinned state with `parent_number`. `create_child_issue` prepends `Parent: #<n>` to the body. Child-state seeding is mandatory — failure persists the partial `children` list and parks awaiting human, so no orphan child is left runnable.
       2. Persist `children`, `dep_graph`, and `umbrella` on the parent. Post the summary comment, set parent label `umbrella` (when the flag is true) or `blocked`, stamp `decomposed_at`.
       3. Activate no-dep children by flipping their label `blocked` → `ready`. Best-effort because `_handle_blocked` / `_handle_umbrella` also treats no-dep children as deps-satisfied, so a crashed activation step is recovered on the next tick.
- **Output**: parent label moved to `ready` / `blocked` / `umbrella` / `implementing` (kill switch), OR a HITL park.

### `_handle_ready` (label `ready` → `implementing`)
- **Trigger**: each tick while the label is `ready`. Reached by either a `single`-decision parent or by a freshly-created child.
- **Action**: if `pickup_comment_id` is unset (the common path for auto-created children), post a "picking this up; starting implementation" comment and seed `created_at` + `pickup_comment_id`.

  Bump `last_action_comment_id` to the latest visible comment id (one-way ratchet) so any human comments posted while the parent was `decomposing` / `blocked` are marked consumed — the implementer reads them at spawn via `_recent_comments_text`, so they must NOT later resurface as fresh PR feedback in `_handle_in_review`'s watermark seed (which would bounce the PR back to validating after merge readiness).

  Then flip the label to `implementing` and fall through into `_handle_implementing` on the same tick.

### `_handle_blocked` (label `blocked`)
- **Trigger**: each tick while the label is `blocked`.
- **Input**: pinned `children` (parent only), optional `dep_graph` (parent only — `{child_idx_str: [child_idx, ...]}`), `parent_number` (child only — seeded by the decomposer at child-creation time).
- **Internal flow**:
  1. If no `children` recorded but `parent_number` is set → no-op. The parent's `_handle_blocked` walks the dep graph and flips this child to `ready` when its dependencies finish; this tick has nothing to do.
  2. If no `children` and no `parent_number` (manual relabel suspected), park awaiting human.
  3. Read each child's current workflow label via `gh.get_issue(n)` + `gh.workflow_label(child)`.
  4. If any child is `rejected` → park parent awaiting human (the human decides whether to re-decompose or close).
  5. If any child is closed (`state=="closed"`) but its label is not `done`, `rejected`, or `in_review` → retry `_finalize_if_pr_merged(gh, spec, child, child_state)` against each such child before parking. If the child's pinned `pr_number` resolves to a merged PR, the helper flips the child to `done` (with the same `merged_at` stamp / `pr_merged` event / terminal cleanup as the per-stage finalize) and the parent treats that child as `done` for the aggregation. Only children whose PR is not merged (or who have no pinned `pr_number`) fall through to the manually-closed park.

     This finalize-on-poll is the parent's defense-in-depth for an externally-merged child whose own handler has not yet finalized: a sibling fan-out worker may still be queued behind the family bucket on the same tick, so the parent reads the stale pre-merge label first. For closed children labeled with any of the seven sweep labels, the child's own entry-time `_finalize_if_pr_merged` / `_finalize_if_issue_closed` chain flips the child to `done` or `rejected` on the same or next tick anyway — so the parent's manually-closed park here is mainly the catch for closed children at pre-PR labels (`decomposing` / `ready` / `blocked` / `umbrella`) which are NOT swept and would otherwise stay frozen. `in_review` is intentionally allowed: a state=closed/label=`in_review` child is the externally-merged transient that the closed-`in_review` sweep finalizes on the next tick.
  6. If every child is `done` → post a summary comment, flip parent → `ready`. The next tick `_handle_ready` picks it up and the implementer takes over.
  7. Otherwise walk children: any `blocked` child whose recorded dependencies are all `done` gets relabeled `ready`. A child with no recorded deps is also flipped (vacuous all-done over an empty list) — this recovers no-dep children that the decomposer's same-tick activation step left as `blocked`.

     This walk both unblocks middle-of-the-graph children and rescues stuck activations without waiting on the parent.
- **Output**: parent → `ready` (all done), OR a sibling unblocked, OR a HITL park (rejected child, manually-closed child, or unattributed `blocked`), OR a no-op for a child still waiting on its dependencies.

### `_handle_umbrella` (label `umbrella`)
- **Trigger**: each tick while the label is `umbrella` (only ever a parent — set by the decomposer when the manifest's `umbrella` boolean is true).
- **Input**: pinned `children` and optional `dep_graph` on the parent.
- **Internal flow**: mirrors `_handle_blocked` for the rejected / manually-closed checks and the dep-graph activation walk; the only difference is the all-done terminal. The same `_finalize_if_pr_merged` recovery for `manually_closed` children runs here, so an externally-merged child whose label never advanced past an in-flight stage does not strand the umbrella aggregation.

  An umbrella parent has no implementation work of its own — its purpose is purely aggregation — so when every child reaches `done`, the handler posts a checkmark comment, stamps `umbrella_resolved_at`, sets label `done`, and closes the issue (no flip back through `ready`/`implementing`).

  A `children`-less umbrella is treated as corrupt state and parks awaiting human.
- **Output**: terminal `done` (all children resolved, issue closed), OR a sibling unblocked, OR a HITL park, OR a no-op.

### `_handle_implementing` (label `implementing`)
- **Trigger**: each tick while the label is `implementing`.
- **Input**: issue + comments + pinned state (`dev_agent`/`dev_session_id`, retry-budget keys, etc.).
- **Internal flow**:
  0. **External-merge short-circuit.** Before any dev work runs, the handler calls `_finalize_if_pr_merged(gh, spec, issue, state)`: when a `pr_number` is pinned and the PR has already merged (an operator cherry-picked the change, or a sibling branch landed and the human merged this PR early), the helper stamps `merged_at`, flips the label to `done`, emits `pr_merged` with `merge_method="external"`, closes the issue if it is still open, and runs `_cleanup_terminal_branch`. The handler then returns — the dev session is not resumed, no fresh spawn fires, and the retry budget is untouched.

     Immediately after that returns False, the handler also calls `_finalize_if_issue_closed(gh, spec, issue, state)`: now that the closed-issue sweep yields closed-`implementing` issues, a human-closed issue must NOT spawn the dev agent against a closed thread. The helper stamps `closed_without_merge_at`, flips the label to `rejected`, and writes pinned state; it then emits `pr_closed_without_merge` + runs `_cleanup_terminal_branch` only when the linked PR is also closed (an open PR with a manually-closed issue is left alone so the operator can salvage / reopen it, mirroring the in_review / fixing arc; a closed implementing issue with no `pr_number` flips to `rejected` without emitting the PR event or touching the branch).

     When the pinned PR cannot be fetched, the helper defers without writing any state — `_finalize_if_pr_merged` returns False on BOTH "not merged" and "could not fetch PR", so flipping to `rejected` without a successful fetch here would permanently terminal-label a merged-PR issue whose merge finalize hit a transient GitHub / network failure. The same deferral fires when the closed-issue helper's own fetch reveals the PR IS actually merged (the prior merged finalize raced with a real fetch failure): rather than incorrectly mis-labeling a merged-PR issue as `rejected`, the helper returns True without state changes so the caller stops at the closed-issue guard this tick and the next tick re-runs `_finalize_if_pr_merged` against a fresh PR state.
  1. If `awaiting_human`: re-check for new human comments since `last_action_comment_id`; if any, **resume** the dev session via `run_agent(dev_agent, ...)` with that text. If no new comments, return.

     The full spec persisted in `dev_agent` — backend AND configured CLI args — is re-parsed via `_read_dev_session` and reused for the resume; flipping `DEV_AGENT` in env does not migrate in-flight issues. See [`workflow.md`](workflow.md#in-flight-session-lock--pinned-full-spec-until-the-session-ends) for the spec-format and backward-compat details.
  2. Otherwise: ensure a per-issue worktree at `<WORKTREES_DIR>/<owner>__<name>/issue-<n>` (the slug subdir keeps two repos with the same issue number isolated on disk) on branch `orchestrator/issue-<n>`. Worktrees with unpushed commits are reused (crash recovery); otherwise force-removed and recreated from `<spec.remote_name>/<spec.base_branch>` in `spec.target_root`.
  3. If the worktree already has commits (recovered), skip the agent and go straight to push.
  4. Else gate the run on the **per-issue retry budget** (`MAX_RETRIES_PER_DAY`, default 3): a 24h window opens at the first counted spawn and resets after 24h; only fresh spawns count, not human-resume runs or recovered-worktree pushes. If the cap is exhausted, park awaiting human and return.
  5. Else build the **implementer prompt** (issue body + recent comments + "commit, do not push").

     Resolve the spec for this issue via `_read_dev_session(state)`, falling back to the current config only for the first-ever spawn. Persist the raw full spec to `dev_agent` BEFORE invoking `run_agent` (see [`workflow.md`](workflow.md#in-flight-session-lock--pinned-full-spec-until-the-session-ends) for the lock-on-first-spawn rationale), then spawn via `run_agent(dev_backend, prompt, wt, extra_args=dev_args)`. On a new session id, also persist `dev_session_id`.
  6. Branch on result:
     - `timed_out` → park awaiting human (`@HITL_HANDLE`).
     - new commits + clean tree → `_on_commits`: push branch, open PR (or reuse an existing open one), comment `:sparkles: PR opened: #N`, set label `validating` (the docs pass only runs as the final-docs handoff after the reviewer approves), reset `review_round=0` and `retry_count=0` (next bounce back into implementing starts fresh).
     - new commits + dirty files → `_on_dirty_worktree`: park; refuse to publish a partial branch.
     - no new commits → `_on_question`: post the agent's last message as a HITL question, park.
- **Output**: a pushed branch + open PR + label moved to `validating` (the reviewer runs on the next tick), OR a HITL park.

### `_handle_documenting` (label `documenting`)
- **Trigger**: each tick while the label is `documenting`. Set only by the **final-docs handoff** in `_handle_validating`'s approval branch (after verify + squash); the docs pass runs exactly once per reviewer-approval handoff, between reviewer approval and `in_review`. A PR may visit `documenting` more than once over its life: if PR feedback later bounces the issue to `fixing` and the dev pushes a fix, the next reviewer approval triggers another final-docs pass. Also runs on closed-`documenting` issues yielded by the polling sweep so an externally-merged PR finalizes to `done`.
- **Input**: pinned `pr_number`, `branch`, `dev_agent`/`dev_session_id` (the docs pass runs AS the dev role and reuses the same locked spec / session id — there is no separate `documenting_agent`), plus `docs_checked_sha` / `docs_verdict` / `silent_park_count` watermarks. The `documenting` label itself is the handoff signal.
- **Internal flow**:
  0. **External-merge / closed-issue short-circuit.** Identical to the implementing / validating entry checks: `_finalize_if_pr_merged` flips a merged PR to `done`; `_finalize_if_issue_closed` flips a closed issue to `rejected` (only emitting `pr_closed_without_merge` + running `_cleanup_terminal_branch` when the linked PR is also closed). The same fetch-failure / merged-PR deferral the implementing handler relies on applies here.
  1. **`pr_number` missing → park.** Documenting only runs against an existing PR worktree; without a pinned `pr_number` the handler cannot anchor on the dev's branch (branching off base would orphan the docs commit from the implementing PR). Park awaiting human with `park_reason="missing_pr_number"` and tell the operator to relabel back to `implementing`. Idempotent under `awaiting_human` — a no-reply re-tick returns without re-posting.
  2. **User-content drift → relabel back to `validating`.** `_detect_user_content_change` hashes title + body + human-authored comments, so any of those changing during the final-docs hop (a title/body edit OR a fresh human comment on the issue) invalidates the prior approval — the reviewer voted on stale requirements. The docs agent is NOT spawned; the reviewer must re-evaluate the updated content on the next tick before any docs work could land. Mirrors `_handle_in_review`'s drift handling (route directly back to `validating` with `review_round` reset).

     Housekeeping on entry: post a `:pencil2: issue body changed; routing back to validating` notice on the issue, advance `last_action_comment_id` past every visible issue-thread comment via `_mark_drift_comments_consumed`, refresh `user_content_hash`, clear `awaiting_human` / `park_reason`, and reset `review_round=0`. The round counter is cleared BEFORE any fallible step so a park below leaves no stale counter an operator unpark / manual relabel could ride into a fresh final-docs handoff.

     **Reconcile the PR worktree** so the next reviewer round runs against the actual remote PR head and no docs work authored against the pre-drift requirements survives. When the worktree exists on disk:
     - Fetch `<remote>/<branch>`; park with `park_reason="fetch_failed"` on failure.
     - Probe HEAD vs. the freshly-fetched ref inline (the shared `_branch_ahead_behind` helper swallows git errors as `(0, 0)`, so the probe runs inline here to distinguish a probe failure from a real "in sync" result).
     - When `ahead > 0`, `behind > 0`, OR `_worktree_dirty_files` reports any modified-tracked / untracked path, run `git reset --hard <remote>/<branch>` followed by `git clean -fd`. The reset moves HEAD to the remote PR head and discards local commits + modified-tracked files; the clean removes untracked files / directories `reset --hard` leaves behind (e.g. a new under-`docs/` subdir the prior docs agent never committed).
     - Park with `park_reason="worktree_reset_failed"` on inline-probe / `reset --hard` / `clean -fd` failure.

     The reconcile is what stops a future final-docs hop's recovered-commit shortcut from silently pushing a stale commit, stops the next reviewer round from `git diff`ing against an un-fetched stale local HEAD, and stops a prior dirty-park's edits from riding into the next reviewer round.

     The `docs_drift_unwind_pending` sentinel is set while a cleanup is in progress and cleared only on the success path that relabels to `validating`. On a parked cleanup, an operator unpark or a fresh human comment re-enters this drift block on the next tick to retry the reconcile + relabel — an unpark cannot fall through to a docs spawn or recovered-commit shortcut and skip the required `validating` re-review. When the sentinel is set, parked, and no new human input has arrived, the handler returns silently to avoid re-posting the park comment every tick.
  3. **Awaiting-human + no new comment → early return.** When `awaiting_human` is set and no human reply has arrived since `last_action_comment_id`, return BEFORE the fetch + ahead/behind check. Otherwise a transient `fetch_failed` / `diverged_branch` failure would re-post its park comment every tick.
  4. **Ensure the PR worktree** (`_ensure_pr_worktree`, restored from `<remote>/<branch>` so the dev's commits are intact) and refresh the remote-tracking ref via `_authed_fetch` BEFORE the ahead/behind check. A fetch failure parks with `park_reason="fetch_failed"`.
  5. **Ahead/behind check** vs. the just-fetched `<remote>/<branch>`:
     - `behind > 0` (worktree diverged) → park with `park_reason="diverged_branch"`. Force-pushing local state would clobber the real PR head.
     - `ahead > 0` recovered commits → synthesize an `AgentResult` and skip the agent spawn; the unified commit/dirty/push branch below pushes the recovered docs commit. A drift event this tick would have routed back to `validating` above before this branch is reached, so the recovered commit is always against the still-valid approved body.
     - `(0, 0)` in sync → fall through to fresh spawn (or awaiting-human resume).
  6. **Awaiting-human resume.** A `fetch_failed` / `agent_timeout` / `agent_silent` resume may be the FIRST time this session sees the docs-stage instructions (the `DOCS: NO_CHANGE` marker, what files to inspect, what to commit), so the resume rebuilds the **full** docs prompt via `_build_documentation_prompt` rather than `_resume_developer_on_human_reply`'s new-comments-only shape. Advance `last_action_comment_id` past every just-read human comment, snapshot `before_sha` from the fetched worktree, persist `docs_checked_sha=before_sha` BEFORE the spawn, then `_resume_dev_with_text`.
  7. **Fresh spawn.** Snapshot `before_sha`, persist `docs_checked_sha=before_sha` and the locked `dev_agent` spec BEFORE invoking the agent, build the docs prompt (issue body + recent comments + `DOCS: NO_CHANGE` marker contract), then `_run_agent_tracked` with `agent_role="developer"` / `stage="documenting"`.
  8. Branch on the post-agent state. Every success exit calls one of two helpers (`_advance_after_docs_push` / `_advance_after_docs_no_change`) that route to **`in_review`** and ratchet `pr_last_comment_id` via `_ratchet_in_review_watermark_for_final_docs` past any issue-thread reply the awaiting-human resume consumed so the next in_review tick does not bounce the issue to `fixing` over already-addressed feedback. Branches:
     - `timed_out` → park with `park_reason="agent_timeout"` (transient: dashboards / a later tick may re-spawn).
     - dirty worktree (regardless of whether a commit also landed) → `_on_dirty_worktree`: park; refuse to publish a partial branch or silently drop edits.
     - new commit on a clean tree → `_push_branch` (with the same hardened path the implementing push uses). On success record `docs_checked_sha=after_sha`, `docs_verdict="updated"`, reset `silent_park_count=0`, post `:books: documenting pass: pushed docs commit.` (or the recovered-commit variant) to the PR, then `_advance_after_docs_push()`. A push failure parks with `park_reason="push_failed"`.
     - no commit + `DOCS: NO_CHANGE` verdict:
       - if `ahead > 0` (a prior tick committed but never landed the push), push the recovered commit and advance via `_advance_after_docs_push()` — the local-only commit can't be left behind on the dev's worktree. A push failure parks with `push_failed`.
       - otherwise persist `docs_checked_sha=after_sha`, `docs_verdict="no_change"`, reset `silent_park_count=0`, post `:books: documenting pass: no docs changes required.` (with the dev's justification quoted when present), then `_advance_after_docs_no_change()` — no commit landed, PR head unchanged.
     - no commit + unknown verdict → `_on_question`: post the agent's last message as a HITL question, park (the helper distinguishes the silent-crash case via stderr diagnostics and tags `silent_park_count` so a poisoned session is dropped on the next resume).
- **Output**: label moved to `in_review` (pushed docs commit OR no-change verdict) OR label moved to `validating` (drift unwind: body edit invalidated the prior approval, no docs spawn this tick) OR terminal `done`/`rejected` (external-merge / closed-issue short-circuit) OR a HITL park (`missing_pr_number`, `fetch_failed`, `diverged_branch`, `worktree_reset_failed`, `agent_timeout`, `push_failed`, `dirty_worktree`, `agent_question`, `agent_silent`).


The docs pass is deliberately a thin dev-session rerun on the existing PR worktree rather than a separate role: there is no `documenting_agent` pin and no separate retry budget. The dev session resumes on its locked `(backend, args)` spec, so `DEV_AGENT` flips made mid-flight do not retarget the docs pass either.

### `_handle_validating` (label `validating`)
- **Trigger**: each tick while label is `validating` (set by `_handle_implementing` after `_on_commits` opens the PR — straight handoff, no pre-review docs hop — by `_handle_documenting`'s drift unwind when a body edit invalidates the prior approval, and by `_handle_fixing` / `_handle_in_review`'s drift exits / `_handle_resolving_conflict`'s pushed exits — every pre-approval push routes here so the reviewer sees the new branch directly).
- **Input**: PR #, branch, `dev_agent`/`dev_session_id`, pinned state, `review_round`.
- **Internal flow**:
  0. **External-merge / closed-issue short-circuit.** Identical to the implementing / documenting entry checks. Two helpers chain:
     - `_finalize_if_pr_merged` flips the label to `done` and runs `_cleanup_terminal_branch` when the PR was merged externally while the reviewer was queued.
     - `_finalize_if_issue_closed` then flips a closed-`validating` issue to `rejected` (operator rejected mid-review, or the linked PR closed without merge). The closed-PR variant emits `pr_closed_without_merge` + runs `_cleanup_terminal_branch`; the open-PR variant leaves the branch alone for operator salvage.

     The reviewer is not spawned on either short-circuit. When the linked PR's state cannot be confirmed, the closed-issue helper returns True without state changes, so the reviewer does not spawn against a closed issue this tick and the next tick re-attempts `_finalize_if_pr_merged` against a fresh PR fetch.
  1. Awaiting-human path: same resume mechanic as implementing (resume on the dev's locked spec (backend + args)); on a successful pushed fix, bump `review_round` and stay on `validating` — no label flip emitted — so the reviewer re-evaluates the new head on the next tick. A no-commit / ACK reply keeps the issue on `validating`.

     Exception: on a `review_cap` park (`park_reason="review_cap"`), the human reply does **not** wake the dev session — resuming would just bump past the cap on the next tick. Instead, the operator must post `/orchestrator add-review-rounds N` on its own line; that resets `review_round` to `MAX_REVIEW_ROUNDS - N`, clears the park flags, and falls through to spawn the reviewer this same tick. A plain reply (or one with an invalid `N`) leaves the issue parked.
  2. If `review_round >= MAX_REVIEW_ROUNDS` (default 3), park awaiting human. The park comment surfaces the `/orchestrator add-review-rounds N` escape hatch so the operator can grant more rounds without losing the PR/worktree.
  3. Otherwise persist `config.REVIEW_AGENT_SPEC` to `review_agent` for traceability (the reviewer is spawned **fresh each round** with no resume, so the current config spec wins each time — see [`workflow.md`](workflow.md#in-flight-session-lock--pinned-full-spec-until-the-session-ends)).

     Then spawn a **fresh reviewer session** via `run_agent(config.REVIEW_AGENT, review_prompt, wt, timeout=config.REVIEW_TIMEOUT, extra_args=config.REVIEW_AGENT_ARGS)` with the **reviewer prompt** (read-only: `git log` / `git diff <spec.remote_name>/<spec.base_branch>...HEAD`, must end with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`).
  4. Parse last `VERDICT:` marker (`_parse_review_verdict`):
     - `approved` → in this order:
       1. **Local-verify gate.** Run `_run_verify_commands(wt, config.VERIFY_COMMANDS, config.VERIFY_TIMEOUT)` in the per-issue worktree. A default-empty `VERIFY_COMMANDS` short-circuits to `status="ok"`.

          Any non-ok result parks the issue on `validating` via `_park_verify_failure` with a typed `park_reason` (`verify_failed`, `verify_timeout`, `verify_dirty`, or `verify_head_changed`) and a park comment that names the failing command, its exit code (or timeout), and a redacted / truncated tail of the captured output. The approval comment, squash, watermark seeding, and `in_review` handoff do **not** fire.

          The verify gate is the first gate after the reviewer agent — it catches regressions locally so an obviously-broken branch never reaches `in_review`. GitHub CI still runs against the PR; the human merging the PR is the consumer of CI's verdict, since the orchestrator is permanently manual-merge-only from `in_review`. See [`configuration.md#local-verification-gate`](configuration.md#local-verification-gate) for the env-var reference and per-`park_reason` semantics.
       2. Post `:white_check_mark: codex review approved.` on the PR (so the comment exists even when squash later fails).
       3. When `SQUASH_ON_APPROVAL` is on (default), call `_squash_and_force_push` to collapse the dev's commits into one. Subject reuses the first commit when already conventional-commit-shaped, otherwise `feat: <issue title>`; body lists the original subjects; pushed with `--force-with-lease` against the pre-squash SHA.

          On squash or force-push failure, **park awaiting human and stay on `validating`** (no relabel) so the original commits remain on the branch for manual triage — the approval comment has already landed on the PR.
       4. On success, if `squashed_count > 1` post `:package: squashed N commits to 1 after approval` to the PR before seeding the in_review watermarks, so the seed walks past it.
       5. Seed the in_review comment watermarks (inside the `else` arm of the `gh.get_pr()` try so a snapshot failure leaves them untouched). THEN, outside the try, relabel to `documenting`. `_handle_documenting`'s success exits always advance to `in_review` and ratchet `pr_last_comment_id` past any issue-thread reply consumed by the awaiting-human resume so the in_review tick does not bounce the issue to `fixing` over already-addressed feedback. The approval comment and squash comment seeded here ride through the documenting hop untouched.
     - `unknown` (no marker) → park.
     - `changes_requested` → post the feedback to the PR, then **flip the label to `fixing` BEFORE spawning the dev** so the active job is observably "fixing reviewer-requested changes" rather than `validating` (which is now reviewer/verify work only). Resume the developer's session on its locked spec (backend + args) with the fix prompt; if it produces a new commit on a clean tree, push, **bump `review_round`, and flip the label back to `validating`** so the reviewer re-evaluates the new head on the next tick. The dev spawn records `stage="fixing"` for analytics so the fix-loop spend is attributed to `fixing` rather than `validating`. On any park (timeout, no-commit, dirty tree, push failure) the label **stays on `fixing`** with `awaiting_human=True` and `_handle_fixing` owns the awaiting-human rescan + dev resume cycle. The fixing handler's filter drops the orchestrator's own reviewer-feedback PR comment and park comment, so an awaiting_human=True tick with no fresh human reply returns silently rather than bouncing back to `validating`. On a fresh human reply, the fixing handler resumes the dev via `_resume_dev_with_text` and on a pushed fix flips back to `validating` while **bumping** `review_round` (the validating-route discriminator is `pending_fix_at is None` — the in_review-route variant sets it and resets the round to 0 instead, because the previous reviewer round there was APPROVED).
- **Output**: label moved to `documenting` (approval after verify + squash, so the docs pass hands off to `in_review`) OR label moved to `fixing` (CHANGES_REQUESTED — the dev fix runs there and flips back to `validating` on a pushed fix, with `review_round` bumped on the return) OR no label change with `review_round` bumped (an awaiting-human resume, user-content drift, or a transient-park-recovery push that finished a pending push — the issue stays on `validating` and the reviewer re-evaluates on the next tick) OR a HITL park (squash/force-push failure stays on `validating` with the approval comment already on the PR; every other park branch keeps the existing label).

### `_handle_in_review` (label `in_review`)
- **Trigger**: each tick while label is `in_review` (set by `_handle_documenting` on the final-docs hop after `_handle_validating` approves: the docs pass either pushes a docs commit and advances to `in_review`, or emits `DOCS: NO_CHANGE` against the remote-clean approved head and advances without pushing). Also runs on closed-`in_review` issues yielded by the closed-issue sweep, so an external manual merge gets finalized to `done` even when `Resolves #N` already closed the issue.
- **Input**: pinned `pr_number`, `branch`, `dev_agent`/`dev_session_id`, and three watermarks — one per id namespace GitHub uses for PR feedback:
  - `pr_last_comment_id` (issue thread + PR conversation, shared IssueComment id space; falls back to `last_action_comment_id` for back-compat).
  - `pr_last_review_comment_id` (inline review comments, PullRequestComment id space).
  - `pr_last_review_summary_id` (PR review summaries in the PullRequestReview id space).

    Only the *bodies* of non-empty `CHANGES_REQUESTED` or `COMMENTED` reviews are forwarded to the dev, and only those review IDs ever advance this watermark.

    `APPROVED`, `DISMISSED`, `PENDING`, and empty-body reviews are filtered out by `gh.pr_reviews_after` *before* the id watermark is applied, and `_bump_in_review_watermarks` mirrors the same filter, so excluded review IDs never enter the candidate set.

    Re-scanning is harmless: the filter runs each tick, so an `APPROVED` id above the watermark is silently re-skipped rather than re-forwarded.

  Mixing any two namespaces under one watermark would silently drop or replay one side.
- **Internal flow**:
  1. If `pr_number` is missing (manual relabel suspected), park awaiting human and return; subsequent ticks no-op until the human relabels.
  2. Read the PR via `gh.get_pr` and delegate the terminal arcs to the shared `_drain_review_pr_terminals` helper (also called by `_handle_fixing` and `_handle_resolving_conflict` so the three review-side stages share one finalize path). The orchestrator is permanently manual-merge-only and never calls `gh.merge_pr` from here, so any `merged` state observed below was produced by a human or bot landing the PR externally. Branch on `gh.pr_state(pr)`:
     - `merged` (external manual merge) → stamp `merged_at`, set label `done`, write pinned state, emit `pr_merged` (`stage="in_review"`, `merge_method="external"`), then `issue.edit(state="closed")`. (Pinned-state write before close so PyGithub caching cannot serve a stale issue body to the writer; the event is emitted before close so an `issue.edit` failure does not also drop the audit record.) Cleanup follows via `_cleanup_terminal_branch`.
     - `closed` (without merge) → stamp `closed_without_merge_at`, set label `rejected`, write state, emit `pr_closed_without_merge`, then close, then call `_cleanup_terminal_branch`. The branch name is derived from the issue number (`orchestrator/issue-<n>`) so cleanup cannot touch an arbitrary branch.
     - `open` BUT the issue itself was closed manually → set label `rejected`, stamp `closed_without_merge_at`, write state, WITHOUT branch cleanup so the operator can salvage the still-open PR (no `pr_closed_without_merge` emit either — that event is reserved for the actual closed-PR arc).
     - `open` with an open issue → fall through.
  3. **Fresh PR feedback (including any human CI-fix request) → route to `fixing`.** A human CI-fix request — a "please fix CI" / "tests are red, fix" comment on any of the four surfaces below — is just one shape of fresh PR feedback as far as this handler is concerned: the route triggers on the *presence* of an unread human comment past the watermark, not on its content. Read four sources independently, one per id namespace:
     - `gh.comments_after(issue, pr_last_comment_id)` (issue thread).
     - `gh.pr_conversation_comments_after(pr, pr_last_comment_id)` (PR conversation; shares id space with the issue thread, so one watermark suffices).
     - `gh.pr_inline_comments_after(pr, pr_last_review_comment_id)` (inline review comments).
     - `gh.pr_reviews_after(pr, pr_last_review_summary_id)` (PR review summary bodies submitted with `CHANGES_REQUESTED` or `COMMENTED` — `APPROVED` bodies are filtered out as informational, dismissed/pending never count, empty bodies are dropped).

     Without the `pr_reviews_after` surface, a "Comment" review with a request in the body would be silently ignored (and the HITL ping would invite a manual merge over it), and a `CHANGES_REQUESTED` review with body but no inline comments would never reach the dev agent.

     If any source is newer than its watermark, record pending-fix metadata in pinned state (`pending_fix_at` ISO timestamp plus per-namespace `pending_fix_issue_max_id` / `pending_fix_review_max_id` / `pending_fix_review_summary_max_id` bookmarks) and flip the label to `fixing` immediately. The `_handle_in_review` handler deliberately does NOT honor `IN_REVIEW_DEBOUNCE_SECONDS` here or spawn the dev itself — the `fixing` stage owns debouncing, the dev resume, the push, and the DIRECT bounce back to `validating` (docs do not run here -- the single docs pass is deferred to the final-docs handoff after reviewer approval) so the in_review handler stays focused on PR-state terminals and the HITL ping path. Watermarks are deliberately NOT advanced on this route so the `fixing` handler can read the triggering comments to build its dev-resume prompt; the `pending_fix_*_max_id` keys are bookmarks (a hint for the `fixing` handler / for observability), not watermarks. If `awaiting_human` / `park_reason` were carried over from a prior transient park, they are cleared as part of the route (the human comment that triggered the route is the resume signal).
  4. **User-content drift → relabel back to `validating`.** Reached when no fresh PR-side ID surfaced a comment past the watermarks above (so the `fixing` route did not fire) but `_detect_user_content_change` still reports a hash change. The fixing route and the drift hash watch for different things: fixing keys on a comment id newer than its watermark, while the drift hash is a content hash over issue title + body + every human-authored issue-thread comment body. The branches a tick can therefore land here are:

     - a title or body edit (the common case);
     - an edit to an *existing* issue-thread comment body whose id is already below `pr_last_comment_id` (fresh-by-id watermarks don't see it, but the body hash does);
     - or a fresh issue-thread comment whose id IS above the watermark but whose route was racy enough that the rescan caught only the title/body hash change — in practice the same tick's fixing scan would catch the new id, so this case is rare.

     Note that PR-conversation comment edits to ids below the watermark are covered by neither path: the fixing route is ID-only and the drift hash hashes only issue-thread comments. Edits to existing PR-conversation comments therefore stay invisible until a human posts a fresh issue/PR comment that re-triggers either path.

     Handler steps:
     - `_detect_user_content_change` returns the new hash.
     - Capture unread PR-conversation comments past `pr_last_comment_id` BEFORE posting the orchestrator's notice (the subsequent `_bump_in_review_watermarks` could otherwise leap past a PR-conversation comment whose id falls between the prior watermark and the issue-thread max).
     - Post a `:pencil2: issue body changed; resuming dev session.` notice on the PR.
     - Mark drift comments consumed via `_mark_drift_comments_consumed`.
     - Resume the locked dev session with `_build_user_content_change_prompt` (quoting issue body + recent comments + the captured PR-conversation comments).
     - Route the result through `_post_user_content_change_result`.

     Both successful outcomes — a clean pushed fix AND an explicit `ACK: <reason>` no-commit reply — reset `review_round=0` and bounce DIRECTLY back to `validating` (the prior reviewer approval was for the stale requirements, so re-approval is required before the in_review ready-ping gate can fire again). Docs do not run on this exit. A no-commit response without the `ACK:` marker parks via `_on_question`. Park reasons emitted on the resume failure paths (`agent_timeout`, `push_failed`, `agent_silent`) are durable per the schema above.
  5. **Manual-merge HITL path** (only reached when there is no fresh PR feedback AND no drift to act on). The orchestrator never merges from `in_review` -- humans drive the merge. Sequence:
     - **`pr_is_mergeable`** — `None` means GitHub still computing, try next tick.

       `False` parks awaiting human with `park_reason="unmergeable"` (post a HITL ping on the issue mentioning every `HITL_HANDLE`, bump the in_review watermarks past the orchestrator's own park comment via `_bump_in_review_watermarks`, write pinned state, return). A subsequent human comment routes the issue to `fixing` and clears the park; the operator can also relabel manually to unstick it.
     - **`True`** runs the ready + no-veto gate before pinging:
       - `gh.pr_has_changes_requested(pr, head_sha=head_sha)` returns silently on True — a standing human CHANGES_REQUESTED on the current head vetoes the ready ping (the orchestrator must not advertise a vetoed commit as ready for merge).
       - The ping requires either `docs_checked_sha == pr.head.sha` with `docs_verdict` set by the final-docs handoff (`updated` / `no_change`) OR `gh.pr_is_approved(pr, head_sha=pr.head.sha)` (a human/bot APPROVED review on the current head). Stale docs handoffs and stale GitHub approvals on older commits do not count.

       When the gate passes, post a one-shot `:bell:` ping mentioning every `HITL_HANDLE` so the human knows the PR is ready for review/merge. The ping is de-duplicated by `ready_ping_sha` (the head SHA we pinged for): a long-lived ready PR doesn't spam handles on every poll, but a new commit shifts `pr.head.sha` and triggers a fresh ping after another final-docs handoff or current-head GitHub approval.

       The ping is NOT a park: `awaiting_human` stays false so subsequent ticks still react to new PR comments, an external merge, or a later unmergeable transition.

       Unlike the park branches, the ready ping deliberately does NOT call `_bump_in_review_watermarks`. The watermark bump reads `gh.latest_comment_id(issue)`, which could include a human issue/PR-conversation comment that landed between the handler's earlier comment scan and the ping; bumping past it would silently swallow the feedback. The ping is recorded in `orchestrator_comment_ids` by `_post_issue_comment`, so the next tick's id-set filter already excludes it from `new_issue_side` without needing the watermark to move — and any concurrent human comment naturally surfaces below the unchanged watermark.
  6. Every park inside this handler bumps the in_review watermarks past the orchestrator's own park comment via `_bump_in_review_watermarks`, so the next tick does not see the park message as fresh PR feedback and route the issue to `fixing` over it.
- **Output**: label moved to `done` / `rejected` (terminal, external merge / close) OR a relabel to `fixing` (fresh PR feedback) OR a relabel to `validating` (user-content drift, pushed fix OR ACK no-commit; both reset `review_round=0`) OR a HITL park (unmergeable / missing pr_number / drift-resume failure: `agent_timeout` / `push_failed` / `agent_silent`) OR a HITL ping (ready + mergeable PR, no relabel) OR a no-op tick.

The "route to `fixing` on a new PR comment" arc is intentional: the fixing stage owns the dev-resume + push + hand-back-to-`validating` cycle so the in_review handler stays focused on PR-state terminals and the HITL ping path. The dev resume and reviewer re-run still happen — they just live in different stages — so the "validating re-runs after a fix" guarantee holds. Docs do not run on the pushed-fix exit: the single docs pass is deferred to the final-docs handoff after reviewer approval.

`_park_awaiting_human` posts on the issue (not the PR) so the HITL ping appears alongside the rest of orchestrator state. The PR comment that triggers a route to `fixing` is the human signal; awaiting-human is reserved for *unrecoverable* states (unmergeable PR / missing pr_number).

### `_handle_fixing` (label `fixing`)
- **Trigger**: each tick while label is `fixing`. Two routes set this label:
  - `_handle_in_review` when fresh PR feedback arrives on any of the four comment surfaces — including a human CI-fix request, i.e. a "please fix CI" / "tests are red, fix" comment, which is handled identically to any other unread human comment. This route records `pending_fix_at` + per-namespace `pending_fix_*_max_id` bookmarks.
  - `_handle_validating` on a `CHANGES_REQUESTED` verdict, flipped BEFORE the dev spawn so the dev-fix subphase is observably labeled `fixing` (the active job is "fixing reviewer-requested changes", not "validating"). This route does NOT set `pending_fix_at`; the dev runs inline in the same tick and on a pushed fix the validating handler flips the label back to `validating` itself with `review_round` bumped. Only the parked outcomes (timeout / no-commit / dirty / push-fail) leave the fixing handler to own the awaiting-human cycle on subsequent ticks.

  The label therefore means an unread human comment, a human CI-fix request, OR a parked reviewer-requested fix is queued during the quiet window or actively being addressed by the dev fix-loop. Also runs on closed-`fixing` issues yielded by the closed-issue sweep so an externally-merged PR can be finalized to `done`.
- **Input**: pinned `pr_number`, `branch`, `dev_agent`/`dev_session_id`, plus the `pending_fix_at` ISO timestamp and per-namespace `pending_fix_*_max_id` bookmarks recorded by the in_review route (absent on the validating route). Reads the three in_review watermarks (`pr_last_comment_id`, `pr_last_review_comment_id`, `pr_last_review_summary_id`) which the in_review route deliberately left behind so the rescan can re-discover the triggering feedback. `IN_REVIEW_DEBOUNCE_SECONDS` controls the quiet window.
- **Internal flow**:
  1. PR-state terminals mirror `_handle_in_review` (both stages delegate to the shared `_drain_review_pr_terminals` helper in `workflow.py` for these arcs) so the handler does not strand closed-`fixing` issues. `_handle_fixing` never calls `gh.merge_pr` either, so any `merged` state observed below was produced by a human or bot landing the PR externally:
     - `pr_state == "merged"` (external manual merge) → stamp `merged_at`, set label `done`, write pinned state, emit `pr_merged` (`stage="fixing"`, `merge_method="external"`), then `issue.edit(state="closed")`, then call `_cleanup_terminal_branch`.
     - `pr_state == "closed"` (without merge) → stamp `closed_without_merge_at`, set label `rejected`, write pinned state, emit `pr_closed_without_merge`, then `issue.edit(state="closed")`, then call `_cleanup_terminal_branch`.
     - PR is open BUT the issue was closed manually (sweep yielded it) → flip to `rejected` without branch cleanup so the operator can salvage the still-open PR.

     The fixing handler catches `gh.get_pr` exceptions itself and hands `pr=None` to the helper, which is a no-op; the rest of the fixing body then short-circuits via its own `if pr is None: return` guard. The other two callers (`_handle_in_review`, `_handle_resolving_conflict`) let the fetch raise through to `_process_issue`'s catch.
  2. Closed issue with no resolvable PR (manual relabel, no `pr_number`) → no-op; the operator must relabel manually to finalize.
  3. Open issue with no `pr_number` (manual relabel from outside the in_review route) → park awaiting human with `park_reason="missing_pr_number"`. The dev-resume path needs the PR to push a fix, so we cannot proceed without it.
  4. Rescan unread feedback from the three watermarks across all four surfaces (issue thread + PR conversation share the IssueComment id space; inline-review and review-summary live in their own id spaces). Orchestrator-authored comments are filtered by recorded id AND by the hidden `<!--orchestrator-comment-->` body marker. The route from `_handle_in_review` deliberately leaves the watermarks behind, so the initial fixing tick re-discovers the triggering comments; later ticks pick up additional comments that landed while the label was already `fixing` (which is what naturally extends the debounce window).
  5. If awaiting-human is set (a prior failed resume parked the issue) and the rescan finds no new feedback past the watermarks, branch on `park_reason` AND on the route discriminator `pending_fix_at`:
     - **Transient reason** (`push_failed` / `agent_timeout` / `reviewer_timeout` / `reviewer_failed` — the validating-side `_VALIDATING_TRANSIENT_PARK_REASONS` set, re-exported from `workflow`) **and `pending_fix_at` is unset (validating route)** → call the shared `_try_recover_validating_transient_park` helper. On `stuck` return silently. On `cleared` or `pushed` clear the park flags, clear the `pending_fix_*` bookmarks, and flip the label back to `validating` so the reviewer re-evaluates the current head next tick. The helper bumps `review_round` itself when a deferred push actually landed (the `pushed` outcome). This branch closes the loop for `_handle_validating`'s CHANGES_REQUESTED route, which now leaves transient parks under `fixing` rather than `validating`: without it, the issue would sit in `fixing` forever because the underlying condition (a flake clearing, the remote accepting the next push) does not produce a human comment.
     - **Transient reason BUT `pending_fix_at` is set (in_review route)** → return silently and wait for a fresh human comment. The in_review route advanced the PR-feedback watermarks past the human comment on the timed-out / failed-push resume; silently recovering here would consume that feedback without applying a fix. The shared helper's `pushed` outcome also bumps `review_round`, which contradicts the in_review-route reset-to-0 semantics. The in_review route therefore matches its original wait-for-human behavior.
     - **Non-transient reason** (`agent_question`, `agent_silent`, dirty-tree, missing-pr-number, …) → return silently; the gate stays held until a fresh human reply or new PR-side feedback unsticks it.

     With new feedback in hand, clear the park flags and fall through.
  6. If no unread feedback at all (watermarks already cover the bookmarks — a prior tick consumed them or an operator advanced them manually), clear the `pending_fix_*` bookmarks and bounce the label back to `validating` so the reviewer re-evaluates against the current head.
  7. **Quiet window**: compute the newest `created_at` (or `submitted_at` for review summaries) across the unread feedback; if that timestamp is younger than `IN_REVIEW_DEBOUNCE_SECONDS`, return and wait. A comment arriving on the next tick is naturally picked up by the rescan and resets the wait because the freshest timestamp controls the gate.
  8. **Resume**: build a `_build_pr_comment_followup` prompt over ALL unread surfaces (issue thread + PR conversation + inline + summaries), resume the locked dev session via `_resume_dev_with_text`, then refresh `user_content_hash` (the hash covers title + body + human issue-thread comments, so any issue-thread comment we just fed to the dev would otherwise re-fire `_handle_validating`'s drift check next tick and resume the dev a second time on input it already handled). Apply the validating-side `_handle_dev_fix_result` disposition (timeout / no-commit / dirty / push fail park flows are identical to the validating fix-loop).
  9. **Watermark advance**: regardless of dev outcome, the handler calls `_advance_consumed_watermarks`, which advances each of the three in_review watermarks ONLY to the max id consumed on that surface (ratcheted against the existing watermark).

     The advance deliberately does NOT include `gh.latest_comment_id(issue)` or `last_action_comment_id`. A human comment that landed AFTER the rescan but BEFORE this write was never quoted in `_build_pr_comment_followup`, so silently moving the watermark past it would swallow real feedback — breaking the "comments arriving while already labeled `fixing`" contract on the failure paths AND letting the next in_review tick advertise the PR as ready for merge over unread human feedback on the success path.

     The orchestrator's own park comment posted by `_park_awaiting_human` does NOT need a watermark bump to avoid replay: the next tick's rescan filters orchestrator-authored comments by recorded id AND by the hidden `<!--orchestrator-comment-->` body marker, so the park comment is dropped even when the watermark sits below it.
  10. **On a pushed fix**: clear the `pending_fix_*` bookmarks (they served their purpose), update `review_round` based on which route brought the issue here, write pinned state, and flip the label DIRECTLY back to `validating` so the reviewer re-evaluates the new head next tick. The discriminator is `pending_fix_at`:
      - **in_review->fixing** (set): reset `review_round` to 0. The previous reviewer round was APPROVED (in_review only routes here after the HITL ping gate or its prerequisites passed), so the next round starts fresh under MAX_REVIEW_ROUNDS.
      - **validating->fixing** (unset): bump `review_round` by 1. The previous reviewer round was CHANGES_REQUESTED, so we are still inside the same review cycle and MAX_REVIEW_ROUNDS accounting must advance.

      Docs do not run on this exit — the single docs pass is deferred to the final-docs handoff after reviewer approval.
- **Output**: terminal `done` / `rejected` (PR-state arcs) OR label flipped to `validating` (pushed fix OR no-new-feedback bounce) OR a HITL park (timeout / dirty / push fail / no-commit) OR a no-op tick (quiet-window wait, missing-PR park already set).

### `_handle_resolving_conflict` (label `resolving_conflict`)
- **Trigger**: each tick while label is `resolving_conflict` (set by an operator relabel or the per-tick base-sync detour in `_refresh_base_and_worktrees`). Also runs on closed-`resolving_conflict` issues yielded by the closed-issue sweep, mirroring the in_review terminal handling so a manually-merged PR finalizes to `done` even when `Resolves #N` already closed the issue.
- **Input**: pinned `pr_number`, `branch`, `dev_agent`/`dev_session_id`, `conflict_round`. `MAX_CONFLICT_ROUNDS` from config.
- **Internal flow**:
  1. If `pr_number` is missing (manual relabel suspected), park awaiting human and return.
  2. Read the PR via `gh.get_pr` and hand it to the shared `_drain_review_pr_terminals` helper (the same helper `_handle_in_review` and `_handle_fixing` call). `resolving_conflict` rebases the PR branch onto `<remote>/<base>`; it never merges the PR, so any `merged` state observed below was produced by a human or bot landing the PR externally. Branch on `gh.pr_state(pr)`:
     - `merged` (external manual merge) → stamp `merged_at`, set label `done`, write pinned state, emit `pr_merged` (`stage="resolving_conflict"`, `merge_method="external"`), then `issue.edit(state="closed")`, then call `_cleanup_terminal_branch`.
     - `closed` (without merge) → stamp `closed_without_merge_at`, set label `rejected`, write pinned state, emit `pr_closed_without_merge`, then `issue.edit(state="closed")`, then call `_cleanup_terminal_branch`.
     - `open` → fall through.

     Mirrors the in_review terminal arcs for the case where a human resolves manually mid-stage. Cleanup runs whenever the PR itself is gone so a declined PR doesn't leave its `orchestrator/issue-<n>` branch behind either.
  3. If the issue itself was closed manually while the PR is still open, the helper from step 2 treats it as a hard human stop: flip to `rejected` rather than continuing to spawn the dev agent. Deliberately do NOT clean up the branch here — the PR is still open and may be useful for inspection or salvage.

     Same caveat as the in_review counterpart: once the label flips to `rejected` the closed-issue sweep does not surface this issue, so a subsequent PR close is not observed and the operator must clean up the worktree, local branch, and remote branch by hand. Cleanup fires automatically only when the PR is closed *before* the orchestrator flips the label to `rejected`.
  4. **Awaiting-human resume path**: when parked from a previous round and a new human comment has arrived since `last_action_comment_id`, resume the dev session on the in-progress rebase worktree with the human's text (mirrors `_handle_implementing`'s awaiting-human branch — the park messages explicitly invite that flow). The post-agent step uses the same `_post_conflict_resolution_result` helper as the fresh-rebase path.
  5. **Cap check**: if `conflict_round >= MAX_CONFLICT_ROUNDS`, park awaiting human with the round count and the cap quoted. To escape the park the human must either:
     - (a) relabel the issue back to `validating` (or any other workflow label) so the dispatcher leaves `_handle_resolving_conflict` entirely; or
     - (b) post a new issue comment, which the awaiting-human resume branch (item 4) picks up to drive another dev-agent round.

     A bare branch push or manual rebase alone does NOT unpark — `awaiting_human` stays set and step 4 returns until a comment lands or the label changes.
  6. Ensure the per-issue worktree. `_ensure_pr_worktree` (PR-aware, restores from `<remote>/<branch>`) is used in place of `_ensure_worktree`, which would rebuild from `<remote>/<base>` and silently discard the PR's commits.
  7. Refresh `<remote>/<branch>` over `_authed_fetch` (the same hardened authenticated channel `_push_branch` uses); a stale local `<remote>/<branch>` would mis-classify a real "remote moved out from under us" situation as in-sync.
  8. Compare HEAD to the freshly-fetched `<remote>/<branch>`:
     - `behind > 0` (worktree diverged) → park: force-pushing local state would clobber the real PR head.
     - `ahead > 0` (recovered unpushed commits from a previous tick that crashed before `_push_branch` returned) → run the same dirty-tree check `_on_dirty_worktree` uses, then push the recovered work and flip to `validating` (the single docs pass is deferred to the post-approval hop, so the reviewer re-runs against the recovered commit directly) with `review_round=0`, `conflict_round += 1`.
     - `(0, 0)` (in sync) → fall through.
  9. Refresh `<remote>/<base>` over the same hardened path, then run `git rebase <remote>/<base>` in the worktree under `_git_hardened` (drops global/system git config, disables hooks/fsmonitor/credential helpers/commit signing, and disables rebase autostash — the agent owns the worktree and could otherwise plant a hook to execute attacker code mid-rebase).
  10. **Clean rebase succeeded**: dirty-tree check first (a leftover edit from a crashed prior tick must not silently survive into validating).

      If the HEAD SHA did not move (already up-to-date — `git rebase` returned success without applying anything), skip the push and flip back to `validating` with `review_round=0`, `conflict_round += 1`. Counting the no-op against the cap surfaces a perpetually-unmergeable-due-to-branch-protection PR within `MAX_CONFLICT_ROUNDS` ticks instead of letting it ping-pong between handlers forever.

      If HEAD moved, force-with-lease push the rebased branch and flip to `validating` (same target as the no-op path; the single docs pass is deferred to the post-approval hop, so the reviewer re-runs against the rebased branch directly).
  11. **Conflicted rebase**: build a conflict-resolution prompt via `_build_conflict_resolution_prompt` (lists up to 20 conflicted paths, instructs the agent to resolve and continue the rebase, and not push), resume the dev session on the locked spec (backend + args) with that prompt, then run `_post_conflict_resolution_result`.
  12. `_post_conflict_resolution_result` is the shared post-agent funnel:
      - timeout → park (HITL);
      - unfinished rebase → park;
      - no new commit → `_on_question` park;
      - dirty tree → `_on_dirty_worktree` park;
      - push fail → park;
      - success → force-with-lease push, set `last_conflict_resolved_at`, increment `conflict_round`, reset `review_round=0`, flip to `validating` (the single docs pass is deferred to the post-approval hop, so the reviewer re-runs against the resolved branch directly).

      Fresh conflicted-rebase pushes pin the lease to the pre-rebase PR head captured after the branch/head equality gate. Awaiting-human resume pushes deliberately use `_push_branch`'s live `ls-remote` lease fallback, because the local `before_sha` may be an intermediate rebase or recovered commit SHA rather than the remote PR head.

      The counter increments only on the success path so a timeout/dirty/push-fail does not eat a slot from the cap.
- **Output**: label moved to `validating` (base-up-to-date no-op, clean rebase pushed, recovered push, agent-resolved conflicts, awaiting-human resume push, drift-pushed fix) OR no label change with state written (drift ACK no-commit OR drift `_on_question` park: the rebase is still unfinished, so the next tick re-enters `_handle_resolving_conflict`) OR `done`/`rejected` (terminal arcs) OR a HITL park (cap exhausted, dirty worktree, push fail, agent timeout, agent silence, fetch fail, diverged worktree, missing pr_number).

The rebase path deliberately rewrites the PR branch to keep history linear after other issue PRs land. Every pushed rebase resets `review_round`, so the reviewer agent must re-approve the rewritten head before the in_review ready-ping gate can pass.

### `_handle_question` (label `question`)
- **Trigger**: each tick while the label is `question`. Also runs on closed-`question` issues yielded by the closed-issue sweep — that's the terminal signal the handler consumes to finalize the Q&A thread to `done`.
- **Input**: issue title/body/comments + pinned state (`question_agent` / `question_session_id`, `awaiting_human`, `last_action_comment_id`, `park_reason`). The label is operator-applied — no other handler routes into `question` automatically, and `question` is deliberately NOT in `_FAMILY_AWARE_LABELS` so fan-out concurrency is preserved.
- **Internal flow**:
  1. **Terminal close.** If `issue.state == "closed"`, stamp `question_closed_at`, set label `done`, write pinned state, and tear down the per-issue worktree + local branch via `_cleanup_question_worktree`. Do NOT spawn the agent — the question is moot once the issue is closed. Even an unsafe park's preserved worktree is reaped here because the operator has signaled they're done with it.
  2. **Awaiting-human resume.** If `awaiting_human`, scan for new issue-thread comments past `last_action_comment_id` via `_resume_question_on_human_reply`. No new comments → return without writing state (a no-reply tick is a no-op, but the `finally` block still tears down any worktree left from a prior safe tick). New comments → advance the watermark BEFORE spawning so a crashed/timed-out resume still records the comments as consumed, then resume the locked session via `_build_question_followup_prompt` (or fall back to `_build_question_prompt` when `question_session_id` is empty so a fresh-spawn recovery still gets the full issue context).
  3. **Fresh spawn.** Otherwise ensure the per-issue worktree (same `issue-N` worktree the implementing stage uses) at `<WORKTREES_DIR>/<owner>__<name>/issue-<n>`, resolve the question spec via `_read_question_session(state)` — falling back to `(DECOMPOSE_AGENT_SPEC, DECOMPOSE_AGENT, DECOMPOSE_AGENT_ARGS, None)` only for the first-ever spawn so the question stage rides on the decomposer's backend choice. **Persist `question_agent` BEFORE invoking `run_agent`** so a backend hiccup that yields no session id cannot orphan the role identity (mirrors the `dev_agent` / `decomposer_agent` discipline). Build the read-only `_build_question_prompt`, spawn, and persist `question_session_id` from a fresh session id.
  4. Branch on result:
     - `timed_out` → `_park_question` with `question_timeout`. **Keep** the worktree on disk for operator inspection: the timeout killed the agent mid-run and it may have committed or dirtied the tree before being reaped.
     - new commits → `_park_question` with `question_commits`. **Keep** the worktree: this stage is read-only and the orchestrator refuses to push agent-authored commits as a dev implementation.
     - dirty tree → `_park_question` with `question_dirty`. **Keep** the worktree: same read-only contract.
     - empty `last_message` → `_park_question` with `question_silent` (likely a poisoned resume of a session previously killed mid-stream). The worktree is provably clean here, so it is torn down.
     - clean answer → post the agent's quoted message to the issue thread (pinging `HITL_MENTIONS` so the human is notified), park awaiting human with `question_answer`, and tear the worktree down.

  The `finally` block runs `_cleanup_question_worktree` unless one of the three unsafe-park branches set `keep_worktree=True`. A no-reply tick on a prior unsafe park inherits `keep_worktree` from `park_reason in {question_timeout, question_commits, question_dirty}` so the inspection target survives subsequent no-reply ticks; the safe-branch overrides set it explicitly to `False` so a clean resume after an operator reset ends the inspection window.
- **Cross-stage interaction (relabel to `implementing`).** `_handle_implementing` carries an explicit guard: when it inherits an `awaiting_human=True` + `park_reason` starting with `question_` from this stage, it inspects the worktree AND the local `orchestrator/issue-<n>` branch via `_branch_has_unpushed_commits`. A clean worktree + clean branch drops the question-stage park flags, ratchets `last_action_comment_id` past the question agent's answer comment, and falls through to the fresh dev-spawn path; a dirty worktree OR a branch with commits beyond `<remote>/<base>` re-parks with `question_unsafe_relabel` and tells the operator to reset before the dev agent can start from a clean base.
- **Output**: an issue comment with the agent's answer or follow-up question (always pinging `HITL_MENTIONS`) + a HITL park, OR a terminal flip to `done` on a manual close, OR a no-op tick when awaiting a human reply that has not arrived.

The Q&A flow deliberately keeps state minimal: no PR is ever opened, no branch is ever pushed, and the per-issue worktree only survives across ticks when an unsafe park requires operator inspection. Multi-turn conversations rebuild the worktree on each spawn from a fresh `<remote>/<base>` — the agent session state lives in pinned state, not in the worktree, so the locked session resumes correctly across the cleanup.

## State transition (label lifecycle)

```
   Forward (single-task happy path):
     (none) ──► decomposing ──► ready ──► implementing ──► validating
                ──► documenting (final-docs handoff)
                ──► in_review ──► done | rejected

   Decompose detours:
     decomposing --(split)──► blocked ──(children created)──► ready
                                  ▲
                                  └ child rejected ─► park HITL

   Validating fix loop (any pushed dev fix):
     validating --(CHANGES_REQUESTED)──► label=fixing (pre-spawn flip;
       dev runs with stage="fixing" so analytics tag the active job
       correctly)
         ──► pushed fix: ++review_round, label=validating
              (reviewer re-evaluates on the next tick, no docs hop)
         ──► park (timeout / no-commit / dirty / push fail):
              label stays =fixing, awaiting_human=True; the fixing
              handler owns the awaiting-human rescan + dev resume cycle
              and on a human-reply pushed fix BUMPS review_round (vs the
              in_review-route variant which resets it -- the
              discriminator is `pending_fix_at`)
     validating --(awaiting-human resume / user-content drift / transient-park push)──►
       ++review_round, label stays =validating
         ──► reviewer re-evaluates on the next tick (no docs hop)
     validating --(APPROVED, verify ok, squash ok)──►
       label=documenting (final-docs)
         ──► docs pass (the exit ratchets pr_last_comment_id past any
              issue-thread reply the awaiting-human resume consumed so
              in_review does not re-feed it as fresh PR feedback)
         ──► in_review
     (MAX_REVIEW_ROUNDS exhausted ─► park HITL;
      squash failure ─► park HITL on validating, no relabel emitted)

   In_review terminals and fix bounce
   (the orchestrator never merges from in_review -- humans drive the
    merge; the merged arc below always reflects an external merge):
     in_review --(PR merged externally)──► done
     in_review --(PR closed unmerged)────► rejected
     in_review --(fresh PR feedback)─────► fixing
       fixing --(quiet window expires, dev fix pushed)──► validating
       fixing --(rescan finds no unread feedback)──────► validating
     in_review --(user-content drift, pushed)──► validating
       (review_round=0; docs do not run here -- the single docs pass
        is deferred to the final-docs handoff after reviewer approval)
     in_review --(user-content drift, ACK no-commit)──► validating
       (review_round=0; same destination as the pushed exit)
     in_review --(final-docs-complete or GitHub-approved current head,
                    mergeable, no human CHANGES_REQUESTED,
                    head SHA not yet pinged)──► HITL ping
       (no relabel, awaiting_human stays false)
     in_review --(unmergeable)──► park awaiting human (unmergeable);
       a subsequent human comment routes to fixing and clears the park

     resolving_conflict (operator relabel or per-tick base-sync detour):
       --(any pushed resolution: clean rebase, recovered push,
          agent/human-resume push, drift push)──► validating
       --(base up-to-date no-op, no diff)──► validating
       --(drift ACK no-commit / drift _on_question park)──►
         no relabel; the rebase is still unfinished so next tick
         re-enters resolving_conflict
       --(round ≥ MAX_CONFLICT_ROUNDS)──► park HITL

   decomposing flavors:
     decision='single'  ─► label=ready  (parent itself implements)
     decision='split'   ─► create children, parent=blocked
                           (or `umbrella` when manifest umbrella=true),
                           child[i] = ready if no deps else blocked
     manifest invalid / question / timeout ─► park HITL

   blocked transitions (per tick):
     all children = done ─► parent=ready
     any child = rejected ─► park HITL on parent
     dep_graph walk: any blocked child with all deps=done ─► child=ready

   umbrella transitions (per tick):
     all children = done ─► parent=done, issue closed (no implementation)
     any child = rejected ─► park HITL on parent
     dep_graph walk: any blocked child with all deps=done ─► child=ready

   in_review terminals and routes:
     pr merged (external)                     ─► done (issue closed,
                                                _cleanup_terminal_branch)
     pr closed without merge                  ─► rejected (issue closed,
                                                _cleanup_terminal_branch)
     issue closed manually, PR still open     ─► rejected (issue closed,
                                                no branch cleanup —
                                                operator may salvage;
                                                if the PR is later closed
                                                after the label has flipped
                                                to `rejected`, the closed-
                                                issue sweep does not pick
                                                it up so cleanup must be
                                                done by hand)
     fresh PR feedback on any of the four     ─► label=fixing (record
       comment surfaces (issue thread,           pending_fix_at + per-
       PR conversation, inline review,           namespace pending_fix_*_
       review summary)                           max_id bookmarks, clear
                                                 stale awaiting_human/
                                                 park_reason; no debounce
                                                 wait, no dev spawn here)

   fixing:
     PR-state terminals mirror the in_review arcs so a closed-`fixing`
     issue with a merged PR finalizes to `done` and a closed PR
     finalizes to `rejected` (otherwise the issue would sit closed +
     `fixing` forever). The merged arc always reflects an external
     merge -- `fixing` never calls `gh.merge_pr`:
       pr merged (external) ─► done + merged_at + close + cleanup
       pr closed unmerged   ─► rejected + closed_without_merge_at + cleanup
     Otherwise: rescan unread feedback from the three in_review
     watermarks across all four surfaces (filter orchestrator comments
     by id + hidden body marker); if `awaiting_human` is set with no
     new feedback, bail; if no unread feedback at all, clear the
     `pending_fix_*` bookmarks and bounce back to `validating`;
     otherwise honour `IN_REVIEW_DEBOUNCE_SECONDS` against the freshest
     comment timestamp (newer comments naturally extend the window via
     the next tick's rescan). Past the window, build a
     `_build_pr_comment_followup` prompt over every unread comment,
     resume the locked dev session via `_resume_dev_with_text`, and
     run `_handle_dev_fix_result`. Regardless of outcome, advance the
     three in_review watermarks ONLY to the max id actually fed to the
     dev (tighter than the broad bump so a concurrent human comment
     that landed mid-handler survives to the next tick on BOTH the
     success path and the failure path -- the orchestrator's own park
     comment is filtered by id + body marker on the next tick's
     rescan, so the broad bump is unnecessary). On a pushed fix clear
     bookmarks, reset `review_round`, and flip DIRECTLY to `validating`
     so the reviewer re-evaluates the
     new head next tick (docs do not run on this exit -- the single
     docs pass is deferred to the final-docs handoff after reviewer
     approval). On failure (timeout / dirty / push fail
     / no-commit) park awaiting human; the next tick's
     `awaiting_human and not new_feedback` gate becomes true once the
     park comment is the only unread item (everything else has been
     consumed past the watermark or filtered as orchestrator-authored).

   resolving_conflict (capped by MAX_CONFLICT_ROUNDS):
     git rebase <remote>/<base> clean (HEAD moved) ─► push,
       label=validating (++conflict_round)
     git rebase <remote>/<base> no-op (HEAD unchanged, no diff) ─►
       label=validating (++conflict_round, no push)
     conflicts ─► dev resumes, continues rebase, push ─►
       label=validating (++conflict_round)
     ahead-of-remote recovered commits ─► push ─►
       label=validating (++conflict_round)
     awaiting-human resume push / drift push ─►
       label=validating (++conflict_round)
     drift ACK no-commit / drift _on_question park ─►
       no relabel; the rebase is still unfinished, next tick
       re-enters resolving_conflict
     conflict_round >= MAX_CONFLICT_ROUNDS ─► park awaiting human
     pr merged externally / closed unmerged mid-stage
                                ─► done / rejected (terminal;
                                   `resolving_conflict` rebases but
                                   never merges, so any merged PR
                                   observed here was landed by a
                                   human or bot externally)
     (docs do not run here -- the single docs pass runs after the
      reviewer's final approval via the `documenting` handoff)

   question (operator-applied; no automatic in/out transitions):
     fresh spawn ─► DECOMPOSE_AGENT runs read-only in issue-N worktree,
                    posts answer to issue thread, park awaiting human
                    (question_answer)
     human reply ─► resume locked session (question_agent /
                    question_session_id), post follow-up, park again
     agent commits / dirty / timeout ─► park (question_commits /
                    question_dirty / question_timeout); worktree
                    PRESERVED for operator inspection; base sync skipped
                    while label is question
     agent silent ─► park (question_silent); worktree torn down
     issue closed (operator) ─► label=done, stamp question_closed_at,
                    _cleanup_question_worktree (terminal)
     relabel to implementing ─► implementing's guard: clean worktree
                    AND branch ─► drop question park, resume dev;
                    dirty / branch has commits ─► park
                    (question_unsafe_relabel)

   any stage ──► [park: awaiting_human=true]  (timeout, dirty tree,
                       │                       question, push fail,
                       │                       unknown verdict, max rounds,
                       │                       retry budget exhausted,
                       │                       failed checks, push fail,
                       │                       conflict-rounds exhausted,
                       ▼                       invalid manifest)
                 wait for new human comment ──► resume agent (locked spec (backend + args))
```
