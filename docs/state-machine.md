# Workflow state machine

This file documents the label-based state machine that drives every GitHub issue from pickup to terminal. It is split out of [`architecture.md`](architecture.md), which keeps the high-level overview, module map, and process / agent / push / event-log details.

The sections below cover:

- [Workflow labels](#workflow-labels) — the label set and what each one means.
- [Per-tick flow (`workflow.tick`)](#per-tick-flow-workflowtick) — how a single tick fans out across repos, partitions issues by label, and dispatches handlers; and the per-issue pinned-state schema the handlers read and write.
- [Stage handlers](#stage-handlers) — the per-stage internal flow, the user-content drift hook, and the transitions each handler may produce.
- [State transition (label lifecycle)](#state-transition-label-lifecycle) — the compact label-lifecycle reference diagram.

## Workflow labels

An issue should have at most one workflow label at a time. Non-workflow labels such as `bug` or `enhancement` are preserved; orchestrator label writes only swap labels from its own workflow set. Label names are part of the public contract because live GitHub issues carry them, so renaming or repurposing one is a migration.

The orchestrator also creates the non-workflow control label `hold_base_sync`; while present on an issue, it pauses per-tick base sync, `in_review` auto-merge/unmergeable handling, and `resolving_conflict` base rebases until the label is removed.

A second control label `backlog` is created for postponed work. While present on an issue, every per-tick handler skips it before the workflow label is even read, so the orchestrator does not pick up, decompose, or otherwise advance the issue. Removing the label hands control back to the state machine on the next tick — typically applied at issue creation to queue work that should sit until a human is ready.

| Label | Meaning |
|---|---|
| _(none)_ | Open issue not yet picked up by the orchestrator. |
| `decomposing` | The decomposer is deciding whether the issue is single-context or should become child issues. |
| `ready` | The issue is decomposed and has no unresolved blockers. |
| `blocked` | The issue is waiting on child issues or dependency edges. |
| `umbrella` | Parent issue with no implementation of its own; closes to `done` when all children resolve. |
| `implementing` | The dev agent is producing commits in a per-issue worktree. |
| `documenting` | The dev session runs a docs pass on the existing PR worktree. Reached only on the **final-docs handoff**: after `_handle_validating` approves + verifies + squashes, the marker `docs_final_pending=True` is set and the label flips to `documenting`. The docs pass advances to `in_review` (updating `agent_approved_sha` to the new pushed head when a docs commit lands AND validating already seeded that slot, so the AUTO_MERGE `agent_approved_sha == pr.head.sha` invariant survives; if validating's PR snapshot failed and left the slot unset, the docs push leaves it unset too so AUTO_MERGE stays gated). An explicit `DOCS: NO_CHANGE` verdict against a remote-clean head advances without pushing. Every former pre-approval entry (`implementing` PR open, `validating` pushed fixes, `fixing` PR-feedback pushes, `in_review` drift pushes, every `resolving_conflict` pushed exit) now hands straight back to `validating` so a fix or rebased branch only ever sees this single post-approval docs pass. |
| `validating` | The reviewer agent is checking the diff and may bounce fixes back to the dev agent. |
| `in_review` | A PR is open and ready for human review or auto-merge gates. |
| `fixing` | Unread in-review feedback (issue thread / PR conversation / inline review / PR review summary) or a human CI-fix request is queued during the quiet window or actively being addressed by the dev fix-loop. A successful fix flips DIRECTLY back to `validating` so the reviewer re-approves before auto-merge can proceed; when the rescan finds no unread feedback at all the bounce also targets `validating`. The pre-approval pushed-fix exit deliberately skips the `documenting` hop -- docs land in the final-docs pass after reviewer approval. |
| `resolving_conflict` | The orchestrator is trying to rebase an approved but unmergeable PR branch onto `origin/<base>`. |
| `question` | Operator-applied read-only Q&A label: the orchestrator runs the decomposer agent in the per-issue worktree, posts the answer to the issue thread, and waits on a human reply or a manual close. No PR is opened on this label. |
| `done` | Terminal success; the PR merged, an umbrella parent resolved after all children reached `done`, or a `question` issue was closed by the operator. |
| `rejected` | Terminal rejection; the PR or issue was closed without merge. |

## Per-tick flow (`workflow.tick`)

Each tick the polling loop fans out across **every configured repo**. `config.default_repo_specs()` returns a list of `RepoSpec(slug, target_root, base_branch, remote_name, parallel_limit)` — one entry per `REPOS` line, or a single entry derived from the legacy `REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH` / `REMOTE_NAME` quartet when `REPOS` is unset (with `parallel_limit` taken from `MAX_PARALLEL_ISSUES_PER_REPO`, default 1). Each `REPOS` entry may override `remote_name` via its optional fourth pipe-separated field (default `origin`) and `parallel_limit` via its optional fifth (default `MAX_PARALLEL_ISSUES_PER_REPO`).

`main._run_tick` fans the per-repo `workflow.tick(gh, spec)` calls out across a `ThreadPoolExecutor` (one worker thread per configured repo) so a slow repo does not delay the others; a per-repo exception is logged and swallowed so one wedged repo cannot stop the others from advancing this tick. The single-repo legacy path stays in-thread (no executor) to keep deployments without `REPOS` unchanged. Each `GitHubClient` is constructed once at startup with `repo_spec=spec` and `ensure_workflow_labels` runs per repo so a fresh target repo bootstraps its labels on first connect.

A single `threading.BoundedSemaphore(MAX_PARALLEL_ISSUES_GLOBAL)` is built once at startup and threaded through every `workflow.tick(gh, spec, global_semaphore=...)` call. Each tick acquires it around every `_process_issue` invocation so workers from different repos contend on the same semaphore — total in-flight per-issue handlers across all repos never exceeds `MAX_PARALLEL_ISSUES_GLOBAL` (default 3) regardless of how many `parallel_limit` slots each repo declares.

Within one repo, `spec.parallel_limit` caps how many issues `workflow.tick` may advance concurrently on a single tick. Default is 1 (legacy one-at-a-time behavior); each `REPOS` entry can override it via its optional fifth pipe-separated field, and the global `MAX_PARALLEL_ISSUES_PER_REPO` (default 1) supplies the default for entries that omit the field. `parallel_limit == 1` keeps the legacy sequential, streaming loop directly over `gh.list_pollable_issues()` so a partial enumeration failure still processes everything yielded before the failure.

`parallel_limit > 1` materializes the eligible-issue set up front (to bound `max_workers` correctly) and partitions it by workflow label:

- **Family-aware labels** — `decomposing`, `blocked`, `umbrella`, and unlabeled (pickup) issues — read and write cross-issue state (parent ↔ child). Two of these running at once could race a parent's child-state write against the child's own handler on a sibling thread. They are folded into a SINGLE drain task that processes them sequentially on one worker thread; this caps the family bucket's executor footprint at exactly one slot regardless of how many family-aware issues are pending, so the other `limit - 1` slots stay free for fan-out work.
- **Fan-out labels** — `ready`, `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict` — only touch their own per-issue pinned state and worktree. Each is submitted as its own future and runs concurrently up to `parallel_limit`. The two buckets share one executor capped at `min(parallel_limit, total_tasks)` and the family drain can overlap with non-family workers, so a slow decomposer no longer blocks unrelated implementing / documenting / validating issues on the same tick.

Only issue numbers cross the thread boundary — each worker calls `gh._for_worker_thread()` to mint a fresh `GitHubClient` (and through it a fresh `Github` / `Requester` / `Repository`) and refetches its Issue against that client, so every in-flight HTTP call is the sole consumer of its requester's state (PyGithub's per-request state is not documented as thread-safe across a shared `Requester`). The label used for partitioning is read on the caller thread; a lazy-load failure on one issue's labels is logged and that issue is conservatively routed into the family bucket where the per-issue try/except picks up any sustained failure.

Inside `workflow.tick(gh, spec)`, before any issue is dispatched the tick runs `_refresh_base_and_worktrees(gh, spec)`: a single `git fetch <spec.remote_name> <spec.base_branch>` in `spec.target_root` (the remote name defaults to `origin` but is overridable per `REPOS` entry via the fourth pipe-separated field, so a `REPOS=...|private|2` row fetches from `private/<base>`), then per-issue dispatch on each existing worktree under `<WORKTREES_DIR>/<owner>__<name>/issue-*`. The per-stage `_ensure_*_worktree` helpers only fetch base on (re)creation, so a worktree that survives across ticks would otherwise stay anchored at whatever `<remote>/<base>` looked like when it was first added.

Two paths depending on whether a PR already exists for the issue:

- **Pre-PR worktrees** (no `pr_number` in pinned state) get a clean-tree `git rebase origin/<base>` directly — there is no remote to push to, so the local branch can be kept linear without publishing a rewrite.
- **PR-having worktrees** in `validating` / `in_review` / `fixing` are detoured to `resolving_conflict` instead (via `_route_pr_worktree_to_resolving_conflict`: post a PR notice, seed `conflict_round` only when absent, flip the label) so the existing `_handle_resolving_conflict` handler does rebase + force-with-lease push + relabel-to-`validating` (the same target as the base-up-to-date no-op) in one consistent flow.

Applying `hold_base_sync` to an issue skips both paths for that issue; removing the label lets the next tick perform the accumulated base sync once. The `question` workflow label skips base sync unconditionally for the same read-only reason `_handle_question` already tears down its own worktree on every safe exit — merging `origin/<base>` into a question worktree would either accrete commits on a read-only branch or mask the inspection state of an unsafe park (`question_commits` / `question_dirty` / `question_timeout`).

A local-only rebase on a pushed branch would otherwise diverge local HEAD from `pr.head.sha` and break the validating reviewer (it reads local HEAD, so it would snapshot `agent_approved_sha` to a SHA that isn't on the PR), `_squash_and_force_push`'s `--force-with-lease=<original_head>` (the lease compares against the un-rebased remote tip), and AUTO_MERGE's `agent_approved_sha == pr.head.sha` gate. The detour works under `AUTO_MERGE=off` too — `_handle_resolving_conflict` never reads AUTO_MERGE, it just does rebase + push + relabel.

The detour deliberately does NOT call `_bump_in_review_watermarks` (the `_handle_in_review` analog runs that AFTER scanning new comments — running it here, before any handler scans, would silently mark unread human "do not merge" / fix-request comments as consumed and AUTO_MERGE could land the PR over them). The orchestrator's own PR notice is filtered out via `orchestrator_comment_ids` on the next in_review scan, so leaving the watermark alone is safe.

The detour also skips when `awaiting_human=True` because `_handle_resolving_conflict`'s awaiting-human branch returns early without rebasing unless a new human comment arrived; relabeling here would just hide the existing park behind a `resolving_conflict` label without progress, including the documented `AUTO_MERGE=off` unmergeable-park case.

Before relabeling, the detour fetches `gh.get_pr(pr_number)` and skips when `pr_state != "open"`: a just-merged PR advances `origin/<base>`, so the still-validating / still-in_review / still-fixing worktree pointed at the now-stale branch is naturally behind base; without this gate the refresh would post an "auto-resolution" notice and relabel to `resolving_conflict` on a PR the next handler call would finalize to `done` (or `rejected` for a closed-without-merge PR).

A `gh.get_pr` failure is treated as "leave alone" so the handler retries from a stable label rather than racing a half-known PR state. Issues already labeled `resolving_conflict` are also skipped (the handler runs this tick anyway).

Rebase is used across both paths to keep issue branches linear after sibling PRs land. Dirty worktrees (in-flight agent edits, crash-recovered trees) are skipped, and on a pre-PR content conflict the rebase is aborted so the worktree stays on its pre-rebase SHA. For PR branches, every pushed rebase resets `review_round`, so the reviewer must approve the rewritten head before auto-merge can proceed. Failures are logged and swallowed; keeping every issue moving matters more than perfect base sync.

Then `gh.list_pollable_issues()` yields all open non-PR issues plus closed non-PR issues still labeled with any non-terminal workflow label (`implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, or `question`). The closed-`in_review` / `fixing` / `resolving_conflict` sweep is what makes the manual-merge path land cleanly: a human-merged PR with a `Resolves #N` footer auto-closes issue N before the orchestrator can flip the label, and without the sweep `_handle_in_review` / `_handle_fixing` / `_handle_resolving_conflict` would never run on it. The closed-`implementing` / `documenting` / `validating` sweep covers the same external-merge race when the human merges the PR before the orchestrator reached `in_review`: those handlers each run `_finalize_if_pr_merged` at entry, so surfacing the closed issue here is what lets them flip the label to `done` instead of leaving it stuck at the in-flight stage (an umbrella parent would otherwise aggregate on the stale child label forever). The closed-`question` sweep does the same for the Q&A path: a human closing the issue is the terminal signal `_handle_question` consumes to finalize the issue to `done` and clean up the per-issue worktree/branch.

For every yielded issue:

1. Read its workflow label (one of `decomposing/ready/blocked/umbrella/implementing/documenting/validating/in_review/fixing/resolving_conflict/question/done/rejected`).
2. Dispatch by label. The full lifecycle (no label → `decomposing` → `ready`/`blocked`/`umbrella` → `implementing` → `validating` → `documenting` (final-docs hop after reviewer approval) → `in_review` → `fixing` (on fresh PR feedback) or `resolving_conflict` (optional auto-merge detour) → `done`/`rejected`) is implemented; `done` and `rejected` are terminal no-ops, every other label routes to its handler. The operator-applied `question` label is an out-of-lifecycle branch (no automatic stage transitions in or out — see [`_handle_question`](#_handle_question-label-question)). Every handler receives the active `RepoSpec`, so `git worktree add`, `git fetch origin <base>`, push-token resolution (`config._resolve_github_token(spec.slug)`), and PR-base selection all flow from the spec rather than module-level `config.REPO` / `config.TARGET_REPO_ROOT` / `config.BASE_BRANCH` reads.

Per-issue durable state lives in a single **"pinned" comment** on the issue (`<!--orchestrator-state {...json...}-->`). The keys it holds:

- `dev_agent` + `dev_session_id` — the **raw full command spec** that handled this issue (first token plus any configured backend-CLI args, e.g. `"codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"`), re-parsed via `config._parse_agent_spec` on every resume so both the backend AND its args stay locked to whatever the first spawn used; plus the agent's session id.
- `review_agent` — the spec the most recent reviewer spawn used. Reviewer is fresh per round so this is traceability only, not a lock.
- `decomposer_agent` + `decomposer_session_id` — parents only; same raw-full-spec pinning + lock-on-first-spawn semantics as `dev_agent`.
- `question_agent` + `question_session_id` — `question`-stage issues only; same raw-full-spec pinning + lock-on-first-spawn semantics as `dev_agent`. Seeded from `DECOMPOSE_AGENT` on the first spawn and re-parsed on every awaiting-human resume so a multi-turn Q&A keeps the same backend + args. `last_question_at` stamps the most recent spawn; `question_closed_at` stamps the terminal flip to `done` when the operator closes the issue.
- `children` — parents only; child issue numbers, used by `_handle_blocked`.
- `dep_graph` — parents only; `{child_idx_str: [child_idx, ...]}` because GitHub has no first-class blocks-issue relation.
- `decomposed_at`, `pickup_comment_id`.
- `user_content_hash` — SHA-256 over title + body + non-orchestrator comments; updated whenever the orchestrator reacts to a human edit so future ticks have a stable baseline.
- `branch`, `pr_number`, `review_round`.
- `retry_window_start` + `retry_count` — per-issue 24h fresh-spawn budget; shared between implementing and decomposing.
- `awaiting_human`, `last_action_comment_id`.
- `pr_last_comment_id` — in_review high-watermark across the issue thread + PR conversation comments, which share the IssueComment id space. Seeded at validating's approval branch (before the `documenting` hop, before the `in_review` handoff) so the orchestrator's own automated comments don't replay as fresh feedback, and bumped past any park comment so an HITL ping doesn't replay either. The approval comment, squash comment, and `agent_approved_sha` snapshot ride through the final-docs hop untouched; the watermark itself may be **ratcheted** by `_handle_documenting`'s final-docs success exits (via `_ratchet_in_review_watermark_for_final_docs`) past any issue-thread reply the awaiting-human resume consumed, so the next in_review tick does not replay it as fresh PR feedback. The ratchet reuses `_latest_pr_comment_ids` so an unread PR-conversation comment whose id falls below the consumed-through threshold is preserved (the seed walk applies `consumed_through` to the issue-thread surface only).
- `pr_last_review_comment_id` — separate watermark for inline PR review comments, which live in their own id space.
- `pr_last_review_summary_id` — separate watermark in the PullRequestReview id space, distinct from both IssueComment and PullRequestComment ids.

  The watermark *only* advances from review IDs that survived `gh.pr_reviews_after`'s state/body filter — non-empty `CHANGES_REQUESTED` or `COMMENTED` — so `APPROVED`, `DISMISSED`, `PENDING`, and empty-body reviews **never** bump it. `_bump_in_review_watermarks` mirrors the same filter and advances strictly from the filtered list.

  This is safe because the same filter runs on every scan, so an `APPROVED` review id sitting above the watermark is harmlessly re-skipped each tick rather than re-forwarded.
- `agent_approved_sha` — the head SHA the reviewer agent OK'd; `_handle_in_review` keys AUTO_MERGE on this since the agent posts an issue comment, not a real PR review. Seeded by `_handle_validating`'s approval branch (post-squash local head, or the reviewed head when squash is off) and **then updated by `_handle_documenting` on the final-docs hop** when a docs commit lands AND the slot was already seeded — the docs pass owns the AUTO_MERGE `agent_approved_sha == pr.head.sha` invariant across the new head. When validating's PR snapshot raises (recoverable network/PyGithub blip), it deliberately leaves the slot unset; the docs push respects that and does NOT seed it, so AUTO_MERGE stays gated until the next reviewer round approves explicitly. Cleared by `_handle_validating` on every pushed dev fix (CHANGES_REQUESTED, awaiting-human resume, user-content drift "pushed", transient-park recovery push) so AUTO_MERGE cannot land the freshly-pushed head against a stale prior approval, by `_handle_fixing` whenever it pushes a fix (which then bounces directly back to `validating`), by `_handle_in_review`'s drift pushed and ACK outcomes (both bounce directly back to `validating` — the pre-approval drift exit skips the `documenting` hop), and by `_handle_documenting`'s drift block when a body edit fires during the final-docs hop (drops the marker too and resets `review_round=0` so the docs pass falls back to the `validating` route). AUTO_MERGE cannot land the PR until the reviewer re-approves against the updated body.
- `docs_final_pending` — boolean marker set by `_handle_validating`'s approval branch right before flipping the label to `documenting`. `_handle_documenting` consumes the marker on its success exits (advance to `in_review` instead of `validating`, update `agent_approved_sha` when a docs commit moves the head AND the `final_docs_approval_seeded` sentinel is set, and ratchet `pr_last_comment_id` past any issue-thread reply consumed by the awaiting-human resume); the drift block clears it if a body edit invalidates the prior approval mid-hop. **`_handle_validating` also clears it at the top of every tick**, so any prior cycle's stale marker that survived an aborted handoff (operator relabel back from documenting, drift unwind, PR-worktree refresh detour landing on validating) cannot be carried by a later CHANGES_REQUESTED / awaiting-human / drift / transient-park exit into documenting and trigger a premature `in_review` handoff that skips the required re-review. Without the marker, the docs pass keeps its legacy pre-approval route to `validating`.
- `final_docs_approval_seeded` — companion sentinel to `docs_final_pending`, set by `_handle_validating`'s approval branch in the SAME code block that seeds `agent_approved_sha` and the in_review watermarks (inside the `else` arm of its `gh.get_pr()` try) **and only when the local SHA being persisted is non-empty**: if `_head_sha()` returned empty earlier in the tick, `agent_approved_sha` is never actually persisted this round and the sentinel must stay False so the docs push cannot promote the docs commit's SHA into a slot the reviewer never confirmed. `_handle_documenting`'s push exit gates the `agent_approved_sha` update on this sentinel — without it (validating's PR snapshot raised OR its local SHA was empty) a stale non-`None` `agent_approved_sha` left over from an earlier round would be silently promoted to the new docs head and AUTO_MERGE could land the PR against a SHA THIS round's reviewer never confirmed. Documenting clears it alongside `docs_final_pending` on every success exit and in the drift block, and `_handle_validating` clears it (with `docs_final_pending`) at the top of every tick for the same reason described above.
- `pending_fix_at` — ISO timestamp recorded by `_handle_in_review` when it routes fresh PR feedback to `fixing`. Surfaces to operators that the issue is in the `fixing` quiet window or actively being fixed; cleared on a pushed fix (which then bounces directly back to `validating`) or when the rescan finds no unread feedback and bounces directly back to `validating`.
- `pending_fix_issue_max_id` / `pending_fix_review_max_id` / `pending_fix_review_summary_max_id` — per-namespace bookmarks for the PR-feedback ids that triggered the `fixing` route. They are hints for `_handle_fixing` and forensics, NOT watermarks — the in_review watermarks are deliberately left behind so the fixing rescan can re-discover the triggering comments and build the dev-resume prompt. Cleared alongside `pending_fix_at` on the same exits.
- `merged_at` / `closed_without_merge_at` — terminal stamps.
- etc. (see `github.PINNED_STATE_MARKER` / `PINNED_STATE_RE` and `read_pinned_state` / `write_pinned_state`).

The legacy `codex_session_id` key written before the configurable-backend rollout is still honored on read and treated as codex.

## Stage handlers

### `_handle_pickup` (no label → `decomposing` or `implementing`)
- **Trigger**: open issue with no workflow label.
- **Input**: issue title/body/comments; `config.DECOMPOSE` (default on); `config.ALLOWED_ISSUE_AUTHORS` (default empty → allow all).
- **Action**: when `ALLOWED_ISSUE_AUTHORS` is set, an issue authored by anyone outside the list is silently skipped (log only); otherwise post a "picking this up" comment, anchor `pickup_comment_id` for the in_review legacy migration, snapshot `user_content_hash` over title + body + non-orchestrator comments so future ticks can detect a human edit mid-flight, then route:
  - `DECOMPOSE=on` → label `decomposing`, fall into `_handle_decomposing`.
  - `DECOMPOSE=off` → label `implementing`, fall into `_handle_implementing` (legacy bootstrap path).

### User-content drift detection (every per-tick handler)
Every per-tick handler computes `_compute_user_content_hash(issue, orchestrator_comment_ids)` at the start of the tick and compares it to the stored `user_content_hash`. The hash covers the issue title, body, and every comment that is human-authored.

Non-human content is filtered four ways:

- pinned-state comments by `PINNED_STATE_MARKER`;
- orchestrator-posted comments by `_ORCH_COMMENT_MARKER` embedded in the body (every `_post_issue_comment` / `_post_pr_comment` wraps the body via `_with_orch_marker`; the marker is an HTML comment, invisible in rendered Markdown, and survives id-cap eviction on long-lived issues);
- legacy orchestrator comments by id from `orchestrator_comment_ids` (covers comments posted before the marker was introduced, until their id is evicted from the bounded cap);
- third-party Bot/App accounts (Dependabot, Renovate, CI bots) by GitHub's `user.type == "Bot"` structural flag (a periodic dependency-bump comment would otherwise re-trigger drift on every tick it posts).

Author-login matching is intentionally avoided because the orchestrator PAT is often shared with a human reviewer's GitHub account; the `user.type` flag is a structural account property and does not conflict with that constraint. So the hash drifts on body edits AND on new human comments (acceptance criteria added mid-flight).

`_detect_user_content_change` durably persists the baseline on its FIRST encounter via `gh.write_pinned_state` so an early-return tick (awaiting-human-with-no-new-comments, child-waiting-on-deps, debounce) cannot silently absorb a later edit as the new baseline. On drift the action depends on where in the lifecycle the issue is:
  - `decomposing` / `ready` / `blocked` / `umbrella` (no implementation has started yet) → route back to `decomposing` via `_route_drift_to_decomposing`: drop `decomposer_session_id` (the fresh spawn next tick derives a brand-new manifest against the updated body, not a resume of the stale session), wipe the manifest tracking (`children`, `dep_graph`, `expected_children_count`, `umbrella` flag), clear park flags, post a `:pencil2: issue content changed` notice on the issue, and set the label to `decomposing`.

    Crucially, `decomposer_agent` is PRESERVED across this transition: lock-on-first-spawn means the recorded role spec stays locked for the rest of the issue's lifecycle, even across drift events, so a mid-flight `DECOMPOSE_AGENT` env flip cannot retarget an in-flight issue at a different backend (the fresh spawn picks up the recorded spec via `_read_decomposer_session`).

    For parents with previously-tracked children (in-flight in `blocked`, all-done after the `blocked` -> `ready` transition, or any state for `umbrella`), the child issue numbers are listed in the notice as ORPHANED — the orchestrator no longer tracks them, so the operator must close any that no longer apply.

    Wiping the manifest tracking is what stops `_handle_decomposing`'s half-finished recovery branch from firing on the next tick (it keys on `expected_children_count is not None OR children non-empty`); without it a `ready` parent whose children all finished would loop back to `blocked` without ever re-running the decomposer.

    This is deliberately destructive over "park awaiting human" because silently absorbing a child edit (the old behavior) would let `_handle_ready` later see the new baseline as already consumed and skip the re-decomposer even when the edited child now needs splitting; and an edited umbrella with done children would close to `done` against the stale manifest.
  - `implementing` / `validating` / `in_review` / `resolving_conflict` (a dev session exists and possibly a PR) → on drift the handler:
    1. posts a `:pencil2: issue body changed; resuming dev session` notice — on the issue for implementing/validating, on the PR conversation for in_review and resolving_conflict;
    2. advances `last_action_comment_id` past every visible issue-thread comment via `_mark_drift_comments_consumed`, and bumps the in_review watermarks via `_bump_in_review_watermarks` in the in_review case;
    3. resumes the locked dev session with `_build_user_content_change_prompt`, which quotes the updated title, body, AND the current conversation so a new acceptance criterion posted as a comment is surfaced to the dev;
    4. routes the result through `_post_user_content_change_result`.

    Result routing in `_post_user_content_change_result`:

    - a clean pushed fix hands straight back to `validating` from every stage that runs the drift resume — `in_review`, `resolving_conflict`, and `validating` (the last bumps `review_round` and clears `agent_approved_sha` so AUTO_MERGE cannot land the new head against a stale approval). All three deliberately skip the `documenting` hop -- the single docs pass is deferred to the post-approval hop, so the reviewer re-runs against the new branch directly. From `implementing` the drift path runs the `_on_commits` path to open/push the PR (which also relabels straight to `validating` now);
    - a no-commit reply is treated as an ack ONLY when it carries the explicit `ACK: <reason>` marker the resume prompt instructs the dev to emit when the existing work already satisfies the edit.

      The dev's justification is posted on the issue as an FYI and the handler does NOT park awaiting_human, so a harmless clarification doesn't stall the issue;
    - any other no-commit response (a real clarification question, an ambiguous comment, an empty message) falls back to `_on_question` and parks awaiting human.

      Without the explicit marker requirement, a clarification question would be silently swallowed as "existing work satisfies" and the issue would advance with `awaiting_human=False`, stranding the question.

    The watermark advance is what prevents the validating → in_review handoff from later replaying the same human comment via `_seed_watermark_past_self` and triggering a duplicate dev resume.

    Per-stage specifics:

    - For the `in_review` drift specifically, BOTH the "pushed" and "ack" outcomes reset `review_round` and clear `agent_approved_sha` -- a content drift invalidates the prior reviewer approval (it was for the old requirements), so AUTO_MERGE must not land the PR until the reviewer re-evaluates against the updated body/comments. Both outcomes also share the same destination: a DIRECT bounce back to `validating`. The pre-approval drift exit deliberately skips the `documenting` hop -- docs land in the final-docs pass after reviewer approval, so running the docs stage against an unapproved diff would just push a no-op and waste a tick.

      The in_review drift also captures unread PR-conversation comments past `pr_last_comment_id` BEFORE posting the orchestrator's notice and includes them in the dev's followup prompt — issue thread and PR conversation share the IssueComment id space, so an unread PR comment whose id falls between the prior watermark and the issue-thread max would otherwise be silently consumed by `_bump_in_review_watermarks` (which advances the shared watermark based on `latest_comment_id(issue)`) and never forwarded.
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
- **Internal flow**:
  1. If `awaiting_human`: re-check for new human comments since `last_action_comment_id`; if any, **resume** the decomposer session via `run_agent(decomposer_agent, ...)` with that text. If no new comments, return.

     The full spec persisted in `decomposer_agent` — backend AND configured CLI args (model, reasoning effort, etc.) — is re-parsed via `_read_decomposer_session` and reused for the resume; flipping `DECOMPOSE_AGENT` in env does not migrate the in-flight issue (neither the backend nor the args).

     The pre-spec legacy bare value (`"codex"` / `"claude"`) round-trips to `(backend, ())` so older sessions keep the no-args shape they ran with.
  2. Otherwise: gate on the **per-issue retry budget** (shared with `implementing` — both consume the same daily counter on purpose). If exhausted, park awaiting human.
  3. Ensure a per-issue worktree (read-only — the decomposer never commits, but the agent still wants `git ls-files` / `wc -l` context).
  4. Build the **decomposer prompt** (issue body + recent comments + sizing rule of thumb + the manifest schema).

     Resolve the spec for this issue via `_read_decomposer_session(state)` — `(decomposer_spec, decomposer_backend, decomposer_args, _)` — falling back to the current config (`DECOMPOSE_AGENT_SPEC`, `DECOMPOSE_AGENT`, `DECOMPOSE_AGENT_ARGS`) only for the first-ever spawn.

     **Persist the raw full spec to `decomposer_agent` BEFORE invoking `run_agent`** so a backend hiccup that yields no `session_id` — yet still produces a manifest, parks awaiting human, or commits — does not leave `decomposer_agent` unset (a later `DECOMPOSE_AGENT` flip would otherwise retarget the next awaiting-human resume at a backend that never ran on this issue, and storing only the parsed backend would strip the configured CLI args on subsequent resumes).

     Then spawn via `run_agent(decomposer_backend, prompt, wt, extra_args=decomposer_args)`. On a new session id, also persist `decomposer_session_id`.
  5. **Read-only check**: if the worktree now has new commits or dirty files, park awaiting human. The decomposer is supposed to be read-only; otherwise the implementer recovery path in `_handle_implementing` would later see the leftover commits and push decomposer-authored work as if it were implementation.
  6. Parse the manifest from `result.last_message` via `_parse_manifest` (regex captures the fenced ` ```orchestrator-manifest ` block; structural validation rejects unknown decisions, bad child shape, self-deps, cycles, and >10 children):
     - **invalid manifest** → park awaiting human with the parse error and the agent's last message quoted (same recovery as a malformed reviewer verdict).
     - **no fenced block** → treat as a question; park with the message quoted (mirrors `_on_question` from implementing).
     - **decision == "single"** → post a one-line "fits in one context" comment with the rationale, set label `ready`, stamp `decomposed_at`. `_handle_ready` picks it up next tick.
     - **decision == "split"** → crash-safe creation in three phases. The decomposer is no longer asked to emit a final docs-update child — `_handle_validating`'s approval branch runs a final-docs pass on the squashed head before the `in_review` handoff, and every pre-approval push (`implementing` PR open, `validating` pushed fixes, `fixing` PR-feedback fixes, `in_review` drift pushes, every `resolving_conflict` push) now hands straight back to `validating` so the reviewer re-runs against the new branch directly. The single post-approval docs pass keeps docs in sync for everything, so docs stay current without a synthetic child.
       1. For each child call `gh.create_child_issue(...)` with label `blocked` regardless of dependencies, and seed the child's pinned state with `parent_number`. `create_child_issue` prepends `Parent: #<n>` to the body (no auto-close keyword).

          Child-state seeding is mandatory — failure persists the partial `children` list and parks awaiting human, so no orphan child is left runnable.
       2. Persist `children`, `dep_graph` (`{child_idx_str: [child_idx, ...]}`), and `umbrella` (from the manifest's optional boolean, default false) on the parent. Post the summary comment, set parent label `umbrella` when the flag is true and otherwise `blocked`, stamp `decomposed_at`.
       3. Activate no-dep children by flipping their label `blocked` → `ready`.

          This is best-effort because `_handle_blocked`'s / `_handle_umbrella`'s walk also treats no-dep children as deps-satisfied, so a crashed activation step is recovered on the next tick.
- **Pre-flight (half-finished recovery)**: if `children` is already set on the parent but the label is still `decomposing`, a prior tick crashed between child creation and the parent label flip. Re-running the decomposer would create duplicates, so the handler short-circuits:
  - when not awaiting_human, flip the parent to `umbrella` (when the persisted `umbrella` flag is true) or `blocked` and let the matching handler activate children;
  - when awaiting_human (parent state was parked mid-creation), hold and require manual intervention.
- **Pre-flight (DECOMPOSE kill switch, mid-flight)**: if `config.DECOMPOSE` is off when this handler runs (operator restarted with the rollout disabled while the issue was already labeled `decomposing` or parked there), bail out before any decomposer spawn: post a routing comment, clear the decomposer-side `awaiting_human`/`park_reason` so the legacy implementing flow doesn't trip its resume branch on stale state, flip the label to `implementing`, and fall into `_handle_implementing`.

  The half-finished recovery above runs first and is unaffected — abandoning orphan children that already exist on GitHub just because new decompositions are now disabled is not what a kill switch should do.
- **Output**: parent label moved to `ready` / `blocked` / `umbrella`, OR a HITL park.

### `_handle_ready` (label `ready` → `implementing`)
- **Trigger**: each tick while the label is `ready`. Reached by either a `single`-decision parent or by a freshly-created child.
- **Action**: if `pickup_comment_id` is unset (the common path for auto-created children), post a "picking this up; starting implementation" comment and seed `created_at` + `pickup_comment_id` so the in_review legacy migration has its anchor.

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

     This finalize-on-poll is what stops a human merge of a child's PR — while the child label was still a stale in-flight stage like `validating` — from stranding the umbrella aggregation. A child closed manually before reaching `in_review` with no merged PR is still invisible to `list_pollable_issues` (the closed-issue sweep only yields the non-terminal label set, not closed-without-merge issues that happen to share those labels), so its workflow label stays frozen and the parent surfaces it to a human as before. `in_review` is intentionally excluded — the closed-`in_review` sweep finalizes that transient on the next tick.
  6. If every child is `done` → post a summary comment, flip parent → `ready`. The next tick `_handle_ready` picks it up and the implementer takes over.
  7. Otherwise walk children: any `blocked` child whose recorded dependencies are all `done` gets relabeled `ready`. A child with no recorded deps is also flipped (vacuous all-done over an empty list) — this recovers no-dep children that the decomposer's same-tick activation step left as `blocked`.

     This walk both unblocks middle-of-the-graph children and rescues stuck activations without waiting on the parent.
- **Output**: parent → `ready` (all done), OR a sibling unblocked, OR a HITL park (rejected child, manually-closed child, or unattributed `blocked`), OR a no-op for a child still waiting on its dependencies.

### `_handle_umbrella` (label `umbrella`)
- **Trigger**: each tick while the label is `umbrella` (only ever a parent — set by the decomposer when the manifest's `umbrella` boolean is true).
- **Input**: pinned `children` and optional `dep_graph` on the parent.
- **Internal flow**: mirrors `_handle_blocked` for the rejected / manually-closed checks and the dep-graph activation walk; the only difference is the all-done terminal. The same `_finalize_if_pr_merged` recovery for `manually_closed` children runs here, so an externally-merged child whose label never advanced past an in-flight stage no longer strands the umbrella aggregation.

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

     The full spec persisted in `dev_agent` — backend AND configured CLI args (model, reasoning effort, etc.) — is re-parsed via `_read_dev_session` and reused for the resume; flipping `DEV_AGENT` in env does not migrate in-flight issues (neither the backend nor the args).

     Legacy bare values (`"codex"` / `"claude"` or the pre-spec `codex_session_id` key) round-trip to `(backend, ())` so older sessions keep the no-args shape they ran with.
  2. Otherwise: ensure a per-issue worktree at `<WORKTREES_DIR>/<owner>__<name>/issue-<n>` (the slug subdir keeps two repos with the same issue number isolated on disk) on branch `orchestrator/issue-<n>`. Worktrees with unpushed commits are reused (crash recovery); otherwise force-removed and recreated from `origin/<spec.base_branch>` in `spec.target_root`.
  3. If the worktree already has commits (recovered), skip the agent and go straight to push.
  4. Else gate the run on the **per-issue retry budget** (`MAX_RETRIES_PER_DAY`, default 3): a 24h window opens at the first counted spawn and resets after 24h; only fresh spawns count, not human-resume runs or recovered-worktree pushes. If the cap is exhausted, park awaiting human and return.
  5. Else build the **implementer prompt** (issue body + recent comments + "commit, do not push").

     Resolve the spec for this issue via `_read_dev_session(state)` — `(dev_spec, dev_backend, dev_args, _)` — falling back to the current config (`DEV_AGENT_SPEC`, `DEV_AGENT`, `DEV_AGENT_ARGS`) only for the first-ever spawn.

     **Persist the raw full spec to `dev_agent` BEFORE invoking `run_agent`** so a backend hiccup that produces commits without surfacing a session id (empty codex `-o` file, unparseable claude JSONL line) does not leave `dev_agent` unset; a later `DEV_AGENT` flip would otherwise retarget the next resume at a backend that never ran on this issue, and storing only the parsed backend would strip the configured CLI args on subsequent resumes.

     Then spawn via `run_agent(dev_backend, prompt, wt, extra_args=dev_args)`. On a new session id, also persist `dev_session_id`.
  6. Branch on result:
     - `timed_out` → park awaiting human (`@HITL_HANDLE`).
     - new commits + clean tree → `_on_commits`: push branch, open PR (or reuse an existing open one), comment `:sparkles: PR opened: #N`, set label `validating` (the docs pass only runs as the final-docs handoff after the reviewer approves — there is no longer a pre-review docs hop here), reset `review_round=0` and `retry_count=0` (next bounce back into implementing starts fresh).
     - new commits + dirty files → `_on_dirty_worktree`: park; refuse to publish a partial branch.
     - no new commits → `_on_question`: post the agent's last message as a HITL question, park.
- **Output**: a pushed branch + open PR + label moved to `validating` (the reviewer runs on the next tick), OR a HITL park.

### `_handle_documenting` (label `documenting`)
- **Trigger**: each tick while the label is `documenting`. Set only by the **final-docs handoff** in `_handle_validating`'s approval branch (after verify + squash), which writes the marker `docs_final_pending=True` before flipping the label. Every former pre-approval entry (`implementing` PR open, `validating` pushed fixes, `fixing` PR-feedback pushes, `in_review` drift pushes, every `resolving_conflict` pushed exit) now hands straight back to `validating` and never visits `documenting` outside the final-docs hop. Also runs on closed-`documenting` issues yielded by the polling sweep so an externally-merged PR finalizes to `done`.
- **Input**: pinned `pr_number`, `branch`, `dev_agent`/`dev_session_id` (the docs pass runs AS the dev role and reuses the same locked spec / session id — there is no separate `documenting_agent`), plus `docs_checked_sha` / `docs_verdict` / `silent_park_count` watermarks. The `docs_final_pending` boolean discriminates the two trips: when True (set only by `_handle_validating`'s approval branch), success exits advance to `in_review` instead of `validating`, and a pushed docs commit also updates `agent_approved_sha` to the new head so the AUTO_MERGE `agent_approved_sha == pr.head.sha` invariant survives the final docs commit.
- **Internal flow**:
  0. **External-merge / closed-issue short-circuit.** Identical to the implementing / validating entry checks: `_finalize_if_pr_merged` flips a merged PR to `done`; `_finalize_if_issue_closed` flips a closed issue to `rejected` (only emitting `pr_closed_without_merge` + running `_cleanup_terminal_branch` when the linked PR is also closed). The same fetch-failure / merged-PR deferral the implementing handler relies on applies here.
  1. **`pr_number` missing → park.** Documenting only runs against an existing PR worktree; without a pinned `pr_number` the handler cannot anchor on the dev's branch (branching off base would orphan the docs commit from the implementing PR). Park awaiting human with `park_reason="missing_pr_number"` and tell the operator to relabel back to `implementing`. Idempotent under `awaiting_human` — a no-reply re-tick returns without re-posting.
  2. **User-content drift.** If the hash changed, post a `:pencil2: issue body changed; resuming docs pass on the updated requirements.` notice, advance `last_action_comment_id` past every visible issue-thread comment via `_mark_drift_comments_consumed`, clear any prior park flags, refresh `user_content_hash`, and fall through to the fresh-spawn branch — the docs prompt embeds `issue.body` so a normal fresh spawn picks up the edit without a separate resume-prompt detour (unlike validating, which builds a dedicated user-content-change prompt).
  3. **Awaiting-human + no new comment → early return.** When `awaiting_human` is set and no human reply has arrived since `last_action_comment_id` (and the drift branch above didn't clear the flag), return BEFORE the fetch + ahead/behind check. Otherwise a transient `fetch_failed` / `diverged_branch` failure would re-post its park comment every tick.
  4. **Ensure the PR worktree** (`_ensure_pr_worktree`, restored from `origin/<branch>` so the dev's commits are intact) and refresh the remote-tracking ref via `_authed_fetch` BEFORE the ahead/behind check. A fetch failure parks with `park_reason="fetch_failed"`.
  5. **Ahead/behind check** vs. the just-fetched `<remote>/<branch>`:
     - `behind > 0` (worktree diverged) → park with `park_reason="diverged_branch"`. Force-pushing local state would clobber the real PR head.
     - `ahead > 0` recovered commits + no drift this tick → synthesize an `AgentResult` and skip the agent spawn; the unified commit/dirty/push branch below pushes the recovered docs commit. When drift fired this tick the recovered branch is **deliberately not taken** — the recovered commit was authored against stale requirements, so the handler falls through to a fresh spawn so the dev re-reads the updated body.
     - `(0, 0)` in sync → fall through to fresh spawn (or awaiting-human resume).
  6. **Awaiting-human resume.** A `fetch_failed` / `agent_timeout` / `agent_silent` resume may be the FIRST time this session sees the docs-stage instructions (the `DOCS: NO_CHANGE` marker, what files to inspect, what to commit), so the resume rebuilds the **full** docs prompt via `_build_documentation_prompt` rather than `_resume_developer_on_human_reply`'s new-comments-only shape. Advance `last_action_comment_id` past every just-read human comment, snapshot `before_sha` from the fetched worktree, persist `docs_checked_sha=before_sha` BEFORE the spawn, then `_resume_dev_with_text`.
  7. **Fresh spawn.** Snapshot `before_sha`, persist `docs_checked_sha=before_sha` and the locked `dev_agent` spec BEFORE invoking the agent, build the docs prompt (issue body + recent comments + `DOCS: NO_CHANGE` marker contract), then `_run_agent_tracked` with `agent_role="developer"` / `stage="documenting"`.
  8. Branch on the post-agent state. Every success exit calls one of two helpers (`_advance_after_docs_push` / `_advance_after_docs_no_change`) that route to **`in_review`** when `docs_final_pending` is set (final-docs handoff: clear the marker AND its companion sentinel `final_docs_approval_seeded`; for the push branches update `agent_approved_sha` to the new head **only when `final_docs_approval_seeded` is set** — when validating's `gh.get_pr()` raised the seeding block never ran, so any non-`None` `agent_approved_sha` left in state is a stale snapshot from a prior round and must NOT be promoted to the new docs head, leaving AUTO_MERGE gated; and ratchet `pr_last_comment_id` via `_ratchet_in_review_watermark_for_final_docs` past any issue-thread reply the awaiting-human resume consumed so the next in_review tick does not bounce the issue to `fixing` over already-addressed feedback) and **`validating`** otherwise (pre-approval pass: legacy behaviour). Branches:
     - `timed_out` → park with `park_reason="agent_timeout"` (transient: dashboards / a later tick may re-spawn).
     - dirty worktree (regardless of whether a commit also landed) → `_on_dirty_worktree`: park; refuse to publish a partial branch or silently drop edits.
     - new commit on a clean tree → `_push_branch` (with the same hardened path the implementing push uses). On success record `docs_checked_sha=after_sha`, `docs_verdict="updated"`, reset `silent_park_count=0`, post `:books: documenting pass: pushed docs commit.` (or the recovered-commit variant) to the PR, then `_advance_after_docs_push(after_sha)`. A push failure parks with `park_reason="push_failed"`.
     - no commit + `DOCS: NO_CHANGE` verdict:
       - if `ahead > 0` (a prior tick committed but never landed the push), push the recovered commit and advance via `_advance_after_docs_push(after_sha)` — the reviewer must see the docs in the diff at validating (pre-approval) or AUTO_MERGE must gate on the new head (final-docs), so the local-only commit can't be left behind. A push failure parks with `push_failed`.
       - otherwise persist `docs_checked_sha=after_sha`, `docs_verdict="no_change"`, reset `silent_park_count=0`, post `:books: documenting pass: no docs changes required.` (with the dev's justification quoted when present), then `_advance_after_docs_no_change()` — no commit landed, so the PR head still matches `agent_approved_sha` and the AUTO_MERGE invariant survives the docs hop untouched.
     - no commit + unknown verdict → `_on_question`: post the agent's last message as a HITL question, park (the helper distinguishes the silent-crash case via stderr diagnostics and tags `silent_park_count` so a poisoned session is dropped on the next resume).
- **User-content drift on the final-docs handoff.** A body edit during the final-docs hop invalidates the prior approval (the reviewer voted on stale requirements). The drift block clears `docs_final_pending`, `agent_approved_sha`, and resets `review_round=0` so the fresh spawn's post-spawn helpers route back to `validating` for re-review — mirrors `_handle_in_review`'s drift handling.
- **Output**: label moved to `validating` (pre-approval pass: pushed docs commit, recovered docs commit pushed under a no-change verdict, or a true no-change verdict against the remote-clean head) OR label moved to `in_review` (final-docs handoff with `docs_final_pending=True`: pushed docs commit updates `agent_approved_sha` and advances; no-change verdict advances with `agent_approved_sha` unchanged) OR terminal `done`/`rejected` (external-merge / closed-issue short-circuit) OR a HITL park (`missing_pr_number`, `fetch_failed`, `diverged_branch`, `agent_timeout`, `push_failed`, `dirty_worktree`, `agent_question`, `agent_silent`).

The docs pass is deliberately a thin dev-session rerun on the existing PR worktree rather than a separate role: there is no `documenting_agent` pin and no separate retry budget. The dev session resumes on its locked `(backend, args)` spec, so `DEV_AGENT` flips made mid-flight do not retarget the docs pass either.

### `_handle_validating` (label `validating`)
- **Trigger**: each tick while label is `validating` (set by `_handle_implementing` after `_on_commits` opens the PR — straight handoff, no pre-review docs hop — and by `_handle_documenting` after its pre-approval docs pass pushes a `docs:` commit or emits `DOCS: NO_CHANGE`).
- **Input**: PR #, branch, `dev_agent`/`dev_session_id` (or legacy `codex_session_id`), pinned state, `review_round`.
- **Internal flow**:
  0. **External-merge short-circuit.** Identical to the implementing entry check: `_finalize_if_pr_merged` flips the label to `done` (and runs cleanup) when the PR was merged externally while the reviewer was queued. The reviewer is not spawned for an already-landed branch. `_handle_documenting` carries the same check at its own entry, so any non-PR-aware handler short-circuits identically. The same handler also chains `_finalize_if_issue_closed` right after, so a closed-`validating` issue (operator rejected mid-review, or the linked PR closed without merge) flips to `rejected` instead of reaching the reviewer and relabeling to `in_review`; the closed-PR variant additionally emits `pr_closed_without_merge` + runs `_cleanup_terminal_branch`, the open-PR variant leaves the branch alone for operator salvage. The same fetch-failure / merged-PR deferral the implementing handler relies on applies here too: when the linked PR's state cannot be confirmed, the closed-issue helper returns True without state changes so the reviewer does not spawn against a closed issue this tick and the next tick re-attempts `_finalize_if_pr_merged`.
  1. Awaiting-human path: same resume mechanic as implementing (resume on the dev's locked spec (backend + args)); on a successful pushed fix, bump `review_round` and clear `agent_approved_sha` (so AUTO_MERGE cannot land the freshly-pushed head against a stale prior approval) and stay on `validating` — no label flip emitted — so the reviewer re-evaluates the new head on the next tick. A no-commit / ACK reply keeps the issue on `validating`.

     Exception: on a `review_cap` park (`park_reason="review_cap"`), the human reply does **not** wake the dev session — resuming would just bump past the cap on the next tick. Instead, the operator must post `/orchestrator add-review-rounds N` on its own line; that resets `review_round` to `MAX_REVIEW_ROUNDS - N`, clears the park flags, and falls through to spawn the reviewer this same tick. A plain reply (or one with an invalid `N`) leaves the issue parked.
  2. If `review_round >= MAX_REVIEW_ROUNDS` (default 3), park awaiting human. The park comment surfaces the `/orchestrator add-review-rounds N` escape hatch so the operator can grant more rounds without losing the PR/worktree.
  3. Otherwise persist `config.REVIEW_AGENT_SPEC` (the raw full spec, e.g. `"codex -m gpt-5.5-codex"`) to `review_agent` for traceability — the reviewer is spawned **fresh each round** with no resume, so always overwriting this field with the current config spec is the right behavior here; a `REVIEW_AGENT` flip mid-flight takes effect on the next round, but the field reflects the reviewer's CLI args and which spec ran each round.

     Then spawn a **fresh reviewer session** via `run_agent(config.REVIEW_AGENT, review_prompt, wt, timeout=config.REVIEW_TIMEOUT, extra_args=config.REVIEW_AGENT_ARGS)` with the **reviewer prompt** (read-only: `git log` / `git diff origin/<spec.base_branch>...HEAD`, must end with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`).
  4. Parse last `VERDICT:` marker (`_parse_review_verdict`):
     - `approved` → in this order:
       1. **Local-verify gate.** Run `_run_verify_commands(wt, config.VERIFY_COMMANDS, config.VERIFY_TIMEOUT)` in the per-issue worktree. A default-empty `VERIFY_COMMANDS` short-circuits to `status="ok"` so the legacy "no verification" behavior is unchanged. Any non-ok result parks the issue on `validating` via `_park_verify_failure` with a typed `park_reason` (`verify_failed`, `verify_timeout`, `verify_dirty`, or `verify_head_changed`) and a park comment that names the failing command, its exit code (or timeout), and a redacted / truncated tail of the captured output; the approval comment, squash, watermark seeding, and `in_review` handoff do **not** fire. GitHub CI remains the later auto-merge gate consulted by `_handle_in_review` — the verify gate is the first gate after the reviewer agent, not the only one. See [`configuration.md#local-verification-gate`](configuration.md#local-verification-gate) for the env-var reference and per-`park_reason` semantics.
       2. Post `:white_check_mark: codex review approved.` on the PR (so the comment exists even when squash later fails).
       3. When `SQUASH_ON_APPROVAL` is on (default), call `_squash_and_force_push` to collapse the dev's commits into one. Subject reuses the first commit when already conventional-commit-shaped, otherwise `feat: <issue title>`; body lists the original subjects; pushed with `--force-with-lease` against the pre-squash SHA.

          On squash or force-push failure, **park awaiting human and stay on `validating`** (no relabel) so the original commits remain on the branch for manual triage — the approval comment has already landed on the PR.
       4. On success, if `squashed_count > 1` post `:package: squashed N commits to 1 after approval` to the PR before seeding the in_review watermarks, so the seed walks past it.
       5. Snapshot `agent_approved_sha` from the **local SHA the reviewer (or the squash) produced** — explicitly *not* the current remote PR head. If the remote moves out from under us, `agent_approved_sha != pr.head.sha` in the auto-merge gate and AUTO_MERGE waits for a fresh review round.

          With `SQUASH_ON_APPROVAL=off`, the snapshot is the pre-review local HEAD (`reviewed_sha` captured before `run_agent`).
       6. Seed the in_review comment watermarks (inside the `else` arm of the `gh.get_pr()` try so a snapshot failure leaves them untouched). When the local SHA being persisted is non-empty (`new_head_sha` truthy — the post-squash local head, or the reviewed head when squash is off), set `agent_approved_sha` to that SHA AND set the `final_docs_approval_seeded=True` sentinel in the same `if` block; when the local SHA is empty (`_head_sha()` returned empty earlier), neither is touched so the sentinel stays False. THEN, outside the try, set `docs_final_pending=True` and label `documenting`. The marker tells the next tick's `_handle_documenting` to advance to `in_review` (not back to `validating`) on its success exits, to update `agent_approved_sha` to the newly-pushed head if a docs commit lands so the AUTO_MERGE `agent_approved_sha == pr.head.sha` invariant survives the final docs commit (gated on `final_docs_approval_seeded` so a stale non-`None` `agent_approved_sha` from a prior round cannot be promoted when this round's snapshot failed or its local SHA was empty), and to ratchet `pr_last_comment_id` past any issue-thread reply consumed by the awaiting-human resume so the in_review tick does not bounce the issue to `fixing` over already-addressed feedback. The approval comment, squash comment, and `agent_approved_sha` snapshot seeded here all ride through the documenting hop untouched.
     - `unknown` (no marker) → park.
     - `changes_requested` → post the feedback to the PR, then **resume the developer's session** on its locked spec (backend + args) with the fix prompt; if it produces a new commit on a clean tree, push, increment `review_round`, and clear `agent_approved_sha` so AUTO_MERGE cannot land the freshly-pushed head against a stale prior approval. The label stays on `validating` (no relabel emitted) so the reviewer re-evaluates the new head on the next tick. A no-commit reply parks via `_on_question` and stays on `validating`.
- **Output**: label moved to `documenting` (approval after verify + squash, with `docs_final_pending=True` so the docs pass hands off to `in_review`) OR no label change with `agent_approved_sha` cleared (a new fix commit landed via CHANGES_REQUESTED, awaiting-human resume, user-content drift, or a transient-park-recovery push that finished a pending push — the issue stays on `validating` and the reviewer re-evaluates on the next tick) OR a HITL park (squash/force-push failure stays on `validating` with the approval comment already on the PR; every other park branch keeps the existing label).

### `_handle_in_review` (label `in_review`)
- **Trigger**: each tick while label is `in_review` (set by `_handle_documenting` on the final-docs hop after `_handle_validating` approves: the docs pass either pushes a docs commit and advances to `in_review` with `agent_approved_sha` updated to the new head, or emits `DOCS: NO_CHANGE` against the remote-clean approved head and advances without pushing). Also runs on closed-`in_review` issues yielded by the closed-issue sweep, so an external manual merge gets finalized to `done` even when `Resolves #N` already closed the issue.
- **Input**: pinned `pr_number`, `branch`, `dev_agent`/`dev_session_id` (or legacy `codex_session_id`), and three watermarks — one per id namespace GitHub uses for PR feedback:
  - `pr_last_comment_id` (issue thread + PR conversation, shared IssueComment id space; falls back to `last_action_comment_id` for back-compat).
  - `pr_last_review_comment_id` (inline review comments, PullRequestComment id space).
  - `pr_last_review_summary_id` (PR review summaries in the PullRequestReview id space).

    Only the *bodies* of non-empty `CHANGES_REQUESTED` or `COMMENTED` reviews are forwarded to the dev, and only those review IDs ever advance this watermark.

    `APPROVED`, `DISMISSED`, `PENDING`, and empty-body reviews are filtered out by `gh.pr_reviews_after` *before* the id watermark is applied, and `_bump_in_review_watermarks` mirrors the same filter, so excluded review IDs never enter the candidate set.

    Re-scanning is harmless: the filter runs each tick, so an `APPROVED` id above the watermark is silently re-skipped rather than re-forwarded.

  Mixing any two namespaces under one watermark would silently drop or replay one side.
- **Internal flow**:
  1. If `pr_number` is missing (manual relabel suspected), park awaiting human and return; subsequent ticks no-op until the human relabels.
  2. Read the PR via `gh.get_pr`. Branch on `gh.pr_state(pr)`:
     - `merged` → set label `done`, stamp `merged_at`, write pinned state, then `issue.edit(state="closed")`. (Pinned-state write before close so PyGithub caching cannot serve a stale issue body to the writer.) Cleanup follows via `_cleanup_terminal_branch`.
     - `closed` (without merge) → set label `rejected`, stamp `closed_without_merge_at`, write state, close, then call `_cleanup_terminal_branch`. The branch name is derived from the issue number (`orchestrator/issue-<n>`) so cleanup cannot touch an arbitrary branch.
     - `open` → fall through.
  3. **Fresh PR feedback (including any human CI-fix request) → route to `fixing`.** A human CI-fix request — a "please fix CI" / "tests are red, fix" comment on any of the four surfaces below — is just one shape of fresh PR feedback as far as this handler is concerned: the route triggers on the *presence* of an unread human comment past the watermark, not on its content. Read four sources independently, one per id namespace:
     - `gh.comments_after(issue, pr_last_comment_id)` (issue thread).
     - `gh.pr_conversation_comments_after(pr, pr_last_comment_id)` (PR conversation; shares id space with the issue thread, so one watermark suffices).
     - `gh.pr_inline_comments_after(pr, pr_last_review_comment_id)` (inline review comments).
     - `gh.pr_reviews_after(pr, pr_last_review_summary_id)` (PR review summary bodies submitted with `CHANGES_REQUESTED` or `COMMENTED` — `APPROVED` bodies are filtered out as informational, dismissed/pending never count, empty bodies are dropped).

     Without the `pr_reviews_after` surface, a "Comment" review with a request in the body would be silently ignored (and may be auto-merged over), and a `CHANGES_REQUESTED` review with body but no inline comments would block merge via `pr_has_changes_requested` without ever reaching the dev agent.

     If any source is newer than its watermark, record pending-fix metadata in pinned state (`pending_fix_at` ISO timestamp plus per-namespace `pending_fix_issue_max_id` / `pending_fix_review_max_id` / `pending_fix_review_summary_max_id` bookmarks) and flip the label to `fixing` immediately. The `_handle_in_review` handler deliberately does NOT honor `IN_REVIEW_DEBOUNCE_SECONDS` here or spawn the dev itself — the `fixing` stage owns debouncing, the dev resume, the push, and the DIRECT bounce back to `validating` (the pre-approval pushed-fix exit deliberately skips the `documenting` hop -- docs land in the final-docs pass after reviewer approval) so the in_review handler stays focused on PR-state terminals and the auto-merge gate. Watermarks are deliberately NOT advanced on this route so the `fixing` handler can read the triggering comments to build its dev-resume prompt; the `pending_fix_*_max_id` keys are bookmarks (a hint for the `fixing` handler / for observability), not watermarks. If `awaiting_human` / `park_reason` were carried over from a prior transient park, they are cleared as part of the route (the human comment that triggered the route is the resume signal).
  4. **Auto-merge gate** (only reached when there are no new comments to act on). Off unless `AUTO_MERGE=on`. Sequence:
     - **Standing CHANGES_REQUESTED veto** — `gh.pr_has_changes_requested(pr, head_sha=head_sha)` runs *before* the approval check and silently returns on True.

       A human `CHANGES_REQUESTED` review on the current head SHA blocks merge even when `agent_approved_sha == head_sha`, the PR is mergeable, and checks are green (the agent's APPROVED would otherwise short-circuit `pr_is_approved`).
     - **Approval check** — either `agent_approved_sha == pr.head.sha` (snapshotted by validating when the reviewer agent emitted `VERDICT: APPROVED`), OR `gh.pr_is_approved(pr, head_sha=pr.head.sha)` — only counts human/bot reviews submitted on the *current* head SHA, so a stale APPROVED from before a later push does not unlock auto-merge.
     - **`pr_is_mergeable`** — `None` means GitHub still computing, try next tick.

       `False` with `AUTO_MERGE=on` does NOT park anymore — it routes the issue to the new `resolving_conflict` stage (post a notice on the PR, seed `conflict_round=0` only when absent so a re-entry preserves the cap counter, flip the label, return), where `_handle_resolving_conflict` attempts the base rebase on the next tick. Under `AUTO_MERGE=off` the legacy unmergeable park still fires here.
     - **`pr_combined_check_state`** — `success` proceeds; `pending` waits; `failure`/`none` parks awaiting human — `none` means no checks at all, ambiguous.

     Under `AUTO_MERGE=off` the gate sequence above is skipped entirely. Instead, once the PR is mergeable the handler posts a one-shot `:bell:` ping mentioning every `HITL_HANDLE` so the human knows the PR is ready for review/merge. The ping is de-duplicated by `ready_ping_sha` (the head SHA we pinged for) — a long-lived ready PR doesn't spam handles on every poll, but a new commit shifts `pr.head.sha` and triggers a fresh ping. The ping is NOT a park: `awaiting_human` stays false so subsequent ticks still react to new PR comments, an external merge, or a later unmergeable transition.

     Unlike the park branches, the ready ping deliberately does NOT call `_bump_in_review_watermarks`. The watermark bump reads `gh.latest_comment_id(issue)`, which could include a human issue/PR-conversation comment that landed between the handler's earlier comment scan and the ping; bumping past it would silently swallow the feedback. The ping is recorded in `orchestrator_comment_ids` by `_post_issue_comment`, so the next tick's id-set filter already excludes it from `new_issue_side` without needing the watermark to move — and any concurrent human comment naturally surfaces below the unchanged watermark.
     - **`gh.merge_pr(pr, sha=head_sha)`** — pinned to the *captured* `head_sha` from the start of the gate sequence, **not** `pr.head.sha`.

       `pr_is_mergeable` calls `pr.update()` to resolve a `None` mergeable, which can refresh `pr.head.sha`; the explicit `head_sha` pin (combined with the earlier `pr.head.sha != head_sha` bail) ensures a commit landing during the refresh either bails the tick or causes GitHub to return 409/422 rather than merge an unreviewed head. PyGithub's 405/409/422 are returned as `False` and the next tick retries.
  5. On a successful merge, set label `done`, stamp `merged_at`, write pinned state, close the issue, then call `_cleanup_terminal_branch` (best-effort: remove the per-issue worktree, delete the local branch, and call `gh.delete_remote_branch`). Cleanup runs on every PR-state terminal where the PR itself is gone (external merge, AUTO_MERGE, and closed-without-merge) so neither merged nor declined PRs leave stale `orchestrator/issue-<n>` branches on the remote.

     A manually closed issue with an *open* PR is conservative on purpose: the label flips to `rejected` but the branch is left alone, since the operator may still want to inspect or salvage the PR.

     **Caveat:** once that flip lands, the issue is closed AND labeled `rejected`, so it falls outside `list_pollable_issues` (which only sweeps closed issues still labeled `in_review`, `fixing`, or `resolving_conflict`) and the terminal-label dispatcher is a no-op. If the operator subsequently closes the PR, the orchestrator will never observe it and `_cleanup_terminal_branch` will not run — the worktree, local branch, and remote branch must be removed by hand for that ordering. The reverse ordering (close the PR first, then the issue) is fully automated by the `pr_status == "closed"` arc above.
  6. Every park inside this handler bumps the in_review watermarks past the orchestrator's own park comment via `_bump_in_review_watermarks`, so the next tick does not see the HITL ping as fresh PR feedback and resume the dev agent against it.
- **Output**: label moved to `done` / `rejected` (terminal) OR a relabel to `fixing` (fresh PR feedback) OR a relabel to `resolving_conflict` (under `AUTO_MERGE=on` when the PR is unmergeable past the approval gates) OR a HITL park OR a no-op tick.

The "route to `fixing` on a new PR comment" arc is intentional: the fixing stage owns the dev-resume + push + hand-back-to-`validating` cycle so the in_review handler stays focused on PR-state terminals and the auto-merge gate. The dev resume and reviewer re-run still happen — they just live in different stages — so the "validating re-runs after a fix" guarantee holds. The pre-approval pushed-fix exit deliberately skips the `documenting` hop: docs land in the final-docs pass after reviewer approval, so running the docs stage against an unapproved diff would just push a no-op and waste a tick.

`_park_awaiting_human` posts on the issue (not the PR) so the HITL ping appears alongside the rest of orchestrator state. The PR comment that triggers a route to `fixing` is the human signal; awaiting-human is reserved for *unrecoverable* states (failed checks / push fail / missing pr_number — note: under `AUTO_MERGE=on` "not mergeable" detours to `resolving_conflict` instead of parking).

### `_handle_fixing` (label `fixing`)
- **Trigger**: each tick while label is `fixing` (set by `_handle_in_review` when fresh PR feedback arrives on any of the four comment surfaces — including a human CI-fix request, i.e. a "please fix CI" / "tests are red, fix" comment, which is handled identically to any other unread human comment). The label therefore means an unread human comment OR a human CI-fix request is queued during the quiet window or actively being addressed by the dev fix-loop. Also runs on closed-`fixing` issues yielded by the closed-issue sweep so an externally-merged PR can be finalized to `done`.
- **Input**: pinned `pr_number`, `branch`, `dev_agent`/`dev_session_id`, plus the `pending_fix_at` ISO timestamp and per-namespace `pending_fix_*_max_id` bookmarks recorded by the in_review route. Reads the three in_review watermarks (`pr_last_comment_id`, `pr_last_review_comment_id`, `pr_last_review_summary_id`) which the route deliberately left behind so the rescan can re-discover the triggering feedback. `IN_REVIEW_DEBOUNCE_SECONDS` controls the quiet window.
- **Internal flow**:
  1. PR-state terminals mirror `_handle_in_review` so the handler does not strand closed-`fixing` issues:
     - `pr_state == "merged"` → label `done`, stamp `merged_at`, write pinned state, close the issue, emit `pr_merged` (`stage="fixing"`, `merge_method="external"`), and call `_cleanup_terminal_branch`.
     - `pr_state == "closed"` (without merge) → label `rejected`, stamp `closed_without_merge_at`, write pinned state, close the issue, emit `pr_closed_without_merge`, and call `_cleanup_terminal_branch`.
     - PR is open BUT the issue was closed manually (sweep yielded it) → flip to `rejected` without branch cleanup so the operator can salvage the still-open PR.
  2. Closed issue with no resolvable PR (manual relabel, no `pr_number`) → no-op; the operator must relabel manually to finalize.
  3. Open issue with no `pr_number` (manual relabel from outside the in_review route) → park awaiting human with `park_reason="missing_pr_number"`. The dev-resume path needs the PR to push a fix, so we cannot proceed without it.
  4. Rescan unread feedback from the three watermarks across all four surfaces (issue thread + PR conversation share the IssueComment id space; inline-review and review-summary live in their own id spaces). Orchestrator-authored comments are filtered by recorded id AND by the hidden `<!--orchestrator-comment-->` body marker. The route from `_handle_in_review` deliberately leaves the watermarks behind, so the initial fixing tick re-discovers the triggering comments; later ticks pick up additional comments that landed while the label was already `fixing` (which is what naturally extends the debounce window).
  5. If awaiting-human is set (a prior failed resume parked the issue) and the rescan finds no new feedback past the watermarks, return: the gate stays held until a fresh human reply or new PR-side feedback. With new feedback in hand, clear the park flags and fall through.
  6. If no unread feedback at all (watermarks already cover the bookmarks — a prior tick consumed them or an operator advanced them manually), clear the `pending_fix_*` bookmarks and bounce the label back to `validating` so the reviewer re-evaluates against the current head.
  7. **Quiet window**: compute the newest `created_at` (or `submitted_at` for review summaries) across the unread feedback; if that timestamp is younger than `IN_REVIEW_DEBOUNCE_SECONDS`, return and wait. A comment arriving on the next tick is naturally picked up by the rescan and resets the wait because the freshest timestamp controls the gate.
  8. **Resume**: build a `_build_pr_comment_followup` prompt over ALL unread surfaces (issue thread + PR conversation + inline + summaries), resume the locked dev session via `_resume_dev_with_text`, then refresh `user_content_hash` (the hash covers title + body + human issue-thread comments, so any issue-thread comment we just fed to the dev would otherwise re-fire `_handle_validating`'s drift check next tick and resume the dev a second time on input it already handled). Apply the validating-side `_handle_dev_fix_result` disposition (timeout / no-commit / dirty / push fail park flows are identical to the validating fix-loop).
  9. **Watermark advance**: regardless of dev outcome, the handler calls `_advance_consumed_watermarks`, which advances each of the three in_review watermarks ONLY to the max id consumed on that surface (ratcheted against the existing watermark). Deliberately does NOT include `gh.latest_comment_id(issue)` or `last_action_comment_id` -- a human comment that landed AFTER the rescan but BEFORE this write was never quoted in `_build_pr_comment_followup`, so silently moving the watermark past it would swallow real feedback (breaks the "comments arriving while already labeled `fixing`" contract on the failure paths AND lets AUTO_MERGE land the PR over unread feedback on the success path). The orchestrator's own park comment posted by `_park_awaiting_human` does NOT need a watermark bump to avoid replay: the next tick's rescan filters orchestrator-authored comments by recorded id AND by the hidden `<!--orchestrator-comment-->` body marker, so the park comment is dropped even when the watermark sits below it. The legacy in_review pushed-fix path had the same constraint.
  10. **On a pushed fix**: clear the `pending_fix_*` bookmarks (they served their purpose), reset `review_round` to 0 so the reviewer starts fresh on the new diff, drop the now-stale `agent_approved_sha` (the head just moved — the reviewer must re-approve), write pinned state, and flip the label DIRECTLY back to `validating` so the reviewer re-evaluates the new head next tick. The pre-approval pushed-fix exit deliberately skips the `documenting` hop — docs land in the final-docs pass after reviewer approval, so running the docs stage against an unapproved diff here would just push a no-op and waste a tick.
- **Output**: terminal `done` / `rejected` (PR-state arcs) OR label flipped to `validating` (pushed fix OR no-new-feedback bounce) OR a HITL park (timeout / dirty / push fail / no-commit) OR a no-op tick (quiet-window wait, missing-PR park already set).

### `_handle_resolving_conflict` (label `resolving_conflict`)
- **Trigger**: each tick while label is `resolving_conflict` (set by `_handle_in_review`'s auto-merge gate when an approved PR is unmergeable under `AUTO_MERGE=on`). Also runs on closed-`resolving_conflict` issues yielded by the closed-issue sweep, mirroring the in_review terminal handling so a manually-merged PR finalizes to `done` even when `Resolves #N` already closed the issue.
- **Input**: pinned `pr_number`, `branch`, `dev_agent`/`dev_session_id` (or legacy `codex_session_id`), `conflict_round`. `MAX_CONFLICT_ROUNDS` from config.
- **Internal flow**:
  1. If `pr_number` is missing (manual relabel suspected), park awaiting human and return.
  2. Read the PR via `gh.get_pr`. Branch on `gh.pr_state(pr)`:
     - `merged` → `done` (close issue, stamp `merged_at`, call `_cleanup_terminal_branch`).
     - `closed` (without merge) → `rejected` (close issue, stamp `closed_without_merge_at`, call `_cleanup_terminal_branch`).
     - `open` → fall through.

     Mirrors the in_review terminal arcs for the case where a human resolves manually mid-stage. Cleanup runs whenever the PR itself is gone so a declined PR doesn't leave its `orchestrator/issue-<n>` branch behind either.
  3. If the issue itself was closed manually while the PR is still open, treat as a hard human stop: flip to `rejected` rather than continuing to spawn the dev agent. Deliberately do NOT clean up the branch here — the PR is still open and may be useful for inspection or salvage.

     Same caveat as the in_review counterpart: once the label flips to `rejected` the closed-issue sweep no longer surfaces this issue, so a subsequent PR close is not observed and the operator must clean up the worktree, local branch, and remote branch by hand. Cleanup fires automatically only when the PR is closed *before* the orchestrator flips the label to `rejected`.
  4. **Awaiting-human resume path**: when parked from a previous round and a new human comment has arrived since `last_action_comment_id`, resume the dev session on the in-progress rebase worktree with the human's text (mirrors `_handle_implementing`'s awaiting-human branch — the park messages explicitly invite that flow). The post-agent step uses the same `_post_conflict_resolution_result` helper as the fresh-rebase path.
  5. **Cap check**: if `conflict_round >= MAX_CONFLICT_ROUNDS`, park awaiting human with the round count and the cap quoted. To escape the park the human must either:
     - (a) relabel the issue back to `validating` (or any other workflow label) so the dispatcher leaves `_handle_resolving_conflict` entirely; or
     - (b) post a new issue comment, which the awaiting-human resume branch (item 4) picks up to drive another dev-agent round.

     A bare branch push or manual rebase alone does NOT unpark — `awaiting_human` stays set and step 4 returns until a comment lands or the label changes.
  6. Ensure the per-issue worktree. `_ensure_pr_worktree` (PR-aware, restores from `origin/<branch>`) is used in place of `_ensure_worktree`, which would rebuild from `origin/<base>` and silently discard the PR's commits.
  7. Refresh `origin/<branch>` over `_authed_fetch` (the same hardened authenticated channel `_push_branch` uses); a stale local `origin/<branch>` would mis-classify a real "remote moved out from under us" situation as in-sync.
  8. Compare HEAD to the freshly-fetched `origin/<branch>`:
     - `behind > 0` (worktree diverged) → park: force-pushing local state would clobber the real PR head.
     - `ahead > 0` (recovered unpushed commits from a previous tick that crashed before `_push_branch` returned) → run the same dirty-tree check `_on_dirty_worktree` uses, then push the recovered work and flip to `validating` (the single docs pass is deferred to the post-approval hop, so the reviewer re-runs against the recovered commit directly) with `review_round=0`, `conflict_round += 1`.
     - `(0, 0)` (in sync) → fall through.
  9. Refresh `origin/<base>` over the same hardened path, then run `git rebase origin/<base>` in the worktree under `_git_hardened` (drops global/system git config, disables hooks/fsmonitor/credential helpers/commit signing, and disables rebase autostash — the agent owns the worktree and could otherwise plant a hook to execute attacker code mid-rebase).
  10. **Clean rebase succeeded**: dirty-tree check first (a leftover edit from a crashed prior tick must not silently survive into validating).

      If the HEAD SHA did not move (already up-to-date — `git rebase` returned success without applying anything), skip the push and flip back to `validating` with `review_round=0`, `conflict_round += 1`. Counting the no-op against the cap surfaces a perpetually-unmergeable-due-to-branch-protection PR within `MAX_CONFLICT_ROUNDS` ticks instead of letting it ping-pong between handlers forever.

      If HEAD moved, force-with-lease push the rebased branch, clear any stale `agent_approved_sha`, and flip to `validating` (same target as the no-op path; the single docs pass is deferred to the post-approval hop, so the reviewer re-runs against the rebased branch directly).
  11. **Conflicted rebase**: build a conflict-resolution prompt via `_build_conflict_resolution_prompt` (lists up to 20 conflicted paths, instructs the agent to resolve and continue the rebase, and not push), resume the dev session on the locked spec (backend + args) with that prompt, then run `_post_conflict_resolution_result`.
  12. `_post_conflict_resolution_result` is the shared post-agent funnel:
      - timeout → park (HITL);
      - unfinished rebase → park;
      - no new commit → `_on_question` park;
      - dirty tree → `_on_dirty_worktree` park;
      - push fail → park;
      - success → force-with-lease push, clear any stale `agent_approved_sha`, set `last_conflict_resolved_at`, increment `conflict_round`, reset `review_round=0`, flip to `validating` (the single docs pass is deferred to the post-approval hop, so the reviewer re-runs against the resolved branch directly).

      Fresh conflicted-rebase pushes pin the lease to the pre-rebase PR head captured after the branch/head equality gate. Awaiting-human resume pushes deliberately use `_push_branch`'s live `ls-remote` lease fallback, because the local `before_sha` may be an intermediate rebase or recovered commit SHA rather than the remote PR head.

      The counter increments only on the success path so a timeout/dirty/push-fail does not eat a slot from the cap.
- **Output**: label moved to `validating` (every exit path — base-up-to-date no-op, clean rebase pushed, recovered push, agent-resolved conflicts, awaiting-human resume push, drift-pushed fix) OR `done`/`rejected` (terminal arcs) OR a HITL park (cap exhausted, dirty worktree, push fail, agent timeout, agent silence, fetch fail, diverged worktree, missing pr_number).

The rebase path deliberately rewrites the PR branch to keep history linear after other issue PRs land. Every pushed rebase resets `review_round`, so the reviewer agent must re-approve the rewritten head before AUTO_MERGE can pass.

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
- **Cross-stage interaction (relabel to `implementing`).** `_handle_implementing` carries an explicit guard: when it inherits an `awaiting_human=True` + `park_reason` starting with `question_` from this stage, it inspects the worktree AND the local `orchestrator/issue-<n>` branch via `_branch_has_unpushed_commits`. A clean worktree + clean branch drops the question-stage park flags, ratchets `last_action_comment_id` past the question agent's answer comment, and falls through to the fresh dev-spawn path; a dirty worktree OR a branch with commits beyond `origin/<base>` re-parks with `question_unsafe_relabel` and tells the operator to reset before the dev agent can start from a clean base.
- **Output**: an issue comment with the agent's answer or follow-up question (always pinging `HITL_MENTIONS`) + a HITL park, OR a terminal flip to `done` on a manual close, OR a no-op tick when awaiting a human reply that has not arrived.

The Q&A flow deliberately keeps state minimal: no PR is ever opened, no branch is ever pushed, and the per-issue worktree only survives across ticks when an unsafe park requires operator inspection. Multi-turn conversations rebuild the worktree on each spawn from a fresh `origin/<base>` — the agent session state lives in pinned state, not in the worktree, so the locked session resumes correctly across the cleanup.

## State transition (label lifecycle)

```
   Forward (single-task happy path):
     (none) ──► decomposing ──► ready ──► implementing ──► validating
                ──► documenting (final-docs, docs_final_pending=True)
                ──► in_review ──► done | rejected

   Decompose detours:
     decomposing --(split)──► blocked ──(children created)──► ready
                                  ▲
                                  └ child rejected ─► park HITL

   Validating fix loop (any pushed dev fix):
     validating --(CHANGES_REQUESTED / awaiting-human resume / user-content drift / transient-park push)──►
       ++review_round, clear agent_approved_sha, label stays =validating
         ──► reviewer re-evaluates on the next tick (no docs hop)
     validating --(APPROVED, verify ok, squash ok)──►
       set docs_final_pending=True, label=documenting (final-docs)
         ──► docs pass (push updates agent_approved_sha when validating
              seeded it, else leaves None so AUTO_MERGE stays gated;
              no_change keeps the existing value; the exit also
              ratchets pr_last_comment_id past any issue-thread reply
              the awaiting-human resume consumed so in_review does not
              re-feed it as fresh PR feedback)
         ──► in_review
     (MAX_REVIEW_ROUNDS exhausted ─► park HITL;
      squash failure ─► park HITL on validating, no marker set)

   In_review terminals and fix bounce:
     in_review --(PR merged)─────────────► done
     in_review --(PR closed unmerged)────► rejected
     in_review --(fresh PR feedback)─────► fixing
       fixing --(quiet window expires, dev fix pushed)──► validating
       fixing --(rescan finds no unread feedback)──────► validating
     in_review --(user-content drift, pushed)──► validating
       (review_round=0, agent_approved_sha cleared; pre-approval
        drift exit skips the documenting hop -- docs land in the
        final-docs pass after reviewer approval)
     in_review --(user-content drift, ACK no-commit)──► validating
       (review_round=0, agent_approved_sha cleared; same destination
        as the pushed exit)
     in_review --(AUTO_MERGE on, approved, gates pass)──► merge ─► done
     in_review --(AUTO_MERGE on, approved, unmergeable past gates)──►
       resolving_conflict --(any pushed resolution: clean rebase,
         recovered push, agent/human-resume push, drift push)──►
         validating  (no pre-approval docs hop; the single docs pass
                      runs after the reviewer's final approval)
       resolving_conflict --(base up-to-date no-op, no diff)──► validating
       resolving_conflict --(round ≥ MAX_CONFLICT_ROUNDS)──► park HITL

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
     pr merged (externally or by AUTO_MERGE) ─► done (issue closed,
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
     `fixing` forever):
       pr merged    ─► done + merged_at + close + cleanup
       pr closed    ─► rejected + closed_without_merge_at + cleanup
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
     bookmarks, reset `review_round`, drop `agent_approved_sha`, and
     flip DIRECTLY to `validating` so the reviewer re-evaluates the
     new head next tick (the pre-approval pushed-fix exit deliberately
     skips the `documenting` hop -- docs land in the final-docs pass
     after reviewer approval). On failure (timeout / dirty / push fail
     / no-commit) park awaiting human; the next tick's
     `awaiting_human and not new_feedback` gate becomes true once the
     park comment is the only unread item (everything else has been
     consumed past the watermark or filtered as orchestrator-authored).

   resolving_conflict (AUTO_MERGE only, capped by MAX_CONFLICT_ROUNDS):
     git rebase origin/<base> clean (HEAD moved) ─► push,
       label=validating (++conflict_round)
     git rebase origin/<base> no-op (HEAD unchanged, no diff) ─►
       label=validating (++conflict_round, no push)
     conflicts ─► dev resumes, continues rebase, push ─►
       label=validating (++conflict_round)
     ahead-of-remote recovered commits ─► push ─►
       label=validating (++conflict_round)
     awaiting-human resume push / drift push ─►
       label=validating (++conflict_round)
     conflict_round >= MAX_CONFLICT_ROUNDS ─► park awaiting human
     pr merged/closed mid-stage ─► done / rejected (terminal)
     (no pre-approval documenting hop -- the single docs pass runs
      after the reviewer's final approval via docs_final_pending)

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
