# Architecture of the Current Implementation

Single-process **polling orchestrator** that drives GitHub issues through a label-based state machine, delegating the actual coding work to a configurable coding-agent CLI (`codex` or `claude`) running as a subprocess in isolated git worktrees.

The dev/review/decompose roles are picked independently via `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` (default: claude decomposes, claude implements, codex reviews). Each value is a shell-like command spec: the first token must be `codex` or `claude` and selects the runner (which then launches `CODEX_BIN` or `CLAUDE_BIN`); any remaining tokens are forwarded verbatim as backend-CLI args (model, reasoning effort, etc.) on every spawn for that role. All three are parsed and validated at config load — see [Agent command specs](#agent-command-specs) below.

New unlabeled issues route through a `decomposing` stage that asks the decomposer agent for a structured manifest: `decision=single` flips the issue to `ready` and the implementer takes over; `decision=split` creates child issues, persists the dep graph, and parks the parent on `blocked` (or `umbrella` when the manifest's `umbrella` flag is true — a parent with no implementation of its own that `_handle_umbrella` closes to `done` once every child resolves) until the matching handler walks the children. Decomposition can be disabled with `DECOMPOSE=off`, which reverts to the legacy direct-to-`implementing` pickup.

Once the reviewer approves and the PR is mergeable with green CI, the orchestrator can merge it itself (gated by `AUTO_MERGE`, default off) and close the issue with `done`; an approved-but-unmergeable PR detours through a `resolving_conflict` stage that rebases onto `origin/<base>` (capped by `MAX_CONFLICT_ROUNDS`). Every pushed conflict-resolution path (clean rebase, recovered push, agent-resolved conflicts, human-reply resume, and user-content drift pushed fixes) routes through `documenting` so the docs pass runs on the rewritten tree before the reviewer re-runs; a base-up-to-date no-op (no diff changed) skips `documenting` and bounces straight back to `validating`. PRs closed without merge land on `rejected`.

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
                           `_finalize_if_pr_merged` (cross-stage external-merge
                           short-circuit reused by the `implementing` /
                           `documenting` / `validating` entry checks and by
                           the `_handle_blocked` / `_handle_umbrella` child
                           recovery), `_finalize_if_issue_closed` (closed-but-
                           not-merged counterpart that flips the issue to
                           `rejected`, emits `pr_closed_without_merge` when
                           a closed PR is pinned, and runs
                           `_cleanup_terminal_branch` only for the closed-PR
                           case; called right after `_finalize_if_pr_merged`
                           in the three new sweep handlers. Defers without
                           mutating state when the pinned PR cannot be
                           fetched, or when the second fetch reveals the
                           PR IS actually merged -- in either case the
                           helper still returns True so the caller does
                           NOT continue dev / docs / reviewer work on a
                           closed issue, and the next tick re-attempts
                           `_finalize_if_pr_merged` against a fresh PR
                           state),
                           `_run_agent_tracked`. Re-exports the
                           cross-module helpers and the stage entry handlers
                           from the modules below under their original names
                           so existing test patches
                           (`patch.object(workflow, "_foo", ...)`) keep
                           working. Stage-private helpers that no other
                           module needs (e.g. `_bump_in_review_watermarks`,
                           `_auto_merge_gates_pass`,
                           `_seed_legacy_in_review_watermarks`,
                           `_emit_conflict_round_incremented`) stay private to
                           their stage module and are NOT re-exported.
                           `_comment_created_at` IS re-exported because the
                           `fixing` handler reuses it for the quiet-window
                           debounce.
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
                           `_on_dirty_worktree`). `_on_commits` relabels to
                           `documenting`, NOT `validating`, so the docs pass
                           runs before the reviewer.
    documenting.py      — `_handle_documenting`: a docs pass that resumes the
                           dev session on the existing PR worktree, commits
                           any README / docs / plans updates, pushes them,
                           and advances to `validating`. Refuses to act on
                           a stale or diverged PR branch (fetch + behind
                           check) and routes unrecognized outcomes through
                           the existing dirty / question / push park helpers.
                           Advances without pushing only on an explicit
                           `DOCS: NO_CHANGE` verdict against a remote-clean
                           branch.
    validating.py       — `_handle_validating` plus reviewer-session
                           lifecycle: `_handle_dev_fix_result`,
                           `_post_user_content_change_result`, validating-side
                           transient-park recovery (returns
                           `"stuck"`/`"cleared"`/`"pushed"` so the caller
                           can route pushed recoveries through documenting),
                           the local-verify gate park helper
                           (`_park_verify_failure`), and the watermark
                           seeding for the validating→in_review handoff.
                           Pushed dev fixes (CHANGES_REQUESTED,
                           awaiting-human resume, drift pushed,
                           transient-park recovery push) relabel to
                           `documenting`, NOT `validating`, so the docs
                           pass runs against the new head before the
                           reviewer re-evaluates.
    in_review.py        — `_handle_in_review` plus PR-side primitives:
                           transient park-reason set, the quiet auto-merge
                           gate re-check, legacy watermark migration, and the
                           cross-namespace watermark ratchet
                           (`_bump_in_review_watermarks`). User-content
                           drift pushed fixes route through `documenting`
                           (NOT directly to `validating`) so the docs
                           pass runs against the updated body / new head
                           before the reviewer re-evaluates and
                           AUTO_MERGE can land; the no-commit ACK
                           outcome still bounces DIRECTLY back to
                           `validating` (the docs hop is skipped because
                           no commit landed for the docs pass to react
                           to). Both outcomes reset `review_round` and
                           clear `agent_approved_sha`.
    fixing.py           — `_handle_fixing` owns the PR-feedback quiet
                           window and the dev-resume / push /
                           route-through-`documenting` cycle. Stage entered
                           when `_handle_in_review` detects fresh PR
                           feedback and routes the issue there instead of
                           spawning the dev itself; rescans unread feedback
                           from the in_review watermarks each tick,
                           debounces against the freshest comment
                           timestamp, and resumes via `_resume_dev_with_text`
                           with a `_build_pr_comment_followup` prompt over
                           all unread surfaces. Pushed fixes flip to
                           `documenting` so the docs pass runs against
                           the new head before the reviewer re-evaluates;
                           the no-new-feedback bounce still flips
                           directly to `validating` because there is no
                           fix work for the docs pass to react to.
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

An issue should have at most one workflow label at a time. The set is `decomposing`, `ready`, `blocked`, `umbrella`, `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, `question`, and the two terminals `done` / `rejected`. The orchestrator also creates two non-workflow control labels: `hold_base_sync` pauses per-tick base sync and rebases while present, and `backlog` makes per-tick handlers skip the issue entirely.

Label names are part of the public contract because live GitHub issues already carry them. For the meaning of each label, the control-label semantics, and the per-stage transitions they trigger, see [`state-machine.md#workflow-labels`](state-machine.md#workflow-labels).

## Process model

There is **only one long-lived process**: `python -m orchestrator.main`. It is wrapped by `run.sh` so the loop can self-exit and be restarted with new code.

- **Trigger**: started manually (or by a wrapper). Optional `--once` for a single tick.
- **Tick cadence**: every `POLL_INTERVAL` seconds (default 60).
- **Self-restart guard** (`main._self_modifying_merge_happened`): each tick fetches `origin/<ORCHESTRATOR_BASE_BRANCH>` (default `main`); if it advanced past the process's startup SHA *and* the new commits touch `orchestrator/`, the loop exits 0 so the wrapper can re-exec the new code. The branch is decoupled from `BASE_BRANCH` so a target repo with a different default branch does not interfere with self-update detection.
- **Signals**: SIGINT/SIGTERM set a flag; the current tick finishes, then the loop exits.

The coding agent runs as a **transient child subprocess**, not a daemon — spawned per tick when work is needed.

## Per-tick flow (`workflow.tick`)

Each tick the polling loop fans `workflow.tick(gh, spec)` out across **every configured repo** via `main._run_tick`: single-repo deployments stay in-thread (legacy), multi-repo deployments use a `ThreadPoolExecutor` sized to the repo count. A single `BoundedSemaphore(MAX_PARALLEL_ISSUES_GLOBAL)` is shared across all `tick` calls so total in-flight per-issue handlers across all repos never exceeds that cap. Within a single repo, `spec.parallel_limit` partitions eligible issues into a family-aware drain bucket (`decomposing` / `blocked` / `umbrella` / unlabeled) and a fan-out bucket (everything else) so parent ↔ child writes cannot race a sibling thread.

Per-issue durable state lives in a single **pinned comment** on the issue (`<!--orchestrator-state {...json...}-->`). The orchestrator process is stateless; the label and the pinned JSON are the entire dispatch input.

For the full per-tick sequence (eligible-issue enumeration, family vs. fan-out partitioning, the pre-PR rebase / PR-having `resolving_conflict` detour, the `hold_base_sync` / `question` skips, the per-tick external-merge sweeps, and the complete pinned-state JSON schema), see [`state-machine.md#per-tick-flow-workflowtick`](state-machine.md#per-tick-flow-workflowtick).

## Stage handlers

Each workflow label dispatches to a `_handle_<label>` function. The handlers live under `orchestrator/stages/` (see the module map in [Top-level layout](#top-level-layout)) and are re-exported from `workflow.py` so test patches against `workflow.<helper>` keep intercepting calls from inside a stage handler. Every per-tick handler runs the user-content drift hook (`_compute_user_content_hash` → `_detect_user_content_change`) before its own work so an out-of-band human edit re-routes the issue back to `decomposing` (when no dev session exists yet) or resumes the locked dev session with the updated body.

For the full per-stage internal flow — pickup / drift handling / decomposing / ready / blocked / umbrella / implementing / documenting / validating / in_review / fixing / resolving_conflict / question — see [`state-machine.md#stage-handlers`](state-machine.md#stage-handlers).

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
- **Environment** (`agents._filter_agent_env`, shared with the verify-command runner; verify passes `allow_provider_auth=False` so the agent's own provider keys are stripped too — see below):
  - GitHub-token-bearing env vars are stripped (`GITHUB_TOKEN`, `GH_TOKEN`, etc. — the `_FORBIDDEN_AGENT_ENV` exact-match set) so a prompt-injected agent cannot push or call the GitHub API.
  - Production-secret-shaped env vars are stripped by name shape: anything matching `_AGENT_SECRET_SUFFIXES` (`_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD`, `_PAT`, `_CREDENTIAL`) or the bare-name set (`TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `PAT`, `CREDENTIAL`). Without this a `STRIPE_API_KEY` / `DATABASE_PASSWORD` set for the host's other work would ride into a sandbox-bypassed agent or into operator-configured verify shell running against agent-produced code.
  - Credential-file LOCATORS are stripped too -- env vars whose value is a path to a file holding the secret (`*_TOKEN_FILE`, `*_KEY_FILE`, `*_SECRET_FILE`, `*_PASSWORD_FILE`, `*_CREDENTIAL_FILE`, `*_CREDENTIALS`, `*_CREDENTIALS_FILE`, plus bare `TOKEN_FILE` / `CREDENTIALS` / `CREDENTIALS_FILE`). The agent runs as the same OS user, so leaving the pointer in env lets a hostile dependency simply `cat` the target -- the most important case is `ORCHESTRATOR_TOKEN_FILE`, the orchestrator's own write-credential locator that frequently points at a non-default path in multi-repo deployments. The strip does not protect against the agent guessing a well-known default (`~/.aws/credentials`, `~/.config/<repo>/token`), but it removes the trivial follow-the-env-var-pointer exfiltration path.
  - Write-credential locators (`_AGENT_WRITE_CREDENTIAL_LOCATORS`: `SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_ASKPASS`, `GIT_SSH_COMMAND`) are stripped by exact name. These aren't secret-shaped, but they let an agent or verify subprocess use the operator's already-loaded ssh-agent / askpass binary / SSH wrapper to push or authenticate as them. The orchestrator's own push path (`worktrees._push_branch`) constructs its own `GIT_ASKPASS` tempfile in the env it hands to `subprocess.run`, so stripping the operator's copy here does not break it.
  - Provider auth required to reach the agent's own model is allowlisted by exact name in `_AGENT_PROVIDER_AUTH_ALLOWLIST` (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`) **for agent subprocesses only**, so the shape filter does not break codex/claude. The verify-command runner passes `allow_provider_auth=False` and strips those keys too: a verify shell executes untrusted agent-produced code, and a hostile dependency reading `$ANTHROPIC_API_KEY` would gain billable access to the operator's model account. An operator who needs to drive an agent from a verify command must load the key from disk inside a wrapper script — `VERIFY_COMMANDS=./scripts/run-verify.sh` — rather than embedding the literal value, because the verify failure park comment publishes the offending command verbatim. Advanced deployments (Bedrock, Vertex, custom proxies) need to extend the allowlist explicitly.
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
| `pr_merged` | `_handle_in_review`, `_handle_fixing`, and `_handle_resolving_conflict` terminal arcs (external merge OR successful `gh.merge_pr` under AUTO_MERGE); plus `_finalize_if_pr_merged` from `_handle_implementing` / `_handle_documenting` / `_handle_validating` entry checks and from the `_handle_blocked` / `_handle_umbrella` manually-closed child recovery (all `merge_method="external"`) | `pr_number`, `sha`, `merge_method` (`external` / `squash`), `check_state`, `review_round`, `conflict_round`, `retry_count`; `stage` reflects the workflow label at finalize entry (`implementing`, `documenting`, `validating`, `in_review`, `fixing`, or `resolving_conflict`) |
| `pr_closed_without_merge` | `_handle_in_review`, `_handle_fixing`, and `_handle_resolving_conflict` when the PR is closed without merge; plus `_finalize_if_issue_closed` from the `_handle_implementing` / `_handle_documenting` / `_handle_validating` entry checks, but only when the linked PR itself is also closed (an open PR with a manually-closed issue is left alone and emits nothing here, mirroring the in_review / fixing arc; a closed issue with no `pr_number` flips to `rejected` without emitting either) | `pr_number`, `sha`, `review_round`, `conflict_round`, `retry_count`; `stage` reflects the workflow label at finalize entry |
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

## Analytics sink (`ANALYTICS_LOG_PATH`)

Project-local JSONL sink for raw metric records, separate from `EVENT_LOG_PATH`. The audit event log is wired through `GitHubClient.emit_event` for stage transitions / agent lifecycle events; the analytics sink is a foundation layer for future aggregation and reporting work that opts in or out independently via the `ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS` env knobs and the helpers in `orchestrator/analytics/`.

**Settings ownership.** `ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`, and `ANALYTICS_DB_URL` are parsed at import inside `orchestrator/analytics/__init__.py` — *not* in `orchestrator/config.py` — so the analytics package owns its own configuration surface and consumers of `config.LOG_DIR` do not pull the analytics defaults in transitively. The values are exposed as module attributes (`analytics.ANALYTICS_LOG_PATH`, `analytics.ANALYTICS_RETENTION_DAYS`, `analytics.ANALYTICS_DB_URL`); tests patch them directly via `patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`. The audit event log (`config.EVENT_LOG_PATH`) intentionally stays in `config` because `GitHubClient.emit_event` is a general-purpose audit surface, not analytics-specific.

**Filesystem only.** No PostgreSQL, Streamlit, or external services — the sink is one JSONL file under the project log area. Default path is `<LOG_DIR>/analytics.jsonl`, already covered by the `logs/` `.gitignore` rule. Set `ANALYTICS_LOG_PATH=` (empty) or to `off` / `disabled` / `none` to disable writes entirely; in that mode `append_record` and `prune_old_records` are silent no-ops and no file is opened.

**Schema.** Every record is built by `analytics.build_record` and carries `ts` (UTC ISO-8601 at second precision), `repo` (the slug `owner/name`), `issue` (issue number, int), and `event` (the kind). `stage` is included when the caller passes one; extras whose value is `None` are dropped so callers can pass optional context (`session_id`, `review_round`, ...) unconditionally without polluting records that don't carry them. `json.dumps` is called with `sort_keys=True` so the on-disk order is stable across writers. The JSONL file is the raw foundation layer: it is intended to be ingested later into a structured database (e.g. SQLite / DuckDB / Postgres) for aggregation and reporting; the on-disk format keeps a single object per line so a downstream loader can stream it without buffering the whole file.

**Event kinds written today:**

- `event="stage_enter"` -- one record per workflow label transition. Carries `stage` (the new label) and the standard `ts` / `repo` / `issue`. Emitted from `GitHubClient._emit_stage_enter` alongside the existing audit `stage_enter` event so the same chokepoint feeds both sinks. Workflow handlers that bounce an issue back to a stage (`fixing` -> `documenting`, drift detour to `decomposing`, etc.) all flow through `set_workflow_label` and therefore through this hook too -- no per-handler bookkeeping required.
- `event="stage_evaluation"` -- one record per `_process_issue` dispatch. Carries `stage` (the current workflow label, omitted when the issue has none and is routed to `_handle_pickup`), `duration_s` (handler wall-clock at second/3-decimal precision), and `result` (`"ok"` on a clean return, `"error"` when the handler raised). The record is written from a `try/except/finally` around the dispatcher body so a raising handler still produces a timing record before the exception propagates -- `workflow.tick`'s per-issue try/except keeps isolating the failure, so the existing dispatch / exception contract is preserved. Backlog-skips (`backlog` label) short-circuit before the timing wrapper and are deliberately NOT counted: no handler runs and there is nothing to time. Pairing with `stage_enter` gives non-agent stages (`decomposing` no-op ticks, `umbrella` dep-graph walks, `blocked` checks, `in_review` watermark advances, `done` / `rejected` early-returns) timing context in the same sink that `_run_agent_tracked` already populates for agent-driven stages.
- `event="agent_exit"` -- one record per tracked agent invocation, written from `workflow._run_agent_tracked` (see below). Carries the agent context plus parsed token / model / cost details.

**Append.** `analytics.append_record(record)` reopens the file in append mode for every record (`path.open("a", ...)` after `path.parent.mkdir(parents=True, exist_ok=True)`). An `OSError` during append is caught and downgraded to a `log.warning` so a misconfigured path (read-only mount, disk full, permission failure) cannot stop the per-issue tick from making progress; the pinned state on GitHub remains correct regardless.

**Retention pruning.** `analytics.prune_old_records(*, now=None)` reads the file and removes records whose `ts` is older than `ANALYTICS_RETENTION_DAYS`. It is a no-op (returns `0`) when the sink is disabled, retention is non-positive (the documented "keep raw data indefinitely" knob), or the file does not exist yet. The rewrite goes through a temp file in the same directory followed by `os.replace` so a crash mid-prune cannot truncate the analytics file. Records with a missing, non-string, or unparseable `ts` (and any line that is not valid JSON) are preserved verbatim so the prune step never silently drops data it cannot interpret; an operator can clean those lines up.

The polling loop wires retention in: `main._run_tick` calls `analytics.prune_with_retention_logging()` exactly once at the end of every tick (after both the single-repo and multi-repo paths drain), regardless of how many `RepoSpec` entries are configured -- the sink is process-wide, not per-repo, so a single prune per polling iteration is the right cadence. The wrapper lives in the analytics package and delegates to `prune_old_records`, catching exceptions and logging the `"removed N record(s)"` message so the call site in `main` stays a one-liner. Per-tick cost is bounded: the helper reads the file at most once and only rewrites it when at least one record is older than the retention window. A runaway error inside the prune is logged and swallowed so an analytics misconfiguration cannot stop the poll loop -- analytics is observability, never authoritative workflow state.

**Pinned GitHub state is unaffected.** The prune touches only the local file — no issue comment, label, or other GitHub state is rewritten. The analytics sink is local-filesystem observability and is safe to truncate or delete at any time without affecting workflow correctness.

**Per-agent-invocation records.** `workflow._run_agent_tracked` appends a single `event="agent_exit"` analytics record after every tracked agent run, distinct from (and in addition to) the existing `agent_spawn` / `agent_exit` audit events on `EVENT_LOG_PATH`. Each record carries the contextual fields (`repo`, `issue`, `stage`, `agent_role`, `backend`, `review_round`, `retry_count`, `duration_s`, `exit_code`, `timed_out`) the audit pair already covers, the configured `agent_spec` (the role's full `*_AGENT_SPEC` string, e.g. `claude --model claude-opus-4-7`), both the `resume_session_id` passed into the spawn and the live `session_id` from the result (one record carries both, unlike the audit events where each surfaces a different one), plus the parsed token counts (`input_tokens`, `output_tokens`, `cached_tokens`, `cache_read_tokens`, `cache_write_tokens`), the distinct `models` observed in the stream, the `turns` count, `cost_usd`, and `cost_source` from `usage.parse_agent_usage`. The configured model is also pulled out of the role's `extra_args` (via `_configured_model`; recognises `-m <model>` / `-m=<model>` for codex and `--model <model>` / `--model=<model>` for claude) and forwarded as the parser's `fallback_model` so a codex run whose stdout includes usage frames but omits the model (resume frames, minimal completions) still records the configured model and -- when it matches a priced family -- an estimated `cost_usd` rather than `unknown-price`. A stream-reported model always wins over the fallback. Prompts, raw stdout / stderr, secrets, and worktree contents are deliberately NOT stored — the sink is a usage / cost surface, not a debugging mirror. A parser exception or sink IO failure is swallowed so an analytics misconfiguration cannot stop the per-issue tick from advancing.

## Analytics database (`analytics-db/`)

Local Postgres service that is the aggregation target for the JSONL sink above. The service contract and schema are operator-deployed via Docker compose; the JSONL→Postgres replay is implemented in `orchestrator/analytics/sync.py` and is operator-driven (a standalone CLI), NOT wired into the polling tick — the orchestrator's correctness must not depend on database availability.

**Service layout.** [`../analytics-db/compose.yml`](../analytics-db/compose.yml) brings up a single `postgres:16` container with the data directory on a host bind (`./data`, gitignored) and the init directory mounted read-only. The port binding is pinned to `127.0.0.1` so the database is unreachable off-host regardless of firewall configuration; re-binding to `0.0.0.0` is intentionally a code change rather than an env-var change, so a permissive `.env` cannot accidentally expose the database. The credentials default to `orchestrator` / `orchestrator` and are overridable via `analytics-db/.env` (`POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_PORT`). `docker compose` reads `.env` from the compose-file directory, not the orchestrator root, so compose-only knobs stay isolated from the orchestrator's env surface.

**Endpoint shape.** The sync reads a single libpq URL — `ANALYTICS_DB_URL` (default unset, example `postgresql://orchestrator:orchestrator@127.0.0.1:5432/orchestrator_analytics`) — rather than separate host / port / user / password variables. The endpoint-shaped contract means moving the database off-host later (managed Postgres, a different VM, a unix socket) is a one-line repoint rather than a code change; the compose service can be stopped entirely once the orchestrator points at a remote database. Empty value and the sentinels `off` / `disabled` / `none` (case-insensitive) disable the sync the same way `ANALYTICS_LOG_PATH` does, so the two can be turned off in parallel.

**Schema.** [`../analytics-db/init/01-schema.sql`](../analytics-db/init/01-schema.sql) defines one `analytics_events` table whose columns mirror the JSONL record shape produced by `analytics.build_record`. The common keys (`ts`, `repo`, `issue`, `event`) are `NOT NULL`; everything else is nullable so any single record across the three event kinds (`stage_enter`, `stage_evaluation`, `agent_exit`) is a valid row. An `extras JSONB` column captures any field added to `build_record` before the DDL knows about it, so a JSONL record from a newer orchestrator version can be ingested without losing data; promoted-to-column fields should be removed from `extras` by the ingest job when it learns about them. `source_path` / `source_line` are forensic context (which JSONL file and 1-indexed line number the row came from); the authoritative dedup key is `content_hash` -- SHA-256 over the canonical (`sort_keys=True`) JSON form of the record. A plain (non-partial) unique index on `content_hash` plus `INSERT ... ON CONFLICT (content_hash) DO NOTHING` is what makes repeated sync runs idempotent, and the dedup key remains stable across `analytics.prune_old_records` rewrites that shift `source_line` values. The index is deliberately non-partial because Postgres requires a partial index's predicate to be repeated in the `ON CONFLICT` target; keeping the index plain lets the sync's `ON CONFLICT (content_hash)` arbiter resolve without leaking the predicate into application SQL, and migration safety is unaffected because Postgres treats NULLs as distinct in a unique index so pre-`content_hash` rows from an older schema coexist. Indexes cover the expected query dimensions (`ts`, `(event, ts)`, `(repo, issue)`, and a partial index on non-null `stage`). The init script is run by the postgres image once when the data volume is empty (`/docker-entrypoint-initdb.d`); `IF NOT EXISTS` guards plus trailing `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `CREATE UNIQUE INDEX IF NOT EXISTS` for `content_hash` keep it idempotent for the operator-driven case (`psql -f` against an existing instance) and for migrating a pre-`content_hash` data volume without dropping data.

**Sync (`orchestrator/analytics/sync.py`).** Reads `ANALYTICS_LOG_PATH` line by line and inserts each record with `INSERT ... ON CONFLICT (content_hash) DO NOTHING`. Tolerance for malformed lines matches `prune_old_records`: blank lines are silently skipped; lines that are not valid JSON, JSON that is not an object, and records missing one of the required (`ts` / `repo` / `issue` / `event`) keys or carrying an unparseable `ts` are counted as skipped and logged but never abort the sync. The JSONL file is treated as read-only -- the sync never rewrites or truncates it, even when it sees malformed lines (operator cleanup is the same as for `prune_old_records`). Naive timestamps are interpreted as UTC, mirroring the prune helper. A `psycopg` driver-level error mid-stream rolls the whole transaction back and propagates so the CLI exits non-zero rather than reporting "success" on a half-inserted batch.

`sync_jsonl_to_postgres` is a no-op (no connection attempt, no row insertion, no error) when `ANALYTICS_DB_URL` is unset or disabled, when `ANALYTICS_LOG_PATH` is explicitly disabled (note that `ANALYTICS_LOG_PATH` defaults to `LOG_DIR/analytics.jsonl`, so only the empty value or `off` / `disabled` / `none` turns the sink off — leaving it untouched keeps it on), or when the JSONL file is absent on disk, so the CLI is safe to schedule before the operator deploys Postgres. The driver is `psycopg[binary]` (pinned in `pyproject.toml`); the import is lazy inside the connect helper so the module load path remains driver-free for callers that only need `SyncResult` or want to inject a fake `connect` factory in tests.

**Operator workflow.** Run `uv run python -m orchestrator.analytics.sync` on whatever cadence the operator prefers; `--log-path` and `--db-url` override the env values for one-off replays of archived JSONL files. The default cadence is operator-chosen because the JSONL sink is already the authoritative analytics surface on disk -- the database is for aggregation and reporting, not durability.

**Read model (`orchestrator/analytics/read.py`).** A thin, testable data-access layer over the populated `analytics_events` table that exposes plain-Python functions for the shapes the dashboard needs: `get_filter_options` (distinct repos / events / stages / backends / agent_roles for dropdowns), `get_summary` (date-bounded totals plus per-event / per-stage breakdowns and token / cost sums — `distinct_issues` is computed as `COUNT(DISTINCT (repo, issue))` so a multi-repo window does not collapse same-numbered issues across repos into one), `get_time_series` (daily `(day, event, count)` rollups), `get_stage_breakdown` and `get_event_breakdown` (per-stage and per-event counts; the stage version also averages `duration_s`), `get_recent_agent_exits` (the newest rows filtered to `event='agent_exit'`), `get_issues` (date / repo-bounded one-row-per-`(repo, issue)` overview with event count, first / last activity, the most recent non-null stage as a current-status hint, agent-exit count, and rolled-up cost / token totals; sorted by `last_seen DESC` and bounded by a `limit`), and `get_issue_events` (full event trace for a single `(repo, issue)` pair, oldest first). Each function returns a frozen dataclass or list of dataclasses; the module is intentionally Streamlit-free so the read path can be wired into any UI without an import-time dependency on the web layer. `ANALYTICS_DB_URL` being unset short-circuits every function to an empty / zero-valued result with no connection attempt, mirroring the sync's no-op contract so a dashboard process can boot before the operator has deployed Postgres. Connection or query failures (driver-level psycopg errors, schema mismatches, network unreachable) are wrapped in a single `AnalyticsReadError` whose `__cause__` preserves the underlying exception, so callers have one type to catch without re-exporting psycopg's exception hierarchy. The psycopg import is deferred to call time inside `_default_connect`, mirroring `analytics.sync` so the module load path stays driver-free; tests inject a fake `connect(db_url) -> connection` factory and never touch the real driver. One connection is opened per function call and always closed in a `finally`; close-time exceptions are logged and swallowed so a clean read does not surface a teardown failure to the dashboard. The read model is deliberately separate from `analytics/sync.py`: the sync owns the JSONL → Postgres write path (rollback, content-hash dedup, JSON adaptation), while reads have a different error story and a different injection shape, so keeping them apart means a dashboard never imports ingest code and the sync never grows query helpers.

**Dashboard (`orchestrator/dashboard.py`).** Streamlit app over the read model. Sidebar controls cover the date window, repo, event set, stage set, and an issue number; the body renders high-level metrics (events / distinct issues / repos / cost / tokens), a daily time-series bar chart, side-by-side stage / event breakdowns, the recent agent-run table (token / cost / exit columns), the date-bounded issues overview, and a per-issue event drill-down when a number is entered. Every filter is threaded through the read model's SQL via `_build_window_where`, so the overview metrics, time-series chart, breakdowns, recent-runs table, issues overview, and drill-down all narrow together rather than diverging by widget. The `_build_window_where` contract distinguishes three cases for the event / stage selections: `None` is "no filter on this column", a non-empty sequence emits a parameterised `IN (...)`, and an empty sequence emits a tautologically-false predicate (`FALSE`) so the dashboard's cleared multiselect produces an empty result instead of silently reverting to "show everything". The dashboard maps the event multiselect straight through to that contract — `event` is `NOT NULL` in the schema, so passing the full default selection is loss-free. The stage multiselect is asymmetric and routes through `resolve_stage_filter(selected, available)` because `options.stages` only lists the *non-null* stages the DB has seen: the all-selected default (and the no-stage-options case) collapses to `None` so NULL-stage rows are included, an explicitly cleared selection still emits `[]` (FALSE predicate), and a proper subset passes through verbatim. Without this asymmetry the default dashboard would silently exclude `stage_evaluation` rows on issues with no workflow label. The issue number acts as a SQL-level filter when a specific repo is selected (narrowing every widget to that one issue) AND triggers the drill-down section; with the repo filter on "All", it stays inert (GitHub issue numbers are not unique across repos) and the drill-down renders an instructive notice. `get_recent_agent_exits` accepts the same `start` / `end` / `events` / `stages` / `issue` shape so the recent-runs table moves with the date window; deselecting `agent_exit` from the events multiselect short-circuits that widget to empty without a DB round trip. Streamlit (and its transitive pandas) are imported *lazily* inside `main()` so the polling tick's `orchestrator.*` import surface stays free of the dashboard's footprint — `streamlit run orchestrator/dashboard.py` (or a direct `main()` call) is the only path that materializes the imports, and the module itself loads without `streamlit` installed (a regression-guard test in `tests/test_dashboard.py` asserts the invariant). `db_unconfigured_message()` collapses unset `ANALYTICS_DB_URL` (and the disable sentinels) to a single banner that short-circuits the app with `st.warning` + `st.stop`, and `analytics.read.AnalyticsReadError` raised from any of the read calls surfaces as `st.error` + `st.stop` so a misconfigured operator sees an actionable message instead of a stack trace. Run via `uv sync --group dashboard` then `uv run streamlit run orchestrator/dashboard.py`; the dashboard process is independent of the polling tick (no shared state, no GitHub access, read-only Postgres) so it can run on the same host or off-host without coupling.

## Usage parser (`orchestrator/usage.py`)

Pure-Python helpers that decode the JSONL stdout `agents.AgentResult` carries into a `UsageMetrics` dataclass — backend, distinct model(s), turn count, input / output / cached / cache-read / cache-write token totals, `cost_usd`, and a `cost_source` tag of `reported` / `estimated` / `unknown-price` / `no-usage`. No external dependency: the parser is jq-free so the orchestrator does not inherit the shell-reference's runtime requirement on a jq binary.

**Two parsers, one dispatcher.** `parse_claude_usage(stdout)` consumes claude `--output-format stream-json` events, groups assistant frames by `message.id` so the final-frame usage wins (claude streams partial counts on intermediate frames), and sums per-model. `parse_codex_usage(stdout, fallback_model=None)` consumes codex `--json` events and treats usage as cumulative across the session: the *last* non-zero usage record is the authoritative total rather than a sum of per-event deltas. `parse_agent_usage(backend, stdout, fallback_model=None)` dispatches by backend string the same way `agents.run_agent` does, so callers can pass the configured backend straight through.

**Cost precedence.** A `total_cost_usd` reported by the CLI itself always wins (`cost_source="reported"`); otherwise the parser walks first-party Anthropic / OpenAI price tables baked into the module and produces an estimate (`"estimated"`). When usage is present but the model SKU does not match any priced family, the parser returns `cost_source="unknown-price"` and `cost_usd=None` rather than guess at zero or bill cached tokens at the input rate. An empty stream — or one with no usage frames at all — yields `"no-usage"`.

**Resilience.** Malformed JSON lines (banner text, truncated frames, partial flushes) are silently skipped, mirroring the shell reference's `fromjson?` tolerance, so a single bad line never invalidates the rest of the stream. `workflow._run_agent_tracked` calls `parse_agent_usage` after every tracked agent run and appends the parsed counts to the [analytics sink](#analytics-sink-analytics_log_path) under `event="agent_exit"`; a parser exception is caught and downgraded to a `log.exception` so a flaky backend stream cannot break the per-issue tick.

## Summary of "what runs when"

| Component | Type | Trigger | Cadence |
|---|---|---|---|
| `main` polling loop | long-lived Python process | manual start (or wrapper) | every `POLL_INTERVAL`s |
| `workflow.tick(gh, spec)` | function call | each loop iteration | once per tick **per configured `RepoSpec`**, fanned out across a `ThreadPoolExecutor` (one worker thread per repo) when N>1; single-repo legacy mode collapses to N=1 and stays in-thread |
| `_refresh_base_and_worktrees(gh, spec)` | function call | start of each `workflow.tick` | once per tick per repo: one `git fetch <spec.remote_name> <spec.base_branch>` (remote defaults to `origin`, overridable per `REPOS` entry), then per-worktree dispatch (pre-PR worktrees rebase directly; PR-having worktrees behind base detour to `resolving_conflict`). See [Per-tick flow](state-machine.md#per-tick-flow-workflowtick) for the full open-PR / `awaiting_human` / watermark / conflict / dirty-tree rules. |
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
| `analytics.prune_with_retention_logging` | function call | end of each `main._run_tick` after every configured repo drains | once per tick (process-wide, not per-repo); wraps `analytics.prune_old_records` so the exception swallow and the "removed N record(s)" log message live in the analytics package; no-op when the sink is disabled or `ANALYTICS_RETENTION_DAYS <= 0` |
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
   │       fan-out (ready/implementing/documenting/validating/in_review/ │
   │                fixing/resolving_conflict) → up to spec.parallel_limit│
   │         worker threads, each with its own gh._for_worker_thread()    │
   │     every _process_issue call acquires global_semaphore, so total    │
   │     in-flight handlers across all repos ≤ MAX_PARALLEL_ISSUES_GLOBAL │
   │   → for each issue → dispatch by label:                              │
   │     (per-label handler tree, dispositions, and parking flow live in │
   │      docs/state-machine.md#stage-handlers; the compact label-       │
   │      lifecycle reference is at                                       │
   │      docs/state-machine.md#state-transition-label-lifecycle)         │
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
| **main.py** | polling loop + signal handling + self-restart + per-tick `analytics.prune_with_retention_logging` retention pass |
| **workflow.py** | facade: per-repo tick loop, family-aware/fan-out partitioning, `_process_issue` dispatcher, `_handle_pickup`, `_park_awaiting_human`, `_run_agent_tracked`; re-exports the cross-module helpers and stage entry handlers (`_comment_created_at` is re-exported because the `fixing` handler reuses it; other stage-private helpers stay private to their module) |
| **workflow_drift.py** | user-content drift detection and re-route helpers |
| **workflow_messages.py** | prompt builders, parsers, comment posting + orchestrator-comment markers, stderr redaction |
| **worktrees.py** | git/branch/worktree plumbing, hardened fetch/push, squash-on-approval, per-tick base refresh, terminal cleanup |
| **stages/decomposition.py** | `_handle_decomposing` / `_handle_ready` / `_handle_blocked` / `_handle_umbrella` |
| **stages/implementing.py** | `_handle_implementing` + developer-session lifecycle (relabels to `documenting` after PR opens) |
| **stages/documenting.py** | `_handle_documenting` — docs pass on the existing PR worktree (fetch + ahead/behind guard, dirty-check before any outcome, advance to `validating` after push or explicit no-change verdict) |
| **stages/validating.py** | `_handle_validating` + reviewer-session lifecycle |
| **stages/in_review.py** | `_handle_in_review` + PR-watermark / auto-merge primitives; routes fresh PR feedback to `fixing` |
| **stages/fixing.py** | `_handle_fixing` — PR-feedback quiet window, dev resume via `_resume_dev_with_text`, watermark advance, and route through `documenting` on a pushed fix (the no-new-feedback bounce flips directly to `validating`) |
| **stages/conflicts.py** | `_handle_resolving_conflict` + rebase-loop primitives |
| **stages/question.py** | `_handle_question` + question-session lifecycle (read-only Q&A on the `question` label, no PR) |
| **agents.py** | dispatch + spawn codex/claude subprocess, capture session id + last message |
| **github.py** | issues, comments, labels, pinned state, PR open/comment |
| **config.py** | env + token loading (token kept outside REPO_ROOT), backend validation |
| **codex / claude** | the only things that write code; run in isolated worktree |

### State transition (label lifecycle)

The compact label-lifecycle diagram for every forward, fix-loop, terminal, and HITL-park transition lives in [`state-machine.md#state-transition-label-lifecycle`](state-machine.md#state-transition-label-lifecycle).
