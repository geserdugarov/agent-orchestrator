# Workflow state machine

This file documents the label-based state machine that drives every GitHub issue from pickup to terminal. It is split out of [`architecture.md`](architecture.md), which keeps the high-level overview, module map, and process / agent / push / event-log details.

The sections below cover:

- [Workflow labels](#workflow-labels) — the label set and what each one means.
- [Per-tick flow (`workflow.tick`)](#per-tick-flow-workflowtick) — how a single tick fans out across repos, partitions issues by label, dispatches handlers, and what state each handler reads / writes.
- [Stage handlers](#stage-handlers) — the per-stage flow, the user-content drift hook, and the transitions each handler may produce.
- [State transition (label lifecycle)](#state-transition-label-lifecycle) — the compact label-lifecycle reference diagram.

## Workflow labels

An issue should have at most one workflow label at a time. Non-workflow labels such as `bug` or `enhancement` are preserved; the orchestrator only swaps labels from its own workflow set. Label names are part of the public contract because live GitHub issues carry them.

Three non-workflow **control labels** modify behavior without occupying the workflow slot:

- `hold_base_sync` pauses per-tick base sync, `in_review` HITL pings / unmergeable parks, and `resolving_conflict` rebases until removed.
- `backlog` makes the orchestrator skip the issue: the per-tick dispatcher filters it out before the family/fanout split (so a parked, workflow-label-less issue cannot fold into the cap-counted family bucket and starve other work under `parallel_limit=1`), and each stage handler also skips it before the workflow label is read. Removing it hands control back to the state machine on the next tick.
- `community_contribution` is applied by the per-tick open-PR sweep when `ALLOWED_ISSUE_AUTHORS` is configured: any open PR whose author is not in the allowlist is labeled and `HITL_HANDLE` is @-mentioned once per PR. Bot-authored PRs (Dependabot, Renovate, CI bots) are skipped via GitHub's `user.type == "Bot"` flag — they open PRs structurally and are not community contributions. The orchestrator does not otherwise drive these PRs. With `ALLOWED_ISSUE_AUTHORS` empty (the default), the sweep is a no-op.

### Typed states and the transition guard

The label vocabulary is defined once in [`orchestrator/state_machine.py`](../orchestrator/state_machine.py): `WorkflowLabel` (a `StrEnum`) is the single source of truth for workflow states, and `ControlLabel` holds the modifiers above. Because `StrEnum` members *are* their wire strings, GitHub labels and pinned-state JSON are unchanged — the enum just gives the names one authoritative definition.

Two guards run at `GitHubClient.set_workflow_label` (the single label-write chokepoint; `create_child_issue` bypasses `set_workflow_label` and shares only the typo guard for its direct write):

- **Typo guard (always strict).** A label name not in `WorkflowLabel` raises immediately, so a typo cannot be applied as a literal label that the next tick would treat as unlabeled-pickup.
- **Transition guard (`WORKFLOW_TRANSITION_GUARD` = `off` / `warn` / `enforce`, default `warn`).** An illegal `current → new` relabel is checked against `ALLOWED_TRANSITIONS`. `warn` logs and proceeds; `enforce` raises `IllegalTransition`; `off` disables the check. A same-label re-set is always allowed.

`ALLOWED_TRANSITIONS` is a forward spine (e.g. `implementing → validating → documenting`) plus interrupt / detour edges declared per-target. Operator relabels via the GitHub UI bypass both guards, so the guard never fights a human.

| Label | Meaning |
|---|---|
| _(none)_ | Open issue not yet picked up by the orchestrator. |
| `decomposing` | The decomposer is deciding whether the issue is single-context or should become child issues. |
| `ready` | The issue is decomposed and has no unresolved blockers. |
| `blocked` | The issue is waiting on child issues or dependency edges. |
| `umbrella` | Parent issue with no implementation of its own; closes to `done` when all children resolve. |
| `implementing` | The dev agent is producing commits in a per-issue worktree. |
| `documenting` | The single docs pass on the existing PR worktree, reached only via the final-docs handoff in `_handle_validating`'s approval branch (after verify + squash). Advances to `in_review` after a pushed docs commit OR an explicit `DOCS: NO_CHANGE` verdict. |
| `validating` | The reviewer agent is checking the diff; on `VERDICT: APPROVED` the local verify gate runs `VERIFY_COMMANDS` before the squash + `documenting` handoff. `CHANGES_REQUESTED` relabels to `fixing` before the dev spawn. |
| `in_review` | A PR is open and ready for human review. The orchestrator never merges from here — humans drive the merge. A mergeable PR whose current head completed the reviewer-approved final-docs handoff (or carries a real GitHub APPROVED review), with no standing human CHANGES_REQUESTED on that head, earns a one-shot HITL ping per head SHA. |
| `fixing` | The dev fix-loop is active. Entered on unread in-review feedback OR a `CHANGES_REQUESTED` verdict. A successful fix bounces directly back to `validating` so the reviewer re-approves. |
| `resolving_conflict` | The orchestrator is resolving a rebase conflict on a PR branch against `<remote>/<base>`. Reached only when the per-tick base-sync rebase actually leaves conflicted files, or via an operator relabel. |
| `question` | Operator-applied read-only Q&A label: the decomposer agent answers in the per-issue worktree and waits on a human reply or close. No PR is opened. |
| `done` | Terminal success; PR merged, umbrella resolved, or a `question` issue closed. |
| `rejected` | Terminal rejection; PR or issue closed without merge. |

## Per-tick flow (`workflow.tick`)

Each tick fans out across every configured repo (`config.default_repo_specs()` returns one `RepoSpec` per `REPOS` line) and dispatches per-issue handlers through a long-lived `IssueScheduler` capped by `MAX_PARALLEL_ISSUES_GLOBAL` / `MAX_PARALLEL_ISSUES_PER_REPO`. See [`architecture.md#per-tick-flow-workflowtick`](architecture.md#per-tick-flow-workflowtick) for the multi-repo dispatch and scheduler lifecycle.

The dispatch loop classifies each pollable issue by workflow label before submitting it:

- **Family-aware labels** (`decomposing`, `blocked`, `umbrella`, unlabeled pickup) read and write cross-issue state (parent ↔ child). They are folded into one bucket per repo that drains sequentially on a single worker thread, so parent / child handlers cannot race. A bucket whose every label is in `_CAP_EXEMPT_FAMILY_LABELS` (`blocked` or `umbrella` — pure label / dep-graph walks) runs on a dedicated executor and does not consume a `MAX_PARALLEL_ISSUES_*` slot, so a `blocked` parent waiting on children cannot deadlock those children.
- **Fan-out labels** (`ready`, `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, and the operator-applied `question`) only touch their own state and worktree. They run concurrently up to the per-repo and global caps. A **closed** fan-out issue (a merged-PR or closed-question issue still carrying its sweep label, surfaced by the closed-issue sweep) is submitted `cap_exempt=True`: its handler only runs a terminal finalization (flip to `done` / `rejected` + branch cleanup) with no agent spawn, so it must not be starved behind active agent work — otherwise under `parallel_limit=1` a merged-PR issue sits closed-but-labeled for many ticks while a sibling `validating` / `documenting` agent holds the only slot.

The duplicate-active gate keys on `(repo_slug, issue_number)`: an in-flight handler that straddles polling passes is reported active to the next poll's submit, which is rejected as `duplicate_active`. The pre-tick base-refresh skips any active issue's worktree.

Only issue numbers cross the thread boundary — each scheduler worker mints a fresh `GitHubClient` via `gh._for_worker_thread()` and re-fetches its Issue against that client.

### Base refresh

Before any issue is dispatched the tick runs `_refresh_base_and_worktrees(gh, spec)`: a single `git fetch <spec.remote_name> <spec.base_branch>` in `spec.target_root`, then per-issue dispatch on each existing worktree under `<WORKTREES_DIR>/<owner>__<name>/issue-*`. The remote name defaults to `origin` and is overridable per `REPOS` row. Per-stage `_ensure_*_worktree` helpers only fetch on (re)creation, so without this refresh long-lived worktrees would stay anchored to whatever `<remote>/<base>` looked like when first added.

Two paths depending on whether a PR exists:

- **Pre-PR worktrees** get a clean-tree `git rebase <remote>/<base>` directly — no remote to push, so the local branch stays linear without publishing a rewrite.
- **PR-having worktrees** in `validating` / `documenting` / `in_review` / `fixing` go through `_sync_pr_worktree_to_base`. A clean rebase pushes (force-with-lease pinned to the pre-rebase SHA so a foreign update rejects rather than being clobbered), resets `review_round`, posts a PR notice, and relabels to `validating` so the reviewer re-runs against the rewritten head. Only when the rebase actually leaves conflicted files does the helper relabel to `resolving_conflict`.

`hold_base_sync` skips both paths. The `question` label also skips unconditionally — its handler tears down its own worktree, and merging base into a question worktree would either accrete commits on a read-only branch or mask an inspection state.

Refresh-only failure modes — push rejected (`auto_base_rebase_push_failed`), rebase failed without conflicted files (`auto_base_rebase_failed`), dirty-after-clean-rebase (`auto_base_rebase_dirty`) — reset HEAD back to the pre-rebase SHA and park awaiting human with a durable `park_reason`. Recovery is refresh-only and gated on a fresh human issue-thread comment past `last_action_comment_id`; the actual `awaiting_human` / `park_reason` clear is deferred to the same pinned-state write that publishes real progress, so an early-return path cannot silently drop the retry intent. Every PR-stage handler short-circuits at its `awaiting_human` gate when `park_reason in _AUTO_REBASE_PARK_REASONS` so the refresh owns the operator's retry comment.

Before rebasing, the flow fetches `gh.get_pr(pr_number)` and skips when `pr_state != "open"`: a just-merged PR advances `<remote>/<base>`, so the stale worktree is naturally behind base; without this gate the refresh would push and relabel a PR the next handler would finalize. A `gh.get_pr` failure is treated as "leave alone".

### Pollable issues and finalization

`gh.list_pollable_issues()` yields all open non-PR issues plus closed non-PR issues still labeled with one of the seven sweep labels: `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, `question`. The closed-issue sweep makes external manual merges and operator closes finalize cleanly:

- Closed `in_review` / `fixing` / `resolving_conflict` — a human-merged PR with a `Resolves #N` footer auto-closes the issue before the orchestrator can flip the label.
- Closed `implementing` / `documenting` / `validating` — the same external-merge race when the human merges before reaching `in_review`. Each handler's entry-time `_finalize_if_pr_merged` flips to `done` instead of stranding the issue.
- Closed `question` — a human closing the issue is the terminal signal `_handle_question` consumes to finalize to `done`.

Pre-PR labels (`decomposing` / `blocked` / `umbrella` / `ready`) are not swept closed — a closed issue at those stages is a hard human stop until an operator relabels.

The closed-issue sweep issues one closed-issue query per sweep label per repo, every tick — a fixed request cost that drives GitHub primary-rate-limit exhaustion on multi-repo hosts. `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` (default `1`) batches it to once every N ticks; the open-issue poll is unaffected, so the only effect of `N>1` is that an externally-merged/closed issue can take up to `N-1` extra ticks to finalize. See [configuration.md#github-rate-limits](configuration.md#github-rate-limits).

`done` and `rejected` are terminal no-ops. Every handler receives the active `RepoSpec`, so `git worktree add`, `git fetch <spec.remote_name> <spec.base_branch>`, push-token resolution, and PR-base selection all flow from the spec.

### Pinned state

Per-issue durable state lives in a single **pinned comment** on the issue (`<!--orchestrator-state {...json...}-->`). The schema is defined by `read_pinned_state` / `write_pinned_state` (see `github.PINNED_STATE_MARKER` / `PINNED_STATE_RE`). The keys that matter for the state machine fall into a few groups:

- **Agent identity.** `dev_agent` + `dev_session_id` (locked dev session — see [`workflow.md#in-flight-session-lock--pinned-full-spec-until-the-session-ends`](workflow.md#in-flight-session-lock--pinned-full-spec-until-the-session-ends)), `review_agent` (traceability only; reviewer is fresh per round), `decomposer_agent` + `decomposer_session_id` (parents), `question_agent` + `question_session_id` (`question` stage).
- **Decomposition.** `children`, `dep_graph` (`{child_idx_str: [child_idx, ...]}` — GitHub has no first-class blocks relation), `decomposed_at`, `pickup_comment_id`.
- **PR / branch.** `branch`, `pr_number`, `review_round`, `conflict_round`.
- **Drift baseline.** `user_content_hash` — SHA-256 over title + body + non-orchestrator comments; updated whenever the orchestrator reacts to a human edit.
- **HITL park.** `awaiting_human`, `last_action_comment_id`, `park_reason`. `_park_awaiting_human` sets `awaiting_human=True` and clears `park_reason` to `None`; a handler that needs the reason to survive into the next tick explicitly re-sets it after the park call. Park reasons that route via `_park_auto_rebase_failure` (`auto_base_rebase_failed` / `auto_base_rebase_dirty` / `auto_base_rebase_push_failed`) are owned by the per-tick base-sync flow — every PR-stage handler short-circuits when `park_reason in _AUTO_REBASE_PARK_REASONS`.
- **In-review watermarks.** `pr_last_comment_id` (issue thread + PR conversation, shared IssueComment id space), `pr_last_review_comment_id` (inline PR review comments), `pr_last_review_summary_id` (PR review summary bodies). Only non-empty `CHANGES_REQUESTED` or `COMMENTED` review IDs ever advance the summary watermark; `APPROVED`, `DISMISSED`, `PENDING`, and empty-body reviews are filtered before the bump.
- **Final-docs handoff.** `docs_checked_sha` + `docs_verdict` (`updated` / `no_change`) set by `_handle_documenting`'s success exits. `ready_ping_sha` records the head the in_review handler already posted a `:bell:` HITL ping for. `docs_drift_unwind_pending` is set while `_handle_documenting`'s drift block is reconciling and cleared only on the relabel back to `validating`.
- **Fix routing.** `pending_fix_at` + per-namespace `pending_fix_issue_max_id` / `pending_fix_review_max_id` / `pending_fix_review_summary_max_id` recorded by the `in_review → fixing` route. They are hints, not watermarks — the in_review watermarks are deliberately left behind so the `fixing` rescan can re-discover the triggering comments.
- **Crash-recovery anchor.** `pending_auto_base_rebase_push_sha` — set to the pre-rebase local HEAD immediately BEFORE `_rebase_base_into_worktree`; cleared on every exit. A non-empty value on entry means a previous tick rebased and died before the post-push write, and `_recover_pending_auto_base_rebase` keys off it to either no-op, push the recovered head, or park as `auto_base_rebase_push_failed`.
- **Counters / timestamps.** `retry_window_start` + `retry_count` (24h fresh-spawn budget shared between implementing and decomposing), `silent_park_count` (dev-session silent-park counter), `dev_resume_count` (per-dev-session resume budget; once it reaches `DEV_SESSION_MAX_RESUMES` the session is retired and respawned fresh from durable state, reset to 0 on every fresh spawn), `merged_at` / `closed_without_merge_at` terminal stamps.

The legacy `codex_session_id` key (written before `dev_agent` existed) is still honored on read by `_read_dev_session`: it round-trips to `spec="codex"` with no args so an older orchestrator's pin keeps running on codex.

## Stage handlers

### `_handle_pickup` (no label → `decomposing` or `implementing`)
- **Trigger**: open issue with no workflow label.
- **Input**: issue title/body/comments; `config.DECOMPOSE` (default on); `config.ALLOWED_ISSUE_AUTHORS` (default empty → allow all).
- **Action**: when `ALLOWED_ISSUE_AUTHORS` is set, an issue authored by anyone outside the list is silently skipped (log only); otherwise post a "picking this up" comment, anchor `pickup_comment_id`, snapshot `user_content_hash` over title + body + non-orchestrator comments, then route to `decomposing` (`DECOMPOSE=on`) or `implementing` (`DECOMPOSE=off`).

### User-content drift detection

The drift-sensitive handlers — `_handle_decomposing`, `_handle_ready`, `_handle_blocked`, `_handle_umbrella`, `_handle_implementing`, `_handle_validating`, `_handle_documenting`, `_handle_in_review`, `_handle_resolving_conflict` — run `_detect_user_content_change` somewhere in their flow. The hash covers the issue title, body, and every human-authored *issue-thread* comment body (PR-conversation comments are not in the hash).

`_handle_in_review` is the exception in ordering: it runs the four-surface fresh-feedback ID scan FIRST and routes any unread human comment past those watermarks to `fixing`, so the drift check that follows reacts only to changes the ID scan didn't catch (title/body edits, and edits to existing issue-thread comments whose ids are already below the watermark).

`_handle_fixing` and `_handle_question` deliberately skip the drift check. `_handle_fixing` refreshes `user_content_hash` itself once it has consumed the PR-side feedback; `_handle_question` runs its own conversation flow.

Non-human content is filtered four ways:

- pinned-state comments by `PINNED_STATE_MARKER`;
- orchestrator-posted comments by `_ORCH_COMMENT_MARKER` (an HTML comment embedded via `_with_orch_marker`, invisible in rendered Markdown, survives id-cap eviction);
- legacy orchestrator comments by id from `orchestrator_comment_ids`;
- third-party Bot/App accounts (Dependabot, Renovate, CI bots) via GitHub's `user.type == "Bot"` structural flag.

`_detect_user_content_change` durably persists the baseline on its FIRST encounter via `gh.write_pinned_state`, so an early-return tick cannot silently absorb a later edit as the new baseline. On drift the action depends on lifecycle position:

- **`decomposing`** — handled inline at the top of `_handle_decomposing`: drop `decomposer_session_id`, wipe `children` / `dep_graph` / `expected_children_count` / `umbrella`, clear park flags, post a `:pencil2: issue content changed` notice, then fall through in the same tick so the decomposer re-spawns against the updated body.
- **`ready` / `blocked` / `umbrella`** (no implementation has started) — route back to `decomposing` via `_route_drift_to_decomposing`: same state-wipe + notice, plus a label flip to `decomposing`. `decomposer_agent` is preserved across this transition so a mid-flight `DECOMPOSE_AGENT` env flip cannot retarget an in-flight issue. Any previously-tracked children are listed in the notice as ORPHANED — the orchestrator no longer tracks them, so the operator must close any that no longer apply.
- **`implementing` / `validating` / `in_review` / `resolving_conflict`** (a dev session exists and possibly a PR) — post a `:pencil2: issue body changed; resuming dev session` notice (on the issue for implementing/validating, on the PR for in_review/resolving_conflict), advance `last_action_comment_id` past every visible comment, resume the locked dev session with `_build_user_content_change_prompt`, and route the result through `_post_user_content_change_result`.
- **`documenting`** — route back to `validating` (no docs spawn) — see the handler section below.

Result routing in `_post_user_content_change_result`:

- a shutdown-`interrupted` resume short-circuits before any branch below: the helper self-guards (returns `"parked"` without posting, parking, or pushing) and the drift callers in turn bail WITHOUT writing pinned state (in_review / resolving_conflict guard ahead of the helper via `_ignore_if_interrupted`), so the killed run leaves durable state untouched for the next process to retry;
- a clean pushed fix hands straight back to `validating` from every stage that runs the drift resume; from `implementing` the drift path runs `_on_commits` to open/push the PR;
- a no-commit reply whose clean HEAD is strictly ahead of the remote PR branch (a fix a prior parked / interrupted run committed but never pushed) is published through the push tail and counted as a pushed fix (`_stranded_fix_unpushed`), ahead of the ack check;
- a no-commit reply is otherwise treated as an ack ONLY when it carries the explicit `ACK: <reason>` marker the resume prompt instructs the dev to emit when existing work already satisfies the edit;
- any other no-commit response falls back to `_on_question` and parks awaiting human.

Per-stage specifics:

- For **`in_review`** drift, both the "pushed" and "ack" outcomes reset `review_round` (a drift invalidates the prior approval) and bounce directly back to `validating`. The drift block also captures unread PR-conversation comments past `pr_last_comment_id` BEFORE posting its notice so the shared id space doesn't silently swallow a PR comment.
- For **`resolving_conflict`** drift, ONLY the "pushed" outcome relabels back to `validating` (with `review_round=0`, `conflict_round` bumped). Ack and parked outcomes stay on `resolving_conflict` — the rebase work is still unfinished. An `interrupted` resume (shutdown sweep killed the run mid-flight) short-circuits BEFORE `_post_user_content_change_result` and returns WITHOUT writing pinned state, so the refreshed `user_content_hash` / consumed-comment changes are discarded and the next process re-detects and re-runs the drift resume (the caller guards via `_ignore_if_interrupted` ahead of the helper; the shared helper also self-guards on interrupted as a backstop, returning `"parked"`).
- For **`implementing`** drift, the resume runs only when `dev_session_id` is recorded. With recovered unpushed commits but no session the handler parks (the commits were authored against the pre-drift body). With no session, no recovered commits, and `awaiting_human=True`, park flags are cleared so the fresh-spawn branch fires this tick against the updated body.
- For **`validating`** drift, the handler defers to the awaiting-human branch when `park_reason` is reviewer-side (`reviewer_timeout` / `reviewer_failed`): a "retry" reply after a reviewer failure must re-spawn the reviewer, not the dev. The new baseline is still persisted so the next tick doesn't loop.

The hash is re-persisted on every reaction so a single edit triggers exactly one re-route, not a loop.

### `_handle_decomposing` (label `decomposing`)
- **Trigger**: each tick while the label is `decomposing`.
- **Input**: issue + comments + pinned state (`decomposer_agent` / `decomposer_session_id`, retry-budget keys, `children`, `dep_graph`, `expected_children_count`, `umbrella`).
- **Internal flow**:
  1. **User-content drift check** (inline) — see drift section above.
  2. **Half-finished decomposition recovery.** If `expected_children_count` is set OR `children` is non-empty (a prior tick crashed mid-split), the handler cannot safely respawn the decomposer. When `expected_children_count` is set and `len(children) < expected_children_count`, park with `decomposition_crash`. Otherwise repair any child whose pinned `parent_number` was never seeded, then finalize to `umbrella` (when the flag is true) or `blocked`.
  3. **DECOMPOSE kill switch.** If `config.DECOMPOSE` is off when this handler runs, clear decomposer-side park flags, ratchet `last_action_comment_id` past every visible comment, flip the label to `implementing`, and fall into `_handle_implementing`. Step 2 runs first so orphan children are not abandoned.
  4. **Awaiting-human resume OR fresh spawn.** Resume on a new comment; otherwise gate on the per-issue retry budget (shared with `implementing`), ensure a read-only worktree, resolve the spec via `_read_decomposer_session`, persist `decomposer_agent` BEFORE invoking `run_agent`, and spawn the decomposer.
  5. **Read-only check.** If the worktree now has commits or dirty files, park awaiting human and KEEP the worktree for operator inspection. The decomposer is read-only — without this guard, `_handle_implementing`'s recovery path would later push decomposer-authored work as implementation.
  6. **Parse the manifest** via `_parse_manifest` (regex captures the fenced ` ```orchestrator-manifest ` block):
     - invalid manifest → park with the parse error.
     - no fenced block → treat as a question; park.
     - `decision == "single"` → label `ready`, stamp `decomposed_at`.
     - `decision == "split"` → for each child call `gh.create_child_issue(...)` with label `blocked` and seed the child's pinned state with `parent_number`; persist `children` / `dep_graph` / `umbrella` on the parent; activate no-dep children by flipping `blocked` → `ready` (best-effort, since `_handle_blocked` / `_handle_umbrella` also treats no-dep children as deps-satisfied).
- **Output**: parent → `ready` / `blocked` / `umbrella` / `implementing`, OR a HITL park.

### `_handle_ready` (label `ready` → `implementing`)
- **Trigger**: each tick while the label is `ready`. Reached by a `single`-decision parent or a freshly-created child.
- **Action**: post the pickup comment if needed, bump `last_action_comment_id` to the latest visible comment id (so comments posted while the issue sat in `decomposing` / `blocked` are marked consumed before the implementer reads them at spawn), flip to `implementing`, fall through into `_handle_implementing` on the same tick.

### `_handle_blocked` (label `blocked`)
- **Trigger**: each tick while the label is `blocked`.
- **Input**: pinned `children` (parent only), optional `dep_graph`, `parent_number` (child only — seeded at child-creation time).
- **Internal flow**:
  1. No `children` and `parent_number` is set → no-op (the parent walks the dep graph).
  2. No `children` and no `parent_number` (manual relabel suspected) → park.
  3. Read each child's current label.
  4. Any child `rejected` → park parent awaiting human.
  5. Any child closed but its label is not `done` / `rejected` / `in_review` → retry `_finalize_if_pr_merged` (covers an externally-merged child whose own handler has not yet finalized) before falling through to the manually-closed park.
  6. Every child `done` → flip parent → `ready`.
  7. Walk children: any `blocked` child whose recorded dependencies are all `done` gets relabeled `ready`. A child with no recorded deps is also flipped (vacuous all-done over an empty list).
- **Output**: parent → `ready` (all done), OR a sibling unblocked, OR a HITL park, OR a no-op for a child still waiting on its dependencies.

### `_handle_umbrella` (label `umbrella`)
- **Trigger**: each tick while the label is `umbrella` (only ever a parent — set by the decomposer when the manifest's `umbrella` boolean is true).
- **Input**: pinned `children` and optional `dep_graph` on the parent.
- **Internal flow**: mirrors `_handle_blocked` for the rejected / manually-closed checks and dep-graph walk. The only difference is the all-done terminal: when every child reaches `done`, post a checkmark comment, stamp `umbrella_resolved_at`, set label `done`, close the issue. A `children`-less umbrella is treated as corrupt state and parks.
- **Output**: terminal `done`, OR a sibling unblocked, OR a HITL park, OR a no-op.

### `_handle_implementing` (label `implementing`)
- **Trigger**: each tick while the label is `implementing`.
- **Input**: issue + comments + pinned state.
- **Internal flow**:
  0. **External-merge / closed-issue short-circuit.** `_finalize_if_pr_merged` flips a merged PR to `done` (`merge_method="external"`); `_finalize_if_issue_closed` flips a closed issue to `rejected` and emits `pr_closed_without_merge` + cleans up the branch only when the linked PR is also closed (an open PR with a manually-closed issue is left alone for operator salvage). Both helpers defer without writing state when the PR fetch fails so a transient failure cannot mis-label a merged-PR issue.
  1. Awaiting-human resume: on a new human comment past `last_action_comment_id`, resume the dev session via `run_agent(dev_agent, ...)`. The full spec persisted in `dev_agent` is re-parsed via `_read_dev_session` and reused; flipping `DEV_AGENT` in env does not migrate in-flight issues. When parked on `agent_timeout` with **no** new comment, first attempt `_try_recover_implementing_timeout_park` (the implementing counterpart to validating's transient-park recovery): on a clean worktree whose HEAD advanced past the persisted `pre_implement_sha`, publish the recovered commit via `_on_commits` and clear the park; otherwise stay parked silently. This recovers a clean commit a descendant the timeout cleanup raced finishes *after* the park is recorded (the observed `#77` shape: commit timestamp landed after the timeout event) without needing a human "push it" comment. A real human comment takes precedence and drives the normal resume.
  2. Otherwise ensure a per-issue worktree at `<WORKTREES_DIR>/<owner>__<name>/issue-<n>` on branch `orchestrator/<owner>__<name>/issue-<n>` (the slug-namespaced branch keeps two RepoSpecs sharing a `target_root` from colliding on the same `orchestrator/issue-<n>` ref). Worktrees with unpushed commits are reused (crash recovery); otherwise force-removed and recreated from `<spec.remote_name>/<spec.base_branch>`.
  3. If the worktree already has commits (recovered), skip the agent and go straight to push.
  4. Else gate the run on the per-issue retry budget (`MAX_RETRIES_PER_DAY`, default 3); a 24h window opens at the first counted spawn. Only fresh spawns count.
  5. Else build the implementer prompt (issue body + recent comments + "commit, do not push"), persist `dev_agent` BEFORE invoking `run_agent`, then spawn.
  6. Branch on result:
     - `interrupted` (shutdown sweep killed the run mid-flight) → ignore the partial result and return WITHOUT writing pinned state, so durable GitHub state stays exactly as the prior tick left it and the next process retries. Precedes every branch below and applies to both the awaiting-human and user-content-change resumes. Never posts a HITL question, consumes `awaiting_human`, or advances a watermark.
     - `timed_out` → dispose on whether HEAD advanced past the pre-agent SHA snapshot: a clean advance publishes via `_on_commits` exactly as a normal completion (a clean commit produced just before/around the kill is **not** stranded behind `awaiting_human`); a dirty advance parks via `_on_dirty_worktree`; no advance parks (`agent_timeout`) with the durable `park_reason="agent_timeout"` re-set and `pre_implement_sha` persisted for step 1's next-tick recovery. The `pre_implement_sha` watermark (not `_has_new_commits`, which only compares to `<remote>/<base>`) is what tells a commit produced by THIS run apart from commits already carried on the branch. (`_on_commits` clears the spent watermark + stale reason on publish.) Pairs with the hardened `_terminate_process_group` (SIGKILLs surviving descendants after the leader exits) so a build grandchild cannot keep committing into the worktree after the timeout is recorded.
     - new commits + clean tree → `_on_commits`: push branch, open PR (or reuse an existing open one), comment `:sparkles: PR opened: #N`, set label `validating` (the docs pass runs only as the final-docs handoff after approval), reset `review_round=0` and `retry_count=0`.
     - new commits + dirty files → `_on_dirty_worktree`: park; refuse to publish a partial branch.
     - no new commits → `_on_question`: post the agent's last message as a HITL question, park.
- **Output**: pushed branch + open PR + label moved to `validating`, OR a HITL park.

### `_handle_documenting` (label `documenting`)
- **Trigger**: each tick while the label is `documenting`. Set only by the **final-docs handoff** in `_handle_validating`'s approval branch (after verify + squash); the docs pass runs exactly once per reviewer-approval handoff, between approval and `in_review`. A PR may visit `documenting` more than once: if PR feedback bounces the issue to `fixing` and the dev pushes a fix, the next approval triggers another final-docs pass. Also runs on closed-`documenting` issues so an externally-merged PR finalizes to `done`.
- **Input**: pinned `pr_number`, `branch`, `dev_agent` / `dev_session_id` (the docs pass reuses the locked dev spec — there is no separate `documenting_agent`), plus `docs_checked_sha` / `docs_verdict` / `silent_park_count`.
- **Internal flow**:
  0. **External-merge / closed-issue short-circuit** (identical to `_handle_implementing`).
  1. **`pr_number` missing → park** with `missing_pr_number`. Documenting only runs against an existing PR worktree.
  2. **User-content drift → relabel back to `validating`** without spawning the docs agent. A title/body edit (or fresh human comment) during the final-docs hop invalidates the prior approval, so the reviewer must re-evaluate before any docs work can land. Housekeeping: post a `:pencil2: routing back to validating` notice, advance `last_action_comment_id`, refresh `user_content_hash`, clear park flags, reset `review_round=0`. Reconcile the PR worktree (fetch, then probe ahead/behind; on `ahead > 0`, `behind > 0`, or dirty files run `git reset --hard <remote>/<branch>` + `git clean -fd`) so no docs work authored against the pre-drift requirements survives. `docs_drift_unwind_pending` is set while the cleanup is in progress and cleared only on the relabel back to `validating`, so an operator unpark on a parked cleanup re-enters the drift block instead of falling through to a docs spawn.
  3. Awaiting-human + no new comment → early return BEFORE the fetch so a transient `fetch_failed` / `diverged_branch` doesn't re-post its park every tick.
  4. Ensure the PR worktree (`_ensure_pr_worktree`, restored from `<remote>/<branch>` so the dev's commits are intact) and refresh via `_authed_fetch`. Failure parks with `fetch_failed`.
  5. Ahead/behind check vs. the just-fetched `<remote>/<branch>`:
     - `behind > 0` → park with `diverged_branch` (force-pushing would clobber the real PR head).
     - `ahead > 0` recovered commits → synthesize an `AgentResult` and skip the agent; the unified branch below pushes the recovered docs commit.
     - `(0, 0)` → fall through.
  6. Awaiting-human resume: rebuild the FULL docs prompt via `_build_documentation_prompt` (this may be the first time the session sees the docs-stage instructions), persist `docs_checked_sha=before_sha` BEFORE the spawn, then `_resume_dev_with_text`.
  7. Fresh spawn: snapshot `before_sha`, persist `docs_checked_sha=before_sha` and `dev_agent` BEFORE invoking the agent, build the prompt (issue body + recent comments + `DOCS: NO_CHANGE` marker contract), then run.
  8. Branch on result. Every success exit routes to `in_review` via `_advance_after_docs_push` / `_advance_after_docs_no_change`, which ratchets `pr_last_comment_id` past any issue-thread reply the resume consumed so in_review does not bounce over already-addressed feedback. Branches:
     - `interrupted` (shutdown sweep killed the run mid-flight) → ignore the partial result and return WITHOUT writing pinned state (the pre-spawn `docs_checked_sha` / watermark writes are discarded), so the next process re-runs the docs pass. Precedes every branch below. The recovered `ahead > 0` path synthesizes a non-interrupted result, so it is unaffected.
     - `timed_out` → park (`agent_timeout`).
     - dirty worktree → `_on_dirty_worktree`: park.
     - new commit on a clean tree → `_push_branch`. On success record `docs_checked_sha=after_sha`, `docs_verdict="updated"`, reset `silent_park_count=0`, post `:books: documenting pass: pushed docs commit.`, advance. A push failure parks (`push_failed`).
     - no commit + `DOCS: NO_CHANGE` verdict: when `ahead > 0` push the recovered commit and advance; otherwise persist `docs_verdict="no_change"`, post `:books: no docs changes required.`, advance without pushing.
     - no commit + unknown verdict → `_on_question`: park.
- **Output**: label moved to `in_review` (success), OR `validating` (drift unwind), OR terminal `done` / `rejected` (short-circuit), OR a HITL park.

The docs pass is deliberately a thin dev-session rerun on the existing PR worktree rather than a separate role: there is no `documenting_agent` pin and no separate retry budget. The dev session resumes on its locked `(backend, args)` spec, so `DEV_AGENT` flips made mid-flight do not retarget the docs pass either.

### `_handle_validating` (label `validating`)
- **Trigger**: each tick while label is `validating`. Set by `_handle_implementing` after `_on_commits` opens the PR, by `_handle_documenting`'s drift unwind, and by `_handle_fixing` / `_handle_in_review` / `_handle_resolving_conflict` on their pushed exits.
- **Input**: PR #, branch, `dev_agent` / `dev_session_id`, `review_round`.
- **Internal flow**:
  0. **External-merge / closed-issue short-circuit** (same chain as implementing / documenting). The reviewer is not spawned on either short-circuit.
  1. Awaiting-human path: resume on the dev's locked spec; on a successful pushed fix, bump `review_round` and stay on `validating`. Exception: on a `review_cap` park the human reply does NOT wake the dev — the operator must post `/orchestrator add-review-rounds N` on its own line, which resets `review_round` to `MAX_REVIEW_ROUNDS - N`, clears the park, and falls through to spawn the reviewer this same tick.
  2. If `review_round >= MAX_REVIEW_ROUNDS` (default 3), park (`review_cap`). The park comment surfaces the `/orchestrator add-review-rounds N` escape hatch.
  3. Otherwise persist `config.REVIEW_AGENT_SPEC` to `review_agent` (traceability only — the reviewer is spawned fresh each round with no resume), then run the reviewer with the read-only prompt (must end with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`).
  4. Parse the last `VERDICT:` marker (`_parse_review_verdict`):
     - **approved** → in order: (1) run the local verify gate (`_run_verify_commands(wt, config.VERIFY_COMMANDS, config.VERIFY_TIMEOUT)`); a non-ok result parks via `_park_verify_failure` with a typed `park_reason` (`verify_failed` / `verify_timeout` / `verify_dirty` / `verify_head_changed`) and the approval / squash / handoff do NOT fire (see [`configuration.md#local-verification-gate`](configuration.md#local-verification-gate)); (2) post `:white_check_mark: codex review approved.`; (3) when `SQUASH_ON_APPROVAL` is on (default), call `_squash_and_force_push` (subject reuses the first commit when it carries a reusable `<prefix>:` form — Conventional **or** repo-local such as `event:`/`career:` — otherwise `<inferred-prefix>: <issue title>`, where the prefix is inferred from recent base-branch history via `_infer_subject_prefix` and falls back to `fix:`/`feat:` only when no repo-local prefix dominates; pushed with `--force-with-lease`). On squash / force-push failure, park awaiting human and stay on `validating` so the original commits remain for manual triage. (4) On success, if `squashed_count > 1` post `:package: squashed N commits to 1`, seed the in_review watermarks (inside the `gh.get_pr()` try so a snapshot failure leaves them untouched), then relabel to `documenting`.
     - **unknown** (no marker) → park.
     - **changes_requested** → post the feedback to the PR, then flip the label to `fixing` BEFORE spawning the dev so the active job is observably "fixing reviewer-requested changes". Resume the dev with the fix prompt; on a new commit + clean tree push, bump `review_round`, and flip back to `validating`. A no-commit run that finds a stranded unpushed fix on a clean HEAD (see `_handle_fixing` step 8) publishes it the same way. The dev spawn records `stage="fixing"` for analytics. On any park (timeout, no-commit, dirty, push-fail) the label STAYS `fixing` with `awaiting_human=True` and `_handle_fixing` owns the awaiting-human cycle thereafter. An `interrupted` dev resume is ignored: the handler returns WITHOUT writing the post-spawn state (no resume-budget charge, no watermark, no park), so the pre-spawn `fixing` flip stands and the next tick re-runs the cycle; any commit the killed run left is republished later via the stranded-fix tail, not this run.
- **Output**: label moved to `documenting` (approval after verify + squash) OR `fixing` (CHANGES_REQUESTED) OR no label change with `review_round` bumped (awaiting-human resume, drift, transient-park recovery push) OR a HITL park.

### `_handle_in_review` (label `in_review`)
- **Trigger**: each tick while label is `in_review`. Set by `_handle_documenting` on the final-docs hop. Also runs on closed-`in_review` issues for external-merge finalization.
- **Input**: pinned `pr_number`, `branch`, `dev_agent` / `dev_session_id`, and three watermarks (`pr_last_comment_id`, `pr_last_review_comment_id`, `pr_last_review_summary_id`) — one per id namespace GitHub uses for PR feedback. Mixing any two namespaces under one watermark would silently drop or replay one side.
- **Internal flow**:
  1. If `pr_number` is missing → park awaiting human.
  2. Read the PR via `gh.get_pr` and delegate the terminal arcs to the shared `_drain_review_pr_terminals` helper (also called by `_handle_fixing` and `_handle_resolving_conflict`). The orchestrator never merges from here, so any `merged` state observed was produced externally. Branch on `gh.pr_state(pr)`:
     - `merged` → stamp `merged_at`, set label `done`, write pinned state, emit `pr_merged` (`merge_method="external"`), close the issue, `_cleanup_terminal_branch`.
     - `closed` → stamp `closed_without_merge_at`, set label `rejected`, emit `pr_closed_without_merge`, close, cleanup.
     - `open` BUT the issue was closed manually → set label `rejected` WITHOUT branch cleanup so the operator can salvage the still-open PR.
     - `open` with an open issue → fall through.
  3. **Fresh PR feedback (including any human CI-fix request) → route to `fixing`.** Read four sources independently, one per id namespace: issue thread, PR conversation (shares IssueComment id space), inline review comments, PR review summaries (filtered to non-empty `CHANGES_REQUESTED` / `COMMENTED`). If any source is newer than its watermark, record `pending_fix_at` + per-namespace `pending_fix_*_max_id` bookmarks and flip to `fixing`. The handler does NOT honor `IN_REVIEW_DEBOUNCE_SECONDS` here or spawn the dev — `fixing` owns debouncing, the dev resume, and the DIRECT bounce back to `validating`. Watermarks are NOT advanced on this route so `fixing` can re-discover the triggering comments.
  4. **User-content drift → relabel back to `validating`.** Reached when no fresh PR-side ID surfaced a comment but `_detect_user_content_change` still reports a hash change (a title/body edit, or an edit to an existing issue-thread comment whose id is already below the watermark). Capture unread PR-conversation comments past `pr_last_comment_id` BEFORE posting the notice (the shared id space could otherwise leap past one). Resume the locked dev session with `_build_user_content_change_prompt` (quoting issue body + recent comments + the captured PR-conversation comments). Both successful outcomes — pushed fix AND `ACK: <reason>` no-commit reply — reset `review_round=0` and bounce directly back to `validating`. A no-commit response without the `ACK:` marker parks via `_on_question`. An `interrupted` resume short-circuits via `_ignore_if_interrupted` BEFORE `_post_user_content_change_result` and the watermark bump, returning WITHOUT writing pinned state so the drift stays unconsumed for the next process to retry.
  5. **Manual-merge HITL path** (only reached with no fresh PR feedback AND no drift):
     - `pr_is_mergeable` is `None` → try next tick.
     - `False` → park with `unmergeable`; HITL ping mentioning every `HITL_HANDLE`, bump watermarks past the park comment.
     - `True` → check `gh.pr_has_changes_requested(pr, head_sha=head_sha)` (a standing human CHANGES_REQUESTED on the current head vetoes the ping). The ping requires either `docs_checked_sha == pr.head.sha` with `docs_verdict` set OR `gh.pr_is_approved(pr, head_sha=pr.head.sha)` (a human/bot APPROVED review on the current head). When the gate passes, post a one-shot `:bell:` ping de-duplicated by `ready_ping_sha`. The ping is NOT a park: `awaiting_human` stays false so subsequent ticks still react to new comments / an external merge. Unlike park branches, the ready ping does NOT call `_bump_in_review_watermarks` (the bump reads `gh.latest_comment_id(issue)`, which could include a concurrent human comment).
  6. Every park inside this handler bumps the watermarks past the orchestrator's own park comment, so the next tick does not see it as fresh PR feedback.
- **Output**: label moved to `done` / `rejected` (terminal), OR `fixing` (fresh PR feedback), OR `validating` (drift; pushed fix OR ACK no-commit; both reset `review_round=0`), OR a HITL park (unmergeable, missing pr_number, drift-resume failure), OR a HITL ping (no relabel), OR a no-op tick.

`_park_awaiting_human` posts on the issue (not the PR) so the HITL ping appears alongside the rest of orchestrator state. The PR comment that triggers a route to `fixing` is the human signal; awaiting-human is reserved for *unrecoverable* states (unmergeable / missing pr_number).

### `_handle_fixing` (label `fixing`)
- **Trigger**: each tick while label is `fixing`. Two routes set this label:
  - `_handle_in_review` when fresh PR feedback (any of the four surfaces, including a human CI-fix request) arrives — records `pending_fix_at` + per-namespace `pending_fix_*_max_id` bookmarks.
  - `_handle_validating` on a `CHANGES_REQUESTED` verdict, flipped BEFORE the dev spawn. This route does NOT set `pending_fix_at`; the dev runs inline and on a pushed fix validating flips the label back itself. Only the parked outcomes leave the fixing handler to own the awaiting-human cycle.

  Also runs on closed-`fixing` issues so an externally-merged PR finalizes to `done`.
- **Input**: pinned `pr_number`, `branch`, `dev_agent` / `dev_session_id`, `pending_fix_at` + per-namespace bookmarks (in_review route only), the three in_review watermarks (left behind so the rescan can re-discover the triggering feedback), `IN_REVIEW_DEBOUNCE_SECONDS`.
- **Internal flow**:
  1. PR-state terminals mirror `_handle_in_review` (shared `_drain_review_pr_terminals`). `_handle_fixing` catches its own `gh.get_pr` exceptions and hands `pr=None` to the helper, which is a no-op.
  2. Closed issue with no resolvable PR → no-op.
  3. Open issue with no `pr_number` (manual relabel) → park (`missing_pr_number`).
  4. Rescan unread feedback from the three watermarks across all four surfaces. Orchestrator comments are filtered by recorded id AND the hidden `<!--orchestrator-comment-->` body marker.
  5. If `awaiting_human` and the rescan finds nothing new, branch on `park_reason` AND the route discriminator `pending_fix_at`:
     - **Transient reason** (`push_failed` / `agent_timeout` / `reviewer_timeout` / `reviewer_failed` — the `_VALIDATING_TRANSIENT_PARK_REASONS` set) **and `pending_fix_at` unset (validating route)** → call `_try_recover_validating_transient_park`. On `cleared` or `pushed`, clear park, clear `pending_fix_*`, flip back to `validating` (the helper bumps `review_round` on `pushed`). This closes the loop for `_handle_validating`'s CHANGES_REQUESTED route. On `stuck`, fall through to the worktree-drift check below.
     - **Any other awaiting-human shape** (transient reason on the in_review route, non-transient reason like a real agent question, dirty-worktree park, or silent-crash park) → return silently and keep waiting for a human reply. We cannot distinguish "agent has a real question" from "agent reported nothing to change" by inspection (both surface through `_on_question` with `park_reason=None`), so auto-routing either would silently bypass the HITL contract.

     **Worktree-drift dead-lock breaker** (`_reconcile_parked_fixing`). Reached only from the stuck-validating-route-transient branch above: the self-recovery could not clear the condition, and the underlying cause may be a base advance that landed mid-park (the per-tick base sync deliberately stands down on every `awaiting_human` park — `_sync_pr_worktree_to_base` returns at its `awaiting_human` gate — so nobody else will sync this worktree). On a clean worktree (not held by `hold_base_sync`) the breaker routes to `resolving_conflict` — seeding `conflict_round` when absent, clearing the park, posting a PR notice, emitting `conflict_round` `action="entered"` (`stage="fixing"`) — in either of two shapes, both reconciled by the conflict handler, which owns rebasing AND publishing a PR branch:
       - **behind `<remote>/<base>`** (a local `rev-list HEAD..<remote>/<base>`) → needs a rebase;
       - **already on base but local HEAD ≠ the live `pr.head.sha`** (a rebase a prior run ran but never pushed) → needs a force-publish (see `_handle_resolving_conflict` below).

     The routing decision is cheap — no extra fetch, since `pr` was already fetched this tick. With no drift (the worktree is in sync with the PR head), or a dirty / held worktree, the park is left intact and the issue keeps awaiting a human. The `pending_fix_*` bookmarks and in_review watermarks are left untouched so the eventual in_review re-entry still re-discovers the feedback.
  6. If no unread feedback at all (watermarks already cover the bookmarks), clear `pending_fix_*` and bounce back to `validating`.
  7. **Quiet window**: compute the newest `created_at` (or `submitted_at` for review summaries); if younger than `IN_REVIEW_DEBOUNCE_SECONDS`, return.
  8. **Resume**: build a `_build_pr_comment_followup` prompt over ALL unread surfaces, resume the locked dev via `_resume_dev_with_text`, refresh `user_content_hash` (so any issue-thread comment we just fed to the dev doesn't re-fire validating's drift check). An `interrupted` resume is ignored entirely BEFORE the ACK fast path, the stranded-fix check, and the watermark advance below: the handler returns WITHOUT writing pinned state, so no watermark advances, `awaiting_human` is untouched, and the next tick re-discovers the same feedback. Otherwise, a no-commit reply first checks for a **stranded fix** (`_stranded_fix_unpushed`): when the worktree is clean and HEAD is strictly ahead of the fetched remote PR branch (a fix committed by an earlier parked run whose publish was blocked — e.g. a dirty-park whose stray files were cleaned up afterwards), the handler publishes it through the normal push tail and treats the run as a pushed fix — this outranks the ACK fast path on both routes, so an acked stranded fix is published rather than relabeled. **ACK fast path** (in_review route only, no stranded fix): if the dev makes no commit but ends its message with the `ACK: <reason>` marker (the prompt instructs it to emit this when the comments name no actionable change — a vague "continue" / "ok"), clear `pending_fix_*`, post the ack as an FYI, and relabel straight to **`in_review`** without parking. Otherwise apply the same `_handle_dev_fix_result` disposition as the validating fix-loop. Any other unmarked no-commit reply falls through to `_on_question` and parks awaiting human — a no-ACK reply may be a real dev question, and we cannot tell by inspection (a dirty tree, failed fetch, or a remote that moved past the local view also falls back to this park rather than pushing blind).
  9. **Watermark advance**: regardless of dev outcome, `_advance_consumed_watermarks` advances each of the three watermarks ONLY to the max id consumed on that surface — tighter than a broad bump so a concurrent human comment that landed mid-handler survives to the next tick.
  10. **On a pushed fix**: clear `pending_fix_*`, adjust `review_round` per the route discriminator (in_review route resets to 0 — the previous approval was for the prior head; validating route bumps by 1 — same review cycle), flip DIRECTLY back to `validating`. Docs do not run on this exit.
- **Output**: terminal `done` / `rejected`, OR label flipped to `validating` (pushed fix OR no-new-feedback bounce), OR label flipped to `resolving_conflict` (stuck validating-route transient park while the worktree is out of sync with the PR — behind base or an unpushed local rebase), OR label flipped to `in_review` (in_review route, ACK fast path on this tick only), OR a HITL park, OR a no-op (quiet-window wait, missing-PR park already set).

### `_handle_resolving_conflict` (label `resolving_conflict`)
- **Trigger**: each tick while label is `resolving_conflict` (set by an operator relabel, by `_refresh_base_and_worktrees` when the auto rebase actually left conflicted files — a merely-behind-base PR rebase + push lands directly on `validating` — or by `_handle_fixing`'s worktree-drift dead-lock breaker when a validating-route transient `fixing` park whose self-recovery returned `"stuck"` is found out of sync with the PR head). Also runs on closed-`resolving_conflict` issues for terminal handling.
- **Input**: pinned `pr_number`, `branch`, `dev_agent` / `dev_session_id`, `conflict_round`. `MAX_CONFLICT_ROUNDS` from config.
- **Internal flow**:
  1. If `pr_number` is missing → park.
  2. Read the PR and hand it to the shared `_drain_review_pr_terminals` helper. `resolving_conflict` rebases the PR branch onto `<remote>/<base>` — it never merges, so any `merged` state was produced externally. Branch on `pr_state`: `merged` → `done` + close + cleanup; `closed` → `rejected` + close + cleanup; `open` → fall through.
  3. If the issue itself was closed manually while the PR is still open, flip to `rejected` without branch cleanup (operator may salvage). The closed-issue sweep does not surface `rejected`, so the operator must clean up the worktree / branch by hand if the PR later closes.
  4. **Awaiting-human resume**: when parked from a previous round and a new human comment arrived, resume the dev session on the in-progress rebase worktree with the human's text. The post-agent step uses the same `_post_conflict_resolution_result` helper as the fresh path.
  5. **Cap check**: if `conflict_round >= MAX_CONFLICT_ROUNDS`, park. Escape: (a) operator relabels off `resolving_conflict`, or (b) a new issue comment unparks via the resume branch.
  6. Ensure the PR worktree via `_ensure_pr_worktree` (restores from `<remote>/<branch>`, NOT base — `_ensure_worktree` would discard the PR's commits).
  7. Refresh `<remote>/<branch>` over `_authed_fetch` so a stale local ref doesn't mis-classify a "remote moved" situation as in-sync.
  8. Compare HEAD to the freshly-fetched `<remote>/<branch>`:
     - `behind > 0` (worktree diverged) → normally park (`diverged_branch`) since force-pushing could clobber the real PR head. **Exception — already-rebased-but-unpushed:** when the worktree is also `ahead > 0` AND already sits on top of base (`_already_rebased_onto_base` re-fetches base and checks `HEAD..<remote>/<base>` is empty) AND the stale remote head is one the orchestrator itself produced (`_pr_head_orchestrator_produced`: `pr.head.sha == docs_checked_sha` — the only key production code persists for an orchestrator-pushed head, written by `_handle_documenting`'s success exits), the "behind" commits are the orchestrator's own superseded pre-rebase commits — there is nothing external to lose, so fall through to the `ahead > 0` push and force-publish instead of parking. PR heads from earlier in the lifecycle (the initial implementing push, an intermediate fixing push) are not currently recorded anywhere in pinned state, so the exception declines those by design. If either guard fails (not on base, or an unrecognized head that might carry a direct push), keep the `diverged_branch` park.
     - `ahead > 0` (recovered unpushed commits, or the already-rebased fall-through above) → dirty-tree check, then push the recovered work (force-with-lease against the live remote head) and flip to `validating` with `review_round=0`, `conflict_round += 1`.
     - `(0, 0)` → fall through.
  9. Refresh `<remote>/<base>` and run `git rebase <remote>/<base>` under `_git_hardened` (drops global / system config, disables hooks / fsmonitor / credential helpers / commit signing / autostash — the agent owns the worktree and could otherwise plant a hook to execute attacker code mid-rebase).
  10. **Clean rebase succeeded**: dirty-tree check first. If HEAD did not move (already up-to-date), skip the push and flip to `validating` (`review_round=0`, `conflict_round += 1`). Counting no-ops against the cap surfaces a perpetually-unmergeable-due-to-branch-protection PR within `MAX_CONFLICT_ROUNDS` ticks. If HEAD moved, force-with-lease push and flip to `validating`.
  11. **Conflicted rebase**: build a conflict-resolution prompt via `_build_conflict_resolution_prompt`, resume the dev with it, then run `_post_conflict_resolution_result`.
  12. `_post_conflict_resolution_result`: `interrupted` (shutdown sweep killed the run mid-flight) → ignore the partial result and return WITHOUT writing pinned state, leaving durable state retryable (this is the one branch that does not write; it precedes all others); timeout / unfinished rebase / no commit / dirty / push fail → park; success → force-with-lease push, increment `conflict_round`, reset `review_round=0`, flip to `validating`. Fresh-rebase pushes pin the lease to the pre-rebase PR head; awaiting-human resume pushes use `_push_branch`'s live `ls-remote` lease fallback because `before_sha` may be an intermediate SHA.
- **Output**: label moved to `validating` (any pushed resolution OR no-op rebase), OR no label change (drift ACK / `_on_question` park: rebase still unfinished), OR `done` / `rejected` (terminal), OR a HITL park.

The rebase path deliberately rewrites the PR branch to keep history linear after other issue PRs land. Every pushed rebase resets `review_round`, so the reviewer must re-approve the rewritten head before the in_review ready-ping gate can fire.

### `_handle_question` (label `question`)
- **Trigger**: each tick while the label is `question`. Also runs on closed-`question` issues — that's the terminal signal the handler consumes.
- **Input**: issue + comments + pinned state (`question_agent` / `question_session_id`, awaiting-human keys). The label is operator-applied — no other handler routes into `question` automatically, and `question` is deliberately NOT in `_FAMILY_AWARE_LABELS` so fan-out concurrency is preserved.
- **Internal flow**:
  1. **Terminal close.** If the issue is closed, stamp `question_closed_at`, set label `done`, write pinned state, tear down the per-issue worktree + local branch via `_cleanup_question_worktree`. Do NOT spawn the agent.
  2. **Awaiting-human resume.** If `awaiting_human`, scan for new comments past `last_action_comment_id`. No new comments → return (the `finally` block still tears down any worktree from a prior safe tick). New comments → advance the watermark BEFORE spawning, then resume the locked session via `_build_question_followup_prompt`.
  3. **Fresh spawn.** Ensure the per-issue worktree, resolve the question spec via `_read_question_session` (falls back to the decomposer's spec on the first-ever spawn), persist `question_agent` BEFORE invoking `run_agent`, build the read-only `_build_question_prompt`, spawn, and persist `question_session_id`.
  4. Branch on result:
     - `timed_out` → `_park_question` with `question_timeout`. **Keep** the worktree for operator inspection.
     - new commits → `_park_question` with `question_commits`. **Keep** the worktree: this stage is read-only.
     - dirty tree → `_park_question` with `question_dirty`. **Keep** the worktree.
     - empty `last_message` → `_park_question` with `question_silent` (worktree torn down).
     - clean answer → post the agent's quoted message to the issue (pinging `HITL_MENTIONS`), park with `question_answer`, tear the worktree down.

  The `finally` block runs `_cleanup_question_worktree` unless one of the three unsafe-park branches set `keep_worktree=True`.
- **Cross-stage interaction (relabel to `implementing`).** `_handle_implementing` carries an explicit guard: when it inherits `awaiting_human=True` + a `park_reason` starting with `question_`, it inspects the worktree AND the local branch. A clean worktree + clean branch drops the question-stage park flags, ratchets `last_action_comment_id` past the question agent's answer, and falls through to fresh dev-spawn. A dirty worktree OR a branch with commits beyond `<remote>/<base>` re-parks with `question_unsafe_relabel`.
- **Output**: an issue comment with the answer / follow-up question + a HITL park, OR a terminal flip to `done` on a manual close, OR a no-op tick.

The Q&A flow keeps state minimal: no PR is ever opened, no branch is ever pushed, and the per-issue worktree only survives across ticks when an unsafe park requires operator inspection. The locked session resumes across cleanup because session state lives in pinned state, not in the worktree.

## State transition (label lifecycle)

```
   Forward (single-task happy path):
     (none) ──► decomposing ──► ready ──► implementing ──► validating
                ──► documenting (final-docs handoff)
                ──► in_review ──► done | rejected

   Decompose:
     decision='single' ─► label=ready  (parent itself implements)
     decision='split'  ─► create children, parent=blocked
                          (or umbrella when manifest umbrella=true);
                          child[i] = ready if no deps else blocked
     manifest invalid / question / timeout ─► park HITL

   Validating fix loop:
     validating --(CHANGES_REQUESTED)──► label=fixing (pre-spawn flip;
       dev runs with stage="fixing")
         ──► pushed fix: ++review_round, label=validating
         ──► park (timeout / no-commit / dirty / push fail):
              label stays =fixing, awaiting_human=True; the fixing
              handler owns the awaiting-human cycle and on a human-
              reply pushed fix BUMPS review_round (validating route)
              or RESETS it to 0 (in_review route) — discriminator is
              `pending_fix_at`
     validating --(awaiting-human resume / drift / transient-recovery push)──►
       ++review_round, label stays =validating
     validating --(APPROVED, verify ok, squash ok)──►
       label=documenting (final-docs) ──► in_review
     MAX_REVIEW_ROUNDS exhausted ─► park HITL
     squash failure ─► park HITL on validating, no relabel

   in_review (orchestrator never merges; merged arc always external):
     pr merged externally               ─► done (close + cleanup)
     pr closed unmerged                 ─► rejected (close + cleanup)
     issue closed manually, PR open     ─► rejected (no branch cleanup;
                                            operator may salvage)
     fresh PR feedback on any of the    ─► label=fixing (record
       four comment surfaces                pending_fix_at + bookmarks,
                                            clear stale park; no debounce
                                            wait, no dev spawn here)
     user-content drift (pushed or ACK) ─► validating (review_round=0;
                                            no docs hop)
     mergeable + final-docs-complete or ─► HITL ping (no relabel,
       GitHub-approved current head +      awaiting_human stays false)
       no human CHANGES_REQUESTED +
       head SHA not yet pinged
     unmergeable                        ─► park (unmergeable); a
                                            subsequent human comment
                                            routes to fixing

   fixing (terminals mirror in_review; merged arc always external):
     pr merged externally / closed unmerged ─► done / rejected
     Otherwise rescan the three in_review watermarks across all four
     surfaces; if awaiting_human with no new feedback, branch on
     park_reason + pending_fix_at. For a stuck validating-route
     transient park (`_VALIDATING_TRANSIENT_PARK_REASONS` with
     pending_fix_at unset, _try_recover_validating_transient_park
     returns "stuck"), route to resolving_conflict when the clean,
     unheld worktree is out of sync with the PR -- behind base, OR
     already on base but local HEAD != the live pr.head.sha (an
     unpushed local rebase) -- the dead-lock breaker base sync can't
     reach while parked. Every other awaiting-human shape (real agent
     question / dirty park / silent-crash / in_review-route transient)
     stays parked silently to preserve HITL. If no unread feedback at
     all, clear pending_fix_* and bounce to validating; otherwise
     honour IN_REVIEW_DEBOUNCE_SECONDS. Past the window, resume the
     dev with a `_build_pr_comment_followup` prompt and apply the
     validating fix-loop disposition. Watermarks advance ONLY to the
     max id fed to the dev. On a pushed fix, adjust review_round per
     pending_fix_at (in_review->fixing reset to 0; validating->fixing
     bump by 1) and flip directly to validating. Docs do not run on
     this exit.

   resolving_conflict (operator relabel, base-sync flow on actual
       rebase conflicts, or the fixing worktree-drift breaker; capped
       by MAX_CONFLICT_ROUNDS):
     clean rebase, HEAD moved      ─► push, validating (++conflict_round)
     base up-to-date no-op         ─► validating (++conflict_round, no push)
     conflicts ─► dev resumes      ─► push, validating (++conflict_round)
     ahead-of-remote recovered     ─► push, validating (++conflict_round)
     already-rebased, behind stale ─► force-publish, validating
       orchestrator-produced head     (++conflict_round); else
                                      diverged_branch park
     awaiting-human resume push    ─► push, validating (++conflict_round)
     drift pushed fix              ─► validating
     drift ACK / drift _on_question park ─► no relabel; rebase still
                                            unfinished, next tick
                                            re-enters resolving_conflict
     conflict_round >= MAX_CONFLICT_ROUNDS ─► park awaiting human
     pr merged externally / closed unmerged ─► done / rejected (terminal)

   blocked (per tick):
     all children = done       ─► parent=ready
     any child = rejected      ─► park HITL on parent
     dep_graph walk: any blocked child with all deps=done ─► child=ready

   umbrella (per tick):
     all children = done       ─► parent=done, issue closed
                                  (no implementation)
     any child = rejected      ─► park HITL on parent
     dep_graph walk: any blocked child with all deps=done ─► child=ready

   question (operator-applied; no automatic in/out transitions):
     fresh spawn          ─► DECOMPOSE_AGENT runs read-only in issue-N
                             worktree, posts answer, park awaiting human
                             (question_answer)
     human reply          ─► resume locked session, post follow-up,
                             park again
     commits / dirty /    ─► park (question_commits / question_dirty /
       timeout              question_timeout); worktree PRESERVED for
                             operator inspection; base sync skipped
                             while label is question
     agent silent         ─► park (question_silent); worktree torn down
     issue closed         ─► label=done, stamp question_closed_at,
                             cleanup (terminal)
     relabel to           ─► implementing's guard: clean worktree AND
       implementing         branch ─► drop question park, resume dev;
                             dirty or branch has commits ─► park
                             (question_unsafe_relabel)

   any stage ──► [park: awaiting_human=true]
                       (timeout, dirty tree, question, push fail,
                        unknown verdict, max rounds, retry budget
                        exhausted, failed checks, conflict-rounds
                        exhausted, invalid manifest)
                 wait for new human comment ──► resume locked
                                                 session (backend + args)
```
