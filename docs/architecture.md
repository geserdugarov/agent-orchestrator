# Architecture of the Current Implementation

Single-process **polling orchestrator** that drives GitHub issues through a label-based state machine, delegating the actual coding work to a configurable coding-agent CLI (`codex` or `claude`) running as a subprocess in isolated git worktrees.

The dev/review/decompose roles are picked independently via `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` (default: claude decomposes, claude implements, codex reviews). Each value is a shell-like command spec: the first token must be `codex` or `claude` and selects the runner (which then launches `CODEX_BIN` or `CLAUDE_BIN`); any remaining tokens are forwarded verbatim as backend-CLI args (model, reasoning effort, etc.) on every spawn for that role. All three are parsed and validated at config load — see [Agent command specs](#agent-command-specs) below.

New unlabeled issues route through a `decomposing` stage that asks the decomposer agent for a structured manifest: `decision=single` flips the issue to `ready` and the implementer takes over; `decision=split` creates child issues, persists the dep graph, and parks the parent on `blocked` (or `umbrella` when the manifest's `umbrella` flag is true — a parent with no implementation of its own that `_handle_umbrella` closes to `done` once every child resolves) until the matching handler walks the children. Decomposition can be disabled with `DECOMPOSE=off`, which reverts to the legacy direct-to-`implementing` pickup.

Once the reviewer approves and the PR is mergeable with green CI, the orchestrator can merge it itself (gated by `AUTO_MERGE`, default off) and close the issue with `done`; an approved-but-unmergeable PR detours through a `resolving_conflict` stage that rebases onto `origin/<base>` (capped by `MAX_CONFLICT_ROUNDS`) before bouncing back to `validating`; PRs closed without merge land on `rejected`.

## Design constraints

GitHub Issues are the orchestrator's task tracker and durable state surface. The process intentionally avoids an internal database: workflow labels expose the current stage, and the pinned JSON comment holds the per-issue state that the next tick needs. This keeps progress visible to humans on github.com and lets the process restart without reconstructing hidden local state.

The orchestrator is not meant to be fully autonomous. When a stage hits uncertainty, an unsafe repository state, a malformed agent response, or an exhausted retry cap, it parks with `awaiting_human` and mentions `HITL_HANDLE`; a later human issue comment is the resume signal for the parked agent session.

The workflow is deliberately fixed instead of planner-selected: decomposition, implementation, validation, and acceptance are mandatory phases. A future dynamic planner could select extra stages or skip some phases for trivial tasks, but the current implementation keeps routing explicit and label-driven.

Agents run on the host as CLI subprocesses with broad local permissions (`codex --dangerously-bypass-approvals-and-sandbox`, `claude --dangerously-skip-permissions`). The host, container, or VM around the orchestrator is therefore the real sandbox boundary; token handling and hardened git operations are designed around that assumption.

## Top-level layout

```
orchestrator/
  main.py               — entry point, polling loop, self-restart guard
  config.py             — env loading, secrets handling, backend validation
  github.py             — PyGithub wrapper, label bootstrap, pinned-state comment
  agents.py             — coding-agent subprocess runner (codex/claude dispatch)
  workflow.py           — slim facade: per-repo tick loop, `_FAMILY_AWARE_LABELS`
                           partitioning, `_process_issue` label dispatcher,
                           `_handle_pickup`, `_park_awaiting_human`,
                           `_run_agent_tracked`. Re-exports the cross-module
                           helpers and the stage entry handlers from the
                           modules below under their original names so existing
                           test patches (`patch.object(workflow, "_foo", ...)`)
                           keep working. Stage-private helpers that no other
                           module needs (e.g. `_bump_in_review_watermarks`,
                           `_auto_merge_gates_pass`,
                           `_seed_legacy_in_review_watermarks`,
                           `_emit_conflict_round_incremented`) stay private to
                           their stage module and are NOT re-exported.
  workflow_drift.py     — user-content drift helpers:
                           `_compute_user_content_hash`,
                           `_detect_user_content_change`,
                           `_build_user_content_change_prompt` (the drift /
                           user-content-change dev-resume prompt builder),
                           `_mark_drift_comments_consumed`,
                           `_route_drift_to_decomposing`.
  workflow_messages.py  — shared text/parsing/comment helpers: orchestrator
                           comment markers and post helpers, stderr redaction
                           and diagnostics, the implementer / reviewer /
                           decomposer / conflict-resolution / PR-comment
                           followup prompt builders, and the manifest /
                           review-verdict / drift-ACK parsers. The drift /
                           user-content-change prompt builder lives in
                           `workflow_drift.py`, not here.
  worktrees.py          — git, branch, and worktree plumbing: `_branch_name`,
                           slug-safe per-repo worktree paths,
                           `_ensure_worktree` / `_ensure_pr_worktree` /
                           `_ensure_decompose_worktree`, hardened git
                           invocations (`_git`, `_git_hardened`), authenticated
                           fetch/push (`_authed_fetch`, `_authed_target_fetch`,
                           `_push_branch`), `_squash_and_force_push`,
                           `_refresh_base_and_worktrees`,
                           `_cleanup_terminal_branch`, and the local-verify
                           runner (`_run_verify_commands` + `VerifyResult`)
                           used by `_handle_validating`'s pre-`in_review`
                           gate.
  stages/
    __init__.py         — package marker; the dispatcher in `workflow.py`
                           still owns label→handler routing.
    decomposition.py    — `_handle_decomposing`, `_handle_ready`,
                           `_handle_blocked`, `_handle_umbrella`, and the
                           decomposer-session lookup / resume helpers.
    implementing.py     — `_handle_implementing` plus the developer-session
                           lifecycle: `_read_dev_session`,
                           `_resume_developer_on_human_reply`,
                           `_resume_dev_with_text` (with poisoned-session
                           recovery), the 24h retry budget, and the post-agent
                           disposition helpers (`_on_commits`, `_on_question`,
                           `_on_dirty_worktree`).
    validating.py       — `_handle_validating` plus reviewer-session
                           lifecycle: `_handle_dev_fix_result`,
                           `_post_user_content_change_result`, validating-side
                           transient-park recovery, the local-verify gate
                           park helper (`_park_verify_failure`), and the
                           watermark seeding for the validating→in_review
                           handoff.
    in_review.py        — `_handle_in_review` plus PR-side primitives:
                           transient park-reason set, the quiet auto-merge
                           gate re-check, legacy watermark migration, and the
                           cross-namespace watermark ratchet
                           (`_bump_in_review_watermarks`).
    conflicts.py        — `_handle_resolving_conflict` plus
                           `_post_conflict_resolution_result` and the
                           `conflict_round` audit-event emitter.
```

Stage modules reach back into the facade via `from .. import workflow as _wf`
at call time so test patches against `workflow.<helper>` still intercept calls
made from inside a stage handler. Direct imports from `workflow_drift` /
`workflow_messages` / `worktrees` would bind stable references that the
`patch.object(workflow, ...)` pattern cannot override, so stage handlers
deliberately avoid them.

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
| `validating` | The reviewer agent is checking the diff and may bounce fixes back to the dev agent. |
| `in_review` | A PR is open and ready for human review or auto-merge gates. |
| `resolving_conflict` | The orchestrator is trying to rebase an approved but unmergeable PR branch onto `origin/<base>`. |
| `question` | Operator-applied read-only Q&A label: the orchestrator runs the decomposer agent in the per-issue worktree, posts the answer to the issue thread, and waits on a human reply or a manual close. No PR is opened on this label. |
| `done` | Terminal success; the PR merged, an umbrella parent resolved after all children reached `done`, or a `question` issue was closed by the operator. |
| `rejected` | Terminal rejection; the PR or issue was closed without merge. |

## Process model

There is **only one long-lived process**: `python -m orchestrator.main`. It is wrapped by `run.sh` so the loop can self-exit and be restarted with new code.

- **Trigger**: started manually (or by a wrapper). Optional `--once` for a single tick.
- **Tick cadence**: every `POLL_INTERVAL` seconds (default 60).
- **Self-restart guard** (`main._self_modifying_merge_happened`): each tick fetches `origin/<ORCHESTRATOR_BASE_BRANCH>` (default `main`); if it advanced past the process's startup SHA *and* the new commits touch `orchestrator/`, the loop exits 0 so the wrapper can re-exec the new code. The branch is decoupled from `BASE_BRANCH` so a target repo with a different default branch does not interfere with self-update detection.
- **Signals**: SIGINT/SIGTERM set a flag; the current tick finishes, then the loop exits.

The coding agent runs as a **transient child subprocess**, not a daemon — spawned per tick when work is needed.

## Per-tick flow (`workflow.tick`)

Each tick the polling loop fans out across **every configured repo**. `config.default_repo_specs()` returns a list of `RepoSpec(slug, target_root, base_branch, remote_name, parallel_limit)` — one entry per `REPOS` line, or a single entry derived from the legacy `REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH` / `REMOTE_NAME` quartet when `REPOS` is unset (with `parallel_limit` taken from `MAX_PARALLEL_ISSUES_PER_REPO`, default 1). Each `REPOS` entry may override `remote_name` via its optional fourth pipe-separated field (default `origin`) and `parallel_limit` via its optional fifth (default `MAX_PARALLEL_ISSUES_PER_REPO`).

`main._run_tick` fans the per-repo `workflow.tick(gh, spec)` calls out across a `ThreadPoolExecutor` (one worker thread per configured repo) so a slow repo does not delay the others; a per-repo exception is logged and swallowed so one wedged repo cannot stop the others from advancing this tick. The single-repo legacy path stays in-thread (no executor) to keep deployments without `REPOS` unchanged. Each `GitHubClient` is constructed once at startup with `repo_spec=spec` and `ensure_workflow_labels` runs per repo so a fresh target repo bootstraps its labels on first connect.

A single `threading.BoundedSemaphore(MAX_PARALLEL_ISSUES_GLOBAL)` is built once at startup and threaded through every `workflow.tick(gh, spec, global_semaphore=...)` call. Each tick acquires it around every `_process_issue` invocation so workers from different repos contend on the same semaphore — total in-flight per-issue handlers across all repos never exceeds `MAX_PARALLEL_ISSUES_GLOBAL` (default 3) regardless of how many `parallel_limit` slots each repo declares.

Within one repo, `spec.parallel_limit` caps how many issues `workflow.tick` may advance concurrently on a single tick. Default is 1 (legacy one-at-a-time behavior); each `REPOS` entry can override it via its optional fifth pipe-separated field, and the global `MAX_PARALLEL_ISSUES_PER_REPO` (default 1) supplies the default for entries that omit the field. `parallel_limit == 1` keeps the legacy sequential, streaming loop directly over `gh.list_pollable_issues()` so a partial enumeration failure still processes everything yielded before the failure.

`parallel_limit > 1` materializes the eligible-issue set up front (to bound `max_workers` correctly) and partitions it by workflow label:

- **Family-aware labels** — `decomposing`, `blocked`, `umbrella`, and unlabeled (pickup) issues — read and write cross-issue state (parent ↔ child). Two of these running at once could race a parent's child-state write against the child's own handler on a sibling thread. They are folded into a SINGLE drain task that processes them sequentially on one worker thread; this caps the family bucket's executor footprint at exactly one slot regardless of how many family-aware issues are pending, so the other `limit - 1` slots stay free for fan-out work.
- **Fan-out labels** — `ready`, `implementing`, `validating`, `in_review`, `resolving_conflict` — only touch their own per-issue pinned state and worktree. Each is submitted as its own future and runs concurrently up to `parallel_limit`. The two buckets share one executor capped at `min(parallel_limit, total_tasks)` and the family drain can overlap with non-family workers, so a slow decomposer no longer blocks unrelated implementing / validating issues on the same tick.

Only issue numbers cross the thread boundary — each worker calls `gh._for_worker_thread()` to mint a fresh `GitHubClient` (and through it a fresh `Github` / `Requester` / `Repository`) and refetches its Issue against that client, so every in-flight HTTP call is the sole consumer of its requester's state (PyGithub's per-request state is not documented as thread-safe across a shared `Requester`). The label used for partitioning is read on the caller thread; a lazy-load failure on one issue's labels is logged and that issue is conservatively routed into the family bucket where the per-issue try/except picks up any sustained failure.

Inside `workflow.tick(gh, spec)`, before any issue is dispatched the tick runs `_refresh_base_and_worktrees(gh, spec)`: a single `git fetch <spec.remote_name> <spec.base_branch>` in `spec.target_root` (the remote name defaults to `origin` but is overridable per `REPOS` entry via the fourth pipe-separated field, so a `REPOS=...|private|2` row fetches from `private/<base>`), then per-issue dispatch on each existing worktree under `<WORKTREES_DIR>/<owner>__<name>/issue-*`. The per-stage `_ensure_*_worktree` helpers only fetch base on (re)creation, so a worktree that survives across ticks would otherwise stay anchored at whatever `<remote>/<base>` looked like when it was first added.

Two paths depending on whether a PR already exists for the issue:

- **Pre-PR worktrees** (no `pr_number` in pinned state) get a clean-tree `git rebase origin/<base>` directly — there is no remote to push to, so the local branch can be kept linear without publishing a rewrite.
- **PR-having worktrees** in `validating` / `in_review` are detoured to `resolving_conflict` instead (via `_route_pr_worktree_to_resolving_conflict`: post a PR notice, seed `conflict_round` only when absent, flip the label) so the existing `_handle_resolving_conflict` handler does rebase + force-with-lease push + relabel-to-validating in one consistent flow.

Applying `hold_base_sync` to an issue skips both paths for that issue; removing the label lets the next tick perform the accumulated base sync once. The `question` workflow label skips base sync unconditionally for the same read-only reason `_handle_question` already tears down its own worktree on every safe exit — merging `origin/<base>` into a question worktree would either accrete commits on a read-only branch or mask the inspection state of an unsafe park (`question_commits` / `question_dirty` / `question_timeout`).

A local-only rebase on a pushed branch would otherwise diverge local HEAD from `pr.head.sha` and break the validating reviewer (it reads local HEAD, so it would snapshot `agent_approved_sha` to a SHA that isn't on the PR), `_squash_and_force_push`'s `--force-with-lease=<original_head>` (the lease compares against the un-rebased remote tip), and AUTO_MERGE's `agent_approved_sha == pr.head.sha` gate. The detour works under `AUTO_MERGE=off` too — `_handle_resolving_conflict` never reads AUTO_MERGE, it just does rebase + push + relabel.

The detour deliberately does NOT call `_bump_in_review_watermarks` (the `_handle_in_review` analog runs that AFTER scanning new comments — running it here, before any handler scans, would silently mark unread human "do not merge" / fix-request comments as consumed and AUTO_MERGE could land the PR over them). The orchestrator's own PR notice is filtered out via `orchestrator_comment_ids` on the next in_review scan, so leaving the watermark alone is safe.

The detour also skips when `awaiting_human=True` because `_handle_resolving_conflict`'s awaiting-human branch returns early without rebasing unless a new human comment arrived; relabeling here would just hide the existing park behind a `resolving_conflict` label without progress, including the documented `AUTO_MERGE=off` unmergeable-park case.

Before relabeling, the detour fetches `gh.get_pr(pr_number)` and skips when `pr_state != "open"`: a just-merged PR advances `origin/<base>`, so the still-validating / still-in_review worktree pointed at the now-stale branch is naturally behind base; without this gate the refresh would post an "auto-resolution" notice and relabel to `resolving_conflict` on a PR the next handler call would finalize to `done` (or `rejected` for a closed-without-merge PR).

A `gh.get_pr` failure is treated as "leave alone" so the handler retries from a stable label rather than racing a half-known PR state. Issues already labeled `resolving_conflict` are also skipped (the handler runs this tick anyway).

Rebase is used across both paths to keep issue branches linear after sibling PRs land. Dirty worktrees (in-flight agent edits, crash-recovered trees) are skipped, and on a pre-PR content conflict the rebase is aborted so the worktree stays on its pre-rebase SHA. For PR branches, every pushed rebase resets `review_round`, so the reviewer must approve the rewritten head before auto-merge can proceed. Failures are logged and swallowed; keeping every issue moving matters more than perfect base sync.

Then `gh.list_pollable_issues()` yields all open non-PR issues plus closed non-PR issues still labeled `in_review`, `resolving_conflict`, or `question`. The closed-`in_review`/`resolving_conflict` sweep is what makes the manual-merge path land cleanly: a human-merged PR with a `Resolves #N` footer auto-closes issue N before the orchestrator can flip the label, and without the sweep `_handle_in_review` / `_handle_resolving_conflict` would never run on it. The closed-`question` sweep does the same for the Q&A path: a human closing the issue is the terminal signal `_handle_question` consumes to finalize the issue to `done` and clean up the per-issue worktree/branch.

For every yielded issue:

1. Read its workflow label (one of `decomposing/ready/blocked/umbrella/implementing/validating/in_review/resolving_conflict/question/done/rejected`).
2. Dispatch by label. The full lifecycle (no label → `decomposing` → `ready`/`blocked`/`umbrella` → `implementing` → `validating` → `in_review` → `resolving_conflict` (optional detour) → `done`/`rejected`) is implemented; `done` and `rejected` are terminal no-ops, every other label routes to its handler. The operator-applied `question` label is an out-of-lifecycle branch (no automatic stage transitions in or out — see [`_handle_question`](#_handle_question-label-question)). Every handler receives the active `RepoSpec`, so `git worktree add`, `git fetch origin <base>`, push-token resolution (`config._resolve_github_token(spec.slug)`), and PR-base selection all flow from the spec rather than module-level `config.REPO` / `config.TARGET_REPO_ROOT` / `config.BASE_BRANCH` reads.

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
- `pr_last_comment_id` — in_review high-watermark across the issue thread + PR conversation comments, which share the IssueComment id space. Seeded at validating → in_review handoff so the orchestrator's own automated comments don't replay as fresh feedback, and bumped past any park comment so an HITL ping doesn't replay either.
- `pr_last_review_comment_id` — separate watermark for inline PR review comments, which live in their own id space.
- `pr_last_review_summary_id` — separate watermark in the PullRequestReview id space, distinct from both IssueComment and PullRequestComment ids.

  The watermark *only* advances from review IDs that survived `gh.pr_reviews_after`'s state/body filter — non-empty `CHANGES_REQUESTED` or `COMMENTED` — so `APPROVED`, `DISMISSED`, `PENDING`, and empty-body reviews **never** bump it. `_bump_in_review_watermarks` mirrors the same filter and advances strictly from the filtered list.

  This is safe because the same filter runs on every scan, so an `APPROVED` review id sitting above the watermark is harmlessly re-skipped each tick rather than re-forwarded.
- `agent_approved_sha` — the head SHA the reviewer agent OK'd; `_handle_in_review` keys AUTO_MERGE on this since the agent posts an issue comment, not a real PR review.
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

    - a clean pushed fix flips back to `validating` from `in_review` / `resolving_conflict`, stays in `validating` with `review_round++` from `validating`, or runs the implementing `_on_commits` path to open/push the PR from `implementing`;
    - a no-commit reply is treated as an ack ONLY when it carries the explicit `ACK: <reason>` marker the resume prompt instructs the dev to emit when the existing work already satisfies the edit.

      The dev's justification is posted on the issue as an FYI and the handler does NOT park awaiting_human, so a harmless clarification doesn't stall the issue;
    - any other no-commit response (a real clarification question, an ambiguous comment, an empty message) falls back to `_on_question` and parks awaiting human.

      Without the explicit marker requirement, a clarification question would be silently swallowed as "existing work satisfies" and the issue would advance with `awaiting_human=False`, stranding the question.

    The watermark advance is what prevents the validating → in_review handoff from later replaying the same human comment via `_seed_watermark_past_self` and triggering a duplicate dev resume.

    Per-stage specifics:

    - For the `in_review` drift specifically, BOTH the "pushed" and "ack" outcomes bounce back to `validating` and clear `agent_approved_sha`: a content drift invalidates the prior reviewer approval (it was for the old requirements), so even when the dev confirms the existing code already satisfies the edit, AUTO_MERGE must not land the PR until the reviewer agent re-evaluates against the updated body/comments.

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
     - **decision == "split"** → crash-safe creation in three phases. The decomposer prompt requires the last child to be a documentation-update task whose `depends_on` lists every preceding child, so docs updates land after the code changes they describe.
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
  5. If any child is closed (`state=="closed"`) but its label is not `done`, `rejected`, or `in_review` → park parent awaiting human.

     A child closed manually (e.g. via the GitHub UI) before reaching `in_review` is invisible to `list_pollable_issues` (which only sweeps closed-but-`in_review` for the externally-merged path), so its workflow label stays frozen and the parent would otherwise wait forever for it. `in_review` is intentionally excluded — the closed-`in_review` sweep finalizes that transient on the next tick.
  6. If every child is `done` → post a summary comment, flip parent → `ready`. The next tick `_handle_ready` picks it up and the implementer takes over.
  7. Otherwise walk children: any `blocked` child whose recorded dependencies are all `done` gets relabeled `ready`. A child with no recorded deps is also flipped (vacuous all-done over an empty list) — this recovers no-dep children that the decomposer's same-tick activation step left as `blocked`.

     This walk both unblocks middle-of-the-graph children and rescues stuck activations without waiting on the parent.
- **Output**: parent → `ready` (all done), OR a sibling unblocked, OR a HITL park (rejected child, manually-closed child, or unattributed `blocked`), OR a no-op for a child still waiting on its dependencies.

### `_handle_umbrella` (label `umbrella`)
- **Trigger**: each tick while the label is `umbrella` (only ever a parent — set by the decomposer when the manifest's `umbrella` boolean is true).
- **Input**: pinned `children` and optional `dep_graph` on the parent.
- **Internal flow**: mirrors `_handle_blocked` for the rejected / manually-closed checks and the dep-graph activation walk; the only difference is the all-done terminal.

  An umbrella parent has no implementation work of its own — its purpose is purely aggregation — so when every child reaches `done`, the handler posts a checkmark comment, stamps `umbrella_resolved_at`, sets label `done`, and closes the issue (no flip back through `ready`/`implementing`).

  A `children`-less umbrella is treated as corrupt state and parks awaiting human.
- **Output**: terminal `done` (all children resolved, issue closed), OR a sibling unblocked, OR a HITL park, OR a no-op.

### `_handle_implementing` (label `implementing`)
- **Trigger**: each tick while the label is `implementing`.
- **Input**: issue + comments + pinned state (`dev_agent`/`dev_session_id`, retry-budget keys, etc.).
- **Internal flow**:
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
     - new commits + clean tree → `_on_commits`: push branch, open PR (or reuse an existing open one), comment `:sparkles: PR opened: #N`, set label `validating`, reset `review_round=0` and `retry_count=0` (next bounce back into implementing starts fresh).
     - new commits + dirty files → `_on_dirty_worktree`: park; refuse to publish a partial branch.
     - no new commits → `_on_question`: post the agent's last message as a HITL question, park.
- **Output**: a pushed branch + open PR + label moved to `validating`, OR a HITL park.

### `_handle_validating` (label `validating`)
- **Trigger**: each tick while label is `validating` (set after PR opens).
- **Input**: PR #, branch, `dev_agent`/`dev_session_id` (or legacy `codex_session_id`), pinned state, `review_round`.
- **Internal flow**:
  1. Awaiting-human path: same resume mechanic as implementing (resume on the dev's locked spec (backend + args)); on a successful pushed fix, bump `review_round` and stay in `validating` so the reviewer runs next tick.

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
       6. Seed the in_review comment watermarks, then set label `in_review`.
     - `unknown` (no marker) → park.
     - `changes_requested` → post the feedback to the PR, then **resume the developer's session** on its locked spec (backend + args) with the fix prompt; if it produces a new commit on a clean tree, push and increment `review_round` for next tick.
- **Output**: label moved to `in_review` (approval, squash succeeded or disabled) OR a new fix commit + bumped round OR a HITL park (squash/force-push failure stays on `validating` with the approval comment already on the PR; every other park branch keeps the existing label).

### `_handle_in_review` (label `in_review`)
- **Trigger**: each tick while label is `in_review` (set by `_handle_validating` after `VERDICT: APPROVED`). Also runs on closed-`in_review` issues yielded by the closed-issue sweep, so an external manual merge gets finalized to `done` even when `Resolves #N` already closed the issue.
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
  3. **PR-comment debounce → dev resume → bounce back to validating.** Read four sources independently, one per id namespace:
     - `gh.comments_after(issue, pr_last_comment_id)` (issue thread).
     - `gh.pr_conversation_comments_after(pr, pr_last_comment_id)` (PR conversation; shares id space with the issue thread, so one watermark suffices).
     - `gh.pr_inline_comments_after(pr, pr_last_review_comment_id)` (inline review comments).
     - `gh.pr_reviews_after(pr, pr_last_review_summary_id)` (PR review summary bodies submitted with `CHANGES_REQUESTED` or `COMMENTED` — `APPROVED` bodies are filtered out as informational, dismissed/pending never count, empty bodies are dropped).

     Without the `pr_reviews_after` surface, a "Comment" review with a request in the body would be silently ignored (and may be auto-merged over), and a `CHANGES_REQUESTED` review with body but no inline comments would block merge via `pr_has_changes_requested` without ever reaching the dev agent.

     If any source is newer than its watermark and the most recent one is older than `IN_REVIEW_DEBOUNCE_SECONDS` (default 600s), build a follow-up prompt that quotes them and call `_resume_dev_with_text` on the dev's locked spec (backend + args).

     On a successful pushed commit (clean tree + push ok), bump each watermark to the newest seen in its own id space, reset `review_round=0`, and flip the label back to `validating` so the reviewer agent re-runs on the new diff next tick.

     If still inside the debounce window, return — the human may still be typing.
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

     **Caveat:** once that flip lands, the issue is closed AND labeled `rejected`, so it falls outside `list_pollable_issues` (which only sweeps closed issues still labeled `in_review` or `resolving_conflict`) and the terminal-label dispatcher is a no-op. If the operator subsequently closes the PR, the orchestrator will never observe it and `_cleanup_terminal_branch` will not run — the worktree, local branch, and remote branch must be removed by hand for that ordering. The reverse ordering (close the PR first, then the issue) is fully automated by the `pr_status == "closed"` arc above.
  6. Every park inside this handler bumps the in_review watermarks past the orchestrator's own park comment via `_bump_in_review_watermarks`, so the next tick does not see the HITL ping as fresh PR feedback and resume the dev agent against it.
- **Output**: label moved to `done` / `rejected` (terminal) OR a fix push and label bounce to `validating` OR a relabel to `resolving_conflict` (under `AUTO_MERGE=on` when the PR is unmergeable past the approval gates) OR a HITL park OR a no-op tick.

The "back to validating on a new PR comment" arc is intentional: validating is the stage that re-runs the reviewer after a fix is pushed. Staying in `in_review` would skip the automated re-review and rely on humans alone, contradicting the validating loop.

`_park_awaiting_human` posts on the issue (not the PR) so the HITL ping appears alongside the rest of orchestrator state. The PR comment that triggers a resume is the human signal; awaiting-human is reserved for *unrecoverable* states (failed checks / push fail / missing pr_number — note: under `AUTO_MERGE=on` "not mergeable" detours to `resolving_conflict` instead of parking).

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
     - `ahead > 0` (recovered unpushed commits from a previous tick that crashed before `_push_branch` returned) → run the same dirty-tree check `_on_dirty_worktree` uses, then push the recovered work and flip to `validating` with `review_round=0`, `conflict_round += 1`.
     - `(0, 0)` (in sync) → fall through.
  9. Refresh `origin/<base>` over the same hardened path, then run `git rebase origin/<base>` in the worktree under `_git_hardened` (drops global/system git config, disables hooks/fsmonitor/credential helpers/commit signing, and disables rebase autostash — the agent owns the worktree and could otherwise plant a hook to execute attacker code mid-rebase).
  10. **Clean rebase succeeded**: dirty-tree check first (a leftover edit from a crashed prior tick must not silently survive into validating).

      If the HEAD SHA did not move (already up-to-date — `git rebase` returned success without applying anything), skip the push and flip to `validating` with `review_round=0`, `conflict_round += 1`; counting the no-op against the cap surfaces a perpetually-unmergeable-due-to-branch-protection PR within `MAX_CONFLICT_ROUNDS` ticks instead of letting it ping-pong between handlers forever.

      If HEAD moved, force-with-lease push the rebased branch, clear any stale `agent_approved_sha`, and flip to `validating` (same state writes).
  11. **Conflicted rebase**: build a conflict-resolution prompt via `_build_conflict_resolution_prompt` (lists up to 20 conflicted paths, instructs the agent to resolve and continue the rebase, and not push), resume the dev session on the locked spec (backend + args) with that prompt, then run `_post_conflict_resolution_result`.
  12. `_post_conflict_resolution_result` is the shared post-agent funnel:
      - timeout → park (HITL);
      - unfinished rebase → park;
      - no new commit → `_on_question` park;
      - dirty tree → `_on_dirty_worktree` park;
      - push fail → park;
      - success → force-with-lease push, clear any stale `agent_approved_sha`, set `last_conflict_resolved_at`, increment `conflict_round`, reset `review_round=0`, flip back to `validating`.

      Fresh conflicted-rebase pushes pin the lease to the pre-rebase PR head captured after the branch/head equality gate. Awaiting-human resume pushes deliberately use `_push_branch`'s live `ls-remote` lease fallback, because the local `before_sha` may be an intermediate rebase or recovered commit SHA rather than the remote PR head.

      The counter increments only on the success path so a timeout/dirty/push-fail does not eat a slot from the cap.
- **Output**: label moved to `validating` (clean push or up-to-date base) OR `done`/`rejected` (terminal arcs) OR a HITL park (cap exhausted, dirty worktree, push fail, agent timeout, agent silence, fetch fail, diverged worktree, missing pr_number).

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

## Agent command specs

`DEV_AGENT`, `REVIEW_AGENT`, and `DECOMPOSE_AGENT` are shell-like command specs, not bare backend names. `config._parse_agent_spec` runs `shlex.split` over each value and yields `(backend, extra_args)`:

- **First token rule**: must match `codex` or `claude` case-insensitively (`tokens[0].lower()` is what `_parse_agent_spec` compares, so `CODEX`, `Claude`, and `codex` all parse to the same backend). The lowercased form is used only for dispatch — `agents.run_agent` keys off it to pick `_run_codex` vs. `_run_claude`.

  Pinned state (`dev_agent` / `review_agent` / `decomposer_agent`) stores the **raw spec string verbatim** (whatever the env had at first spawn, including the original casing — `DEV_AGENT=CODEX -m gpt-5.5` is persisted as the literal `"CODEX -m gpt-5.5"`); the re-lowercase happens again on every resume when `_parse_agent_spec` re-parses the stored string.

  Any other first token value (full path, alias, typo, empty string, unbalanced quotes) aborts at import with a SystemExit so a misconfiguration cannot silently fall back to a default backend on the next restart. `DECOMPOSE_AGENT` is parsed at import even when `DECOMPOSE=off`, so toggling the kill switch back on never surfaces a fresh "that env var was always invalid" failure.
- **Remaining tokens**: forwarded verbatim as backend-CLI args on every spawn for that role — typically model / reasoning-effort selection. Quoting follows shell rules, so values containing `=`, spaces, or nested quotes survive the round-trip (e.g. `codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`).

  For codex these are placed BEFORE the `exec` subcommand (they are codex global options); for claude they are placed right after the binary, before the orchestrator's own `-p` / `--dangerously-skip-permissions` / `--output-format` flags. The safety/output flags and the prompt stay where they are so operator-provided args cannot silently displace them.
- **`CODEX_BIN` / `CLAUDE_BIN` interaction**: the first token is only a backend selector — it picks `_run_codex` vs. `_run_claude` in `agents.py`. The actual executable launched is `config.CODEX_BIN` when the first token is `codex` and `config.CLAUDE_BIN` when it is `claude`, so override those when the CLI is not on `$PATH`. Writing the full path as the first token is rejected (it would not match `codex` / `claude`).

Examples (any of these is a valid value for any of the three role env vars):

```dotenv
DEV_AGENT=claude
DEV_AGENT=claude --model claude-opus-4-7
DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'
REVIEW_AGENT=codex -m gpt-5.5-codex
REVIEW_AGENT=claude --model claude-sonnet-4-6 --effort high
DECOMPOSE_AGENT=claude --model claude-opus-4-7
```

### In-flight session lock

The parsed spec is persisted to pinned state as the **durable role identity** for an issue, so a config flip mid-flight cannot retarget a live session:

- `_handle_implementing` writes the current spec to `dev_agent` (raw string, e.g. `"codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"`) BEFORE invoking `run_agent`. A spawn that nevertheless commits without surfacing a session id (empty `-o` file, unparseable JSONL line) therefore still anchors the role.
- `_handle_decomposing` does the same for `decomposer_agent`.
- On every resume, `_read_dev_session` / `_read_decomposer_session` re-parse the stored string via the same `_parse_agent_spec` to recover `(backend, extra_args)`. This is what guarantees in-flight issues keep using the **pinned full spec until the session ends** — flipping `DEV_AGENT` / `DECOMPOSE_AGENT` in env only affects fresh issues, and only after the in-flight issue reaches a terminal label (`done` / `rejected`).
- Legacy bare-backend values (`"codex"` / `"claude"`) round-trip to `(backend, ())` — no args, matching what those deployments had at the time. The pre-spec `codex_session_id` key is also still honored on read and yields `spec="codex"`.
- The reviewer is spawned **fresh every round**, so `REVIEW_AGENT` changes take effect on the next validating tick. The current value is recorded in `review_agent` for traceability only.

## Agent subprocess (`agents.run_agent`)

`run_agent(backend, prompt, cwd, ...)` dispatches to the per-backend runner (`_run_codex` / `_run_claude`); `backend` is one of `"codex"` / `"claude"` and is re-validated at call time so a misuse fails loudly. Both runners return a unified `AgentResult(session_id, last_message, exit_code, timed_out, stdout, stderr)`. `CodexResult` is kept as a transitional alias for one release.

- **Trigger**: called by handlers with a backend name + prompt + worktree path.
- **Codex command**: `codex exec [-C cwd | resume <sid>] --dangerously-bypass-approvals-and-sandbox --json -o <tempfile> <prompt>`. The `-o` path is a per-spawn `tempfile.mkstemp` outside the worktree (so target repos without `.codex-*` in `.gitignore` don't see it as untracked); `last_message` is read from it and the tempfile is unlinked in a `finally` block.
- **Claude command**: `claude -p --dangerously-skip-permissions --output-format stream-json --include-partial-messages --verbose <prompt>` (with `--resume <sid>` when resuming). `last_message` is parsed from the stream-json: prefers the terminal `{"type":"result","result":...}` event, falls back to the last `assistant`/`message` text content for schema-drift forward-compat.
- **Input**: prompt string; optional resume session id; timeout (`AGENT_TIMEOUT`/`REVIEW_TIMEOUT`).
- **Environment**:
  - GitHub-token-bearing env vars are stripped (`GITHUB_TOKEN`, `GH_TOKEN`, etc.) so a prompt-injected agent cannot push or call the GitHub API. Provider auth (`ANTHROPIC_API_KEY`, OpenAI keychain, etc.) is intentionally left intact — that is how the agent reaches its own model.
  - `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL` are injected from `AGENT_GIT_NAME`/`AGENT_GIT_EMAIL` (default `agent-orchestrator <agent-orchestrator@users.noreply.github.com>`) so agent commits are stamped with the orchestrator's identity, regardless of the host's `~/.gitconfig`.
- **Output**: `AgentResult(...)`. `session_id` is harvested by walking the JSONL events for any UUID-shaped value at `session_id`/`conversation_id`/etc. (shared between both backends).

## Push path (`workflow._push_branch`)

The orchestrator (not the agent) pushes. The push is hardened against the agent-controlled worktree:
- Token delivered via `GIT_ASKPASS` tempfile, never argv.
- Detaches from `~/.gitconfig` and `/etc/gitconfig` (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`).
- Disables `core.hooksPath`, `credential.helper`, `core.fsmonitor`.
- Refuses to push if the worktree's local config has any `url.*.insteadOf`/`pushInsteadOf` rewrite.
- Pushes via explicit refspec `HEAD:refs/heads/<branch>` (no upstream stored).

## Audit event log (`EVENT_LOG_PATH`)

Optional, opt-in JSONL sink. When `config.EVENT_LOG_PATH` is set (parsed at import from the `EVENT_LOG_PATH` env var), `github._write_event_record` appends one JSON object per audit event to that file inside `GitHubClient.emit_event`; when unset (the default) the helper short-circuits to a no-op and no file is opened. The fake `GitHubClient` in `tests/fakes.py` calls the same `_write_event_record` helper so a single test can cover both the in-memory `recorded_events` capture and the on-disk surface.

**Schema.** Every record is built by `github.build_event_record` and carries `ts` (UTC ISO-8601 at second precision), `repo` (the slug `owner/name`), `issue` (issue number, int), and `event` (the kind). `stage` is included when the emitter passes one (effectively always today).

Extras whose value is `None` are dropped, so callers can pass optional context (`session_id`, `review_round`, `retry_count`, ...) unconditionally without polluting records that don't carry them. `json.dumps` is called with `sort_keys=True` so the on-disk order is stable across writers.

**Event kinds.** Every kind is emitted through the single `GitHubClient.emit_event` chokepoint, which also appends to a capped in-memory tail (`recorded_events`, `_RECORDED_EVENTS_CAP = 500`) for tests and short-window debugging — the file is the durable record.

| `event` | Emitter | Notable extras |
|---|---|---|
| `stage_enter` | `set_workflow_label` (via `_emit_stage_enter`) for every label flip | `stage` |
| `agent_spawn` / `agent_exit` | `_run_agent_with_tracking` wraps every `run_agent` call (decomposer, implementer, reviewer, dev-resume, conflict-resolution dev) | both carry `agent` (backend), `agent_role`, `review_round`, `retry_count`. `session_id` and the `agent_exit`-only fields are described below the table. |
| `review_verdict` | `_handle_validating` after `_parse_review_verdict` reads the reviewer's last message | `verdict` (`approved` / `changes_requested` / `unknown`), `review_round`, `pr_number`, `session_id` |
| `park_awaiting_human` | every `_park_awaiting_human` call site, plus `_on_question`, `_on_dirty_worktree`, `_park_verify_failure`, and the question-stage `_park_question` funnel | `stage` (read from the current workflow label, not passed in), `reason` (`agent_timeout`, `push_failed`, `failed_checks`, `agent_question`, `agent_silent`, `dirty_worktree`, `reviewer_timeout` / `reviewer_failed`, `missing_pr_number`, `verify_failed` / `verify_timeout` / `verify_dirty` / `verify_head_changed`, `question_answer` / `question_silent` / `question_timeout` / `question_commits` / `question_dirty` / `question_unsafe_relabel`, ...) |
| `pr_opened` | `_on_commits` after `gh.open_pr` succeeds | `pr_number`, `branch`, `sha`, `retry_count` |
| `pr_merged` | `_handle_in_review` and `_handle_resolving_conflict` terminal arcs (external merge OR successful `gh.merge_pr` under AUTO_MERGE) | `pr_number`, `sha`, `merge_method` (`external` / `squash`), `check_state`, `review_round`, `conflict_round`, `retry_count` |
| `pr_closed_without_merge` | `_handle_in_review` and `_handle_resolving_conflict` when the PR is closed without merge | `pr_number`, `sha`, `review_round`, `conflict_round`, `retry_count` |
| `merge_attempt` | AUTO_MERGE `gh.merge_pr` call AND every `git rebase origin/<base>` inside `_handle_resolving_conflict` | `method` (`squash` / `base_rebase`), `result` (`success` / `failed` / `conflict`), `pr_number`, `sha`, `conflict_round`, `review_round`, `retry_count` |
| `conflict_round` | `_route_pr_worktree_to_resolving_conflict` and the in_review unmergeable arc emit `action="entered"`; every increment site (`_emit_conflict_round_incremented`) emits `action="incremented"` with `outcome` | `pr_number`, `conflict_round`, `review_round`, `retry_count`, `outcome` (for increments), `sha` |

**`agent_spawn` / `agent_exit` extras.** On top of the shared fields above:

- On `agent_spawn`, `session_id` is the resume session id and is OMITTED for fresh spawns — the caller passes `resume_session_id=None` and `build_event_record` drops `None`-valued extras, so a fresh-spawn record has no `session_id` key at all.
- On `agent_exit`, `session_id` is the result id from `AgentResult`.
- `agent_exit` additionally carries `duration_s`, `exit_code`, and `timed_out`, computed from the `run_agent` return value; none of these three are emitted on `agent_spawn`.

**No built-in rotation.** `_write_event_record` reopens the file in append mode for every event (`path.open("a", ...)` after `path.parent.mkdir(parents=True, exist_ok=True)`); there is no long-lived file descriptor, no size cap, no rename, and no compression. External rotation and recreation are operator-managed — pair `EVENT_LOG_PATH` with `logrotate` (or equivalent) for long-running deployments.

Because each append re-resolves the path, create/rename-style rotation is as safe as `copytruncate`: the next event picks up the new inode without any `SIGHUP` or restart.

An `OSError` during the append is caught and downgraded to a `log.warning` so a misconfigured path (read-only mount, disk full, permission failure) cannot stop the per-issue tick from making progress; the missing record is silently dropped and the pinned state on GitHub remains correct.

**Pinned state is authoritative.** The event log is append-only and observation-only. The orchestrator never reads it back; every dispatch decision keys off the pinned `<!--orchestrator-state ...-->` JSON comment on the issue (and the issue's workflow label).

If the two disagree — a write failed and was logged-and-swallowed, the file was truncated by `logrotate`, events were lost during a disk-full window, or a crash interleaved partially-flushed lines — trust pinned state. The append-only log is therefore safe to truncate or delete at any time without affecting workflow correctness; it does not contribute to durability.

## Summary of "what runs when"

| Component | Type | Trigger | Cadence |
|---|---|---|---|
| `main` polling loop | long-lived Python process | manual start (or wrapper) | every `POLL_INTERVAL`s |
| `workflow.tick(gh, spec)` | function call | each loop iteration | once per tick **per configured `RepoSpec`**, fanned out across a `ThreadPoolExecutor` (one worker thread per repo) when N>1; single-repo legacy mode collapses to N=1 and stays in-thread |
| `_refresh_base_and_worktrees(gh, spec)` | function call | start of each `workflow.tick` | once per tick per repo: one `git fetch <spec.remote_name> <spec.base_branch>` (remote defaults to `origin`, overridable per `REPOS` entry), then per-worktree dispatch (pre-PR worktrees rebase directly; PR-having worktrees behind base detour to `resolving_conflict`). See [Per-tick flow](#per-tick-flow-workflowtick) for the full open-PR / `awaiting_human` / watermark / conflict / dirty-tree rules. |
| `_handle_*` per issue | function call | issue's workflow label | once per tick per open issue (within its repo's `tick`); concurrent up to `spec.parallel_limit` per repo and `MAX_PARALLEL_ISSUES_GLOBAL` across all repos (single shared `BoundedSemaphore`) |
| decomposer agent (`DECOMPOSE_AGENT`) | subprocess (fresh or resumed, locked spec (backend + args)) | `_handle_decomposing` (retry budget OK) or HITL resume | one shot per tick when needed |
| implementer agent (`DEV_AGENT`) | subprocess | `_handle_implementing` (no commits yet, retry budget OK) or HITL resume | one shot per tick when needed |
| reviewer agent (`REVIEW_AGENT`) | subprocess (fresh session) | `_handle_validating`, round < max | one shot per tick |
| dev-fix agent | subprocess (resumed dev session, locked spec (backend + args)) | reviewer says CHANGES_REQUESTED | one shot per tick |
| `_handle_resolving_conflict` | function call | issue label `resolving_conflict` (set by `_handle_in_review` when an approved PR is unmergeable under `AUTO_MERGE=on`); also fires on closed-`resolving_conflict` issues from the polling sweep | once per tick per such issue (drives PR-state terminals → `done`/`rejected`, ahead-of-remote recovery push, `git rebase origin/<base>` then clean-rebase no-op flip / clean-rebase push / dev-conflict resume / cap-park, plus all park branches) |
| dev-conflict agent | subprocess (resumed dev session, locked spec (backend + args)) | `_handle_resolving_conflict` and `git rebase origin/<base>` left conflicts | one shot per tick |
| `_handle_question` | function call | issue label `question` (operator-applied) OR closed-`question` issue from the polling sweep | once per tick per such issue; closed terminal finalizes to `done` + tears down the worktree, open issue spawns the question agent (or resumes it on a new human comment) and parks awaiting human |
| question agent (`DECOMPOSE_AGENT` backend) | subprocess (read-only; fresh first spawn, locked spec on resume) | `_handle_question` (no prior session OR new human comment on a parked Q&A) | one shot per tick when needed |
| `git push` | subprocess | after dev produces clean commits | per fix |
| self-restart check | git fetch + diff | start of each tick | every tick |

## Architecture schema

```
                     ┌──────────────────────────────────────┐
                     │   GitHub repo(s) (REPO or REPOS)     │
                     │   ─ one orchestrator drives N repos  │
                     │   ─ issues (with workflow labels)    │
                     │   ─ pinned state comment per issue   │
                     │   ─ branches / PRs                   │
                     └──────────────┬───────────────────────┘
                                    │ PyGithub (one token per slug)
                                    │
   ┌────────────────────────────────┴─────────────────────────────────────┐
   │  orchestrator process  (python -m orchestrator.main)                 │
   │  ───────────────────────────────────────────────────                 │
   │   main.py                                                            │
   │     startup: build [(spec, GitHubClient(repo_spec=spec)), ...] from  │
   │              config.default_repo_specs() and ensure_workflow_labels  │
   │              once per spec; build one shared                         │
   │              global_semaphore = BoundedSemaphore(                    │
   │                  MAX_PARALLEL_ISSUES_GLOBAL)                         │
   │     loop every POLL_INTERVAL s:                                      │
   │       1. self-restart check                                          │
   │          (origin/<ORCHESTRATOR_BASE_BRANCH> moved & touches orch/?)   │
   │       2. _run_tick(clients, global_semaphore):                       │
   │            len(clients) == 1 → in-thread workflow.tick(              │
   │                                  gh, spec,                           │
   │                                  global_semaphore=global_semaphore)  │
   │            len(clients)  > 1 → ThreadPoolExecutor                    │
   │                                  (max_workers=len(clients)) fans     │
   │                                  workflow.tick(gh, spec,             │
   │                                  global_semaphore=global_semaphore)  │
   │                                  across one worker thread per repo   │
   │          (per-repo exception logged + skipped, never aborts the tick)│
   │                    │                                                 │
   │                    ▼                                                 │
   │   workflow.tick(gh, spec, global_semaphore=...) →                    │
   │     partition pollable issues by label:                              │
   │       family-aware (decomposing/blocked/umbrella/unlabeled) → drain  │
   │         sequentially on one worker (no parent↔child races)           │
   │       fan-out (ready/implementing/validating/in_review/              │
   │                resolving_conflict) → up to spec.parallel_limit       │
   │         worker threads, each with its own gh._for_worker_thread()    │
   │     every _process_issue call acquires global_semaphore, so total    │
   │     in-flight handlers across all repos ≤ MAX_PARALLEL_ISSUES_GLOBAL │
   │   → for each issue → dispatch by label:                              │
   │                                                                      │
   │     (no label) ──► _handle_pickup                            │       │
   │                       ├─ ALLOWED_ISSUE_AUTHORS skip?         │       │
   │                       ├─ DECOMPOSE=on  ─► decomposing        │       │
   │                       └─ DECOMPOSE=off ─► implementing       │       │
   │                                                              │       │
   │     decomposing ──► _handle_decomposing                      │       │
   │                       ├─ retry budget? ─► park if exhausted  │       │
   │                       ├─ ensure worktree (read-only)         │       │
   │                       ├─ run_agent(DECOMPOSE_AGENT, prompt)  │       │
   │                       ├─ decision=single ─► label=ready      │       │
   │                       ├─ decision=split  ─► create children  │       │
   │                       │     parent=blocked (or `umbrella`    │       │
   │                       │     when manifest umbrella=true),    │       │
   │                       │     child=blocked,                   │       │
   │                       │     no-dep child ─► child=ready      │       │
   │                       └─ invalid / question / dirty ─► park  │       │
   │                                                              │       │
   │     ready ──► _handle_ready ──► label=implementing           │       │
   │                                                              │       │
   │     blocked ──► _handle_blocked                              │       │
   │                       ├─ all children done ─► parent=ready   │       │
   │                       ├─ any child rejected ─► park HITL     │       │
   │                       └─ unblock siblings (dep_graph walk)   │       │
   │                                                              │       │
   │     umbrella ──► _handle_umbrella                            │       │
   │                       ├─ all children done ─► parent=done,   │       │
   │                       │     close issue (no implementation)  │       │
   │                       ├─ any child rejected ─► park HITL     │       │
   │                       └─ unblock siblings (dep_graph walk)   │       │
   │                                                              │       │
   │     implementing ──► _handle_implementing ───────────────────┤       │
   │                       │                                      │       │
   │                       ├─ ensure worktree                     │       │
   │                       ├─ retry budget? ─► park if exhausted  │       │
   │                       ├─ run_agent(DEV_AGENT, prompt) ◄──────┼──┐    │
   │                       ├─ commits+clean? push, open PR,       │  │    │
   │                       │     label=validating                 │  │    │
   │                       ├─ dirty?  ─► park awaiting human ─────┤  │    │
   │                       ├─ no commit? ─► park (question) ──────┤  │    │
   │                       └─ timeout? ─► park ───────────────────┤  │    │
   │                                                              │  │    │
   │     validating ──► _handle_validating                        │  │    │
   │                       │                                      │  │    │
   │                       ├─ run_agent(REVIEW_AGENT, fresh)      │  │    │
   │                       │     parse VERDICT marker             │  │    │
   │                       │       APPROVED:                      │  │    │
   │                       │         run VERIFY_COMMANDS locally  │  │    │
   │                       │           ok      ─► label=in_review │  │    │
   │                       │           failed  ─► park (typed     │  │    │
   │                       │             reason: verify_failed /  │  │    │
   │                       │             verify_timeout /         │  │    │
   │                       │             verify_dirty /           │  │    │
   │                       │             verify_head_changed)     │  │    │
   │                       │       CHANGES_REQUESTED:             │  │    │
   │                       │         post feedback on PR          │  │    │
   │                       │         run_agent(dev, fix, resume) ─┘  │    │
   │                       │         push, ++review_round            │    │
   │                       │       UNKNOWN ─► park                   │    │
   │                       └─ round ≥ MAX_REVIEW_ROUNDS ─► park      │    │
   │                                                                 │    │
   │     in_review ──► _handle_in_review                             │    │
   │                       │                                         │    │
   │                       ├─ pr merged externally ─► label=done,    │    │
   │                       │     stamp merged_at, close issue,       │    │
   │                       │     _cleanup_terminal_branch            │    │
   │                       ├─ pr closed unmerged ─► label=rejected,  │    │
   │                       │     stamp closed_without_merge_at,      │    │
   │                       │     close issue,                        │    │
   │                       │     _cleanup_terminal_branch            │    │
   │                       ├─ new PR/issue comment past debounce:    │    │
   │                       │     resume dev (locked spec (backend + args)) ────────┘    │
   │                       │     push, ++pr_last_*_id watermarks,         │
   │                       │     label=validating, review_round=0         │
   │                       ├─ AUTO_MERGE on, approved, mergeable,         │
   │                       │   green checks ─► merge_pr (sha pin),        │
   │                       │   label=done, close,                         │
   │                       │   _cleanup_terminal_branch                   │
   │                       ├─ AUTO_MERGE on, approved, unmergeable        │
   │                       │   ─► label=resolving_conflict (seed          │
   │                       │      conflict_round=0 if absent)             │
   │                       └─ failed checks / AUTO_MERGE off              │
   │                          unmergeable ─► park                         │
   │                                                                 │    │
   │     resolving_conflict ──► _handle_resolving_conflict           │    │
   │                       │                                         │    │
   │                       ├─ pr merged/closed terminals ─►          │    │
   │                       │     done / rejected (mirror in_review)  │    │
   │                       ├─ ensure PR-aware worktree, fetch        │    │
   │                       │   origin/<branch> + origin/<base>       │    │
   │                       ├─ recovered ahead-of-remote commits      │    │
   │                       │   ─► push, ++conflict_round,            │    │
   │                       │      label=validating                   │    │
   │                       ├─ git rebase origin/<base>:              │    │
   │                       │     already up-to-date (HEAD unchanged) │    │
   │                       │       ─► flip to validating,            │    │
   │                       │         ++conflict_round (no push)      │    │
   │                       │     HEAD moved (rebased commits)        │    │
   │                       │       ─► push,                          │    │
   │                       │       label=validating, ++conflict_round│    │
   │                       │     conflicts ─► resume dev (locked) ───┘    │
   │                       │       push resolved commit,                  │
   │                       │       label=validating, ++conflict_round     │
   │                       └─ conflict_round >= MAX_CONFLICT_ROUNDS       │
   │                           ─► park awaiting human                     │
   │                          dirty / push-fail / timeout ─► park         │
   │                                                                 │    │
   │   awaiting_human + new comment ─► resume dev (locked spec (backend + args)) ──┘    │
   │                                                                      │
   └─────────┬───────────────────────────────────────┬────────────────────┘
             │ subprocess                            │ subprocess (hardened)
             ▼                                       ▼
   ┌─────────────────────────────┐         ┌─────────────────────────────┐
   │  coding-agent CLI           │         │  git push                   │
   │  (codex or claude,          │         │  ─ GIT_ASKPASS tempfile     │
   │   per-issue worktree)       │         │  ─ no global/system config  │
   │  ─ env: GH tokens stripped  │         │  ─ hooks/helper disabled    │
   │  ─ env: GIT_AUTHOR/COMMITTER│         │  ─ refuses url-rewrite      │
   │     stamped (orchestrator)  │         └──────────────┬──────────────┘
   │  ─ provider auth left alone │                        │
   │  ─ --bypass / --skip perms  │                        │
   │  ─ JSONL → session_id       │                        │
   │  ─ last_message: -o (codex) │                        │
   │     or stream-json (claude) │                        │
   └──────────────┬──────────────┘                        │
                  │ commits to                            │ pushes branch to
                  ▼                                       ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  git worktree:  <WORKTREES_DIR>/<owner>__<name>/issue-<n>           │
   │  branch:        orchestrator/issue-<n>                              │
   │  ─ slug subdir keeps two repos with the same issue # from colliding │
   │  ─ created from origin/<spec.base_branch> in spec.target_root       │
   │    (or reused if has unpushed commits)                              │
   └─────────────────────────────────────────────────────────────────────┘
```

### Roles in one line

| Component | Role |
|---|---|
| **main.py** | polling loop + signal handling + self-restart |
| **workflow.py** | facade: per-repo tick loop, family-aware/fan-out partitioning, `_process_issue` dispatcher, `_handle_pickup`, `_park_awaiting_human`, `_run_agent_tracked`; re-exports the cross-module helpers and stage entry handlers (stage-private helpers like `_bump_in_review_watermarks` stay private to their module) |
| **workflow_drift.py** | user-content drift detection and re-route helpers |
| **workflow_messages.py** | prompt builders, parsers, comment posting + orchestrator-comment markers, stderr redaction |
| **worktrees.py** | git/branch/worktree plumbing, hardened fetch/push, squash-on-approval, per-tick base refresh, terminal cleanup |
| **stages/decomposition.py** | `_handle_decomposing` / `_handle_ready` / `_handle_blocked` / `_handle_umbrella` |
| **stages/implementing.py** | `_handle_implementing` + developer-session lifecycle |
| **stages/validating.py** | `_handle_validating` + reviewer-session lifecycle |
| **stages/in_review.py** | `_handle_in_review` + PR-watermark / auto-merge primitives |
| **stages/conflicts.py** | `_handle_resolving_conflict` + rebase-loop primitives |
| **stages/question.py** | `_handle_question` + question-session lifecycle (read-only Q&A on the `question` label, no PR) |
| **agents.py** | dispatch + spawn codex/claude subprocess, capture session id + last message |
| **github.py** | issues, comments, labels, pinned state, PR open/comment |
| **config.py** | env + token loading (token kept outside REPO_ROOT), backend validation |
| **codex / claude** | the only things that write code; run in isolated worktree |

### State transition (label lifecycle)

```
                         single
                       ┌─────────────────────────────┐
   (none) ──► decomposing ──► ready ──► implementing ──► validating ──► in_review ──► done | rejected
                  │                          ▲                  │              ▲ │
                  │ split                    │ all children     │              │ │  PR comment past
                  ▼                          │ done             │              │ │  debounce ─► resume
                blocked ──► (children created) ──┐              │              │ │  dev, push, label
                  ▲                              │              │              │ │  back to validating
                  └─ child rejected ─► park HITL │   CHANGES_   │              │ │
                                                 │   REQUESTED  │              │ │
                                                 │              │              └─┘
                                                 └──────────────┘
                                  (APPROVED or MAX_REVIEW_ROUNDS)

                                                 ┌──────────────────────────────┐
                                                 │  in_review --(AUTO_MERGE on, │
                                                 │   unmergeable past approval  │
                                                 │   gates)─► resolving_conflict│
                                                 │  resolving_conflict --(clean │
                                                 │   rebase / pushed resolution)│
                                                 │   ─► validating              │
                                                 │  resolving_conflict --(round │
                                                 │   >= MAX_CONFLICT_ROUNDS)    │
                                                 │   ─► park awaiting human     │
                                                 └──────────────────────────────┘

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

   in_review terminals:
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

   resolving_conflict (AUTO_MERGE only, capped by MAX_CONFLICT_ROUNDS):
     git rebase origin/<base> clean ─► label=validating (++conflict_round)
     conflicts ─► dev resumes, continues rebase, push ─► label=validating
     conflict_round >= MAX_CONFLICT_ROUNDS ─► park awaiting human
     pr merged/closed mid-stage ─► done / rejected (terminal)

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
