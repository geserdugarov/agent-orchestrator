# Architecture

Single-process **polling orchestrator** that drives GitHub issues through a label-based state machine, delegating coding
work to a configurable coding-agent CLI (`codex` or `claude`) running as a subprocess in isolated git worktrees.

State lives in GitHub: a workflow label exposes the current stage and a pinned JSON comment holds per-issue durable
state. The orchestrator process is stateless and can restart at any time.

This file covers the high-level system: design constraints, the module map, the process model, the agent subprocess
shape, the push path, and the observability surfaces. The label set, per-stage internals, per-tick flow, and
pinned-state schema live in [`state-machine.md`](state-machine.md); agent roles and command-spec semantics live in
[`workflow.md`](workflow.md).

## Design constraints

GitHub Issues are the orchestrator's task tracker and durable state surface. The process intentionally avoids an
internal database: workflow labels expose the current stage, and the pinned JSON comment holds the per-issue state that
the next tick needs. This keeps progress visible to humans on github.com and lets the process restart without
reconstructing hidden local state.

The orchestrator is not fully autonomous. When a stage hits uncertainty, an unsafe repository state, a malformed agent
response, or an exhausted retry cap, it parks with `awaiting_human` and mentions `HITL_HANDLE`; a later human issue
comment is the resume signal for the parked agent session.

The workflow is deliberately fixed instead of planner-selected: decomposition, implementation, validation, and
acceptance are mandatory phases. Routing is explicit and label-driven.

Agents run on the host as CLI subprocesses with broad local permissions
(`codex --dangerously-bypass-approvals-and-sandbox`, `claude --dangerously-skip-permissions`). The host, container, or
VM around the orchestrator is therefore the real sandbox boundary; token handling and hardened git operations are
designed around that assumption.

## Top-level layout

The workflow, worktree, analytics-read, and dashboard subsystems expose stable lazy facades backed by immutable export
manifests. Their implementations live in responsibility-named private leaves, while facade lookups preserve every
historical import and object identity. Leaves call through the owning facade at runtime where patch interception is
part of the compatibility contract, so `patch.object(workflow, "<helper>", ...)` still intercepts calls made from
other workflow and stage leaves.

```
orchestrator/
  __init__.py           lazy package/version compatibility surface;
  _package_exports.py   owns root-package export resolution and caching
  cli.py                `agent-orchestrator` console-script entry point,
                        delegating to the `main.py` runtime
  __main__.py           `python -m orchestrator` launch form over `cli.main`
  main.py               stable entry-point and test-patch facade
  _main_*.py            CLI/setup, tick fan-out, loop/drain, logging,
                        self-update probes, and shutdown/watchdog leaves
  config/
    __init__.py         stable configuration surface; binds each resolver
                        result as a module attribute (reload / patch target)
    environment.py      env-value parsers plus the `_SettingsResolver` that
                        reads/validates every knob into a resolved mapping
    _dotenv.py          non-secret `.env` loader
    credentials.py      process/token-file GitHub credential resolution
    models.py           `RepoSpec` / `RepoEnvEntry` repository-config types
    repositories.py     REPOS entry parsing, validation, and default-spec
                        construction
  state_machine.py      stable typed-label and transition-guard surface
  _workflow_labels.py   label enums and strict label-name coercion
  _state_transitions.py declared workflow transition graph
  github.py             stable PyGithub client and compatibility surface
  _github_*.py          labels, queries, pinned state, issues, PRs, reviews,
                        checks, feedback, events, and composed client mixins
  agents/
    __init__.py         stable runner API plus process-termination re-export
    models.py           agent result / run-option / subprocess-result models
    environment.py      credential filtering plus injected git identity
    sessions.py         session-id and Claude final-message JSONL parsing
    processes.py        shared process registry and subprocess-group lifecycle
    runner.py           shared agent dispatch, result assembly, spawn logging
    backends/           per-backend command leaves
      codex.py          Codex command construction, scratch output, execution
  _agent_claude.py      Claude command construction and execution
  _agent_api.py         façade backend-re-export compatibility inventory
  scheduler.py          stable `IssueScheduler` / `SubmissionRequest` surface
  _scheduler_*.py       typed legacy-call binding, scheduler views,
                        reservation, execution, and completion handling
  workflow.py           lazy compatibility facade for tick, dispatch, shared
                        helpers, and stage-handler patch points
  _workflow_export_manifest.py / _workflow_exports.py
                        immutable historical inventory and lazy resolver hooks
  _workflow_dependencies.py
                        import-time config/analytics bindings shared by leaves
  _workflow_*.py        tick/scheduling, dispatch, pickup, terminal routing,
                        prompts, comments, usage, and run-guard leaves
  workflow_drift.py     lazy user-content-drift compatibility facade
  _workflow_drift_*.py  drift hashing and stage-route leaves
  workflow_messages.py  lazy prompt/parser/comment compatibility facade
  _workflow_messages_*.py
                        prompt, parser, redaction, and comment leaves
  comment_trust.py      shared trust helpers (is_trusted_author /
                        filter_trusted) gating comment authors on the
                        ALLOWED_ISSUE_AUTHORS allowlist
  git_plumbing.py       lazy hardened-git compatibility facade
  _git_*.py             immutable command fragments, target-root lock registry,
                        auth, fetch, command, and push leaves
  verify.py             lazy local-verification compatibility facade
  _verify_*.py          verify models, subprocess execution, and probes
  worktree_lifecycle.py lazy naming/creation/cleanup compatibility facade
  _worktree_*.py        paths, creation, recovery, and cleanup leaves
  branch_publication.py lazy branch-publication compatibility facade
  _branch_*.py          probes, squash planning, rewriting, and publication
  base_sync.py          lazy base-refresh/rebase compatibility facade
  _base_sync_*.py       refresh, typed rebase/recovery decisions, conflict
                        routing, persistence, and publication leaves
  worktrees.py          lazy compatibility hub over the five worktree
                        subsystem facades above
  _worktrees_export_manifest.py / _worktrees_exports.py
                        immutable public inventory and lazy resolver hooks
  analytics/
    __init__.py         import-only package compatibility facade and sink bootstrap
    _package_*.py       package initialization, immutable inventory, and hooks
    read.py             lazy read-model compatibility facade with a `.pyi` surface
    _read_*.py          query-family implementations, typed query rows, and hooks
    read_*.py           stable raw, rollup, dashboard, and model compatibility hubs
    read_request*.py    typed filters, connection inputs, options, and legacy binding
    _recording*.py      event-family recording, settings, usage, and JSONL persistence
    _retention*.py      retention scanning and atomic rewrite leaves
    sync.py / _sync_*.py
                        CLI, ingestion, row parsing/mapping, and database lifecycle
    _trajectories.py / _trajectory_*.py
                        trajectory serialization, sanitization, and persistence
  dashboard.py          lazy compatibility facade and direct Streamlit entrypoint
  dashboard_*.py        stable component, read, chart, state, and widget hubs
  _dashboard_*.py       bootstrap/hooks plus focused render, query, and chart leaves
  usage.py              stable usage, skill, and trajectory parser surface
  _usage_*.py           provider payload, pricing, skill, and trajectory leaves
  trajectory_reader.py  pure file-backed filter and summary read model
  _trajectory_*.py      record/view models, parsing, filtering, and file-read leaves
  trajectory_dashboard.py
                        lazy compatibility facade and direct Streamlit entrypoint
  _trajectory_dashboard_*.py
                        viewer bootstrap, page controls, rendering, and HTML leaves
  skill_catalog.py      per-tick repo skill-catalog collection: enumerate
                        SKILL.md definitions on the target base ref and
                        append one `repo_skill_catalog` analytics record;
                        plus the per-run `discover_local_skills` filesystem
                        scan and `discover_codex_tools` baseline that backfill
                        a codex trajectory's offered skills and tools
  _local_skills.py      per-run filesystem skill discovery and codex tool list
  stages/
    <stage>.py          lazy compatibility facade for each historical stage
    _<stage>_exports.py / _<stage>_export_manifest.py
                        stage-specific lazy hooks and complete inventories
    _decomposition_*.py decomposer runs/sessions, child routing, recovery,
                        cleanup, blocked parents, and umbrella handling
    _implementing_*.py  handler entry, sessions, typed resume, recovery,
                        publication, drift, and post-agent dispositions
    _documenting_*.py   preconditions, run, persistence, drift, and outcomes
    _validating_*.py    reviewer/verify flow, watermarks, approval, fixes,
                        drift, and awaiting-human routes
    _in_review_*.py     watermarks, fresh feedback, drift, and manual-merge tail
    _fixing_*.py        bookmarks, quiet-window feedback, resume, and routing
    _conflict_*.py      rebase guards/outcomes, resume, publish, and transitions
    _question_*.py      read-only session, run, outcomes, and handler routing
```

`workflow.py`, `worktrees.py`, `analytics.read`, and `dashboard.py` publish explicit sorted `__all__` inventories,
`.pyi` surfaces, and immutable target registries. Resolution is lazy and cached on the facade, but the resolved object
is the implementation object's exact identity. Existing direct imports, wildcard imports, and `patch.object` calls
therefore keep working. Patches that need to intercept base-sync or publication internals still target their owning
facade (`base_sync` or `branch_publication`), just as before the split. Config and analytics modules retain their
original import-time identity through `_workflow_dependencies.py`, so a diagnostic reload does not silently rebind
already-imported workflow leaves. The analytics package has its own import-only bootstrap so an explicit package reload
still reparses sink settings and keeps stale package holders isolated as before.

Stage-private helpers stay private to their stage facade (`_bump_in_review_watermarks`,
`_seed_legacy_in_review_watermarks`, `_emit_conflict_round_incremented`). Cross-stage helpers like `_comment_created_at`
are re-exported from the facade because more than one stage reaches for them.

## Workflow labels

An issue should have at most one workflow label at a time. The set is `decomposing`, `ready`, `blocked`, `umbrella`,
`implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, `question`, and the two
terminals `done` / `rejected`. The orchestrator also creates three non-workflow control labels: `backlog` and `paused`
each make per-tick handlers skip the issue entirely (`backlog` is a "not yet" hold on a fresh issue, `paused` freezes
an in-flight one), and `community_contribution` is applied by the per-tick open-PR sweep to PRs from non-bot authors
outside `ALLOWED_ISSUE_AUTHORS` so a human reviews them.

Label names are part of the public contract because live GitHub issues already carry them. For the meaning of each
label, the control-label semantics, and the per-stage transitions they trigger, see
[`state-machine.md#workflow-labels`](state-machine.md#workflow-labels).

## Process model

There is **only one long-lived process**: `python -m orchestrator.main`. It is wrapped by `run.sh` so the loop can
self-exit and be restarted with new code.

- **Trigger**: started manually (or by a wrapper). Optional `--once` for a single tick.
- **Tick cadence**: every `POLL_INTERVAL` seconds (default 60).
- **Self-restart guard** (`main._self_modifying_merge_happened`): each tick fetches `origin/<ORCHESTRATOR_BASE_BRANCH>`
  (default `main`); if it advanced past the process's startup SHA *and* the new commits touch `orchestrator/`, the loop
  exits 0 so the wrapper can re-exec the new code. The branch is decoupled from `BASE_BRANCH` so a target repo with a
  different default branch does not interfere with self-update detection.
- **Self-update resilience** (`run.sh self_update`): before each launch — at startup and after every
  self-modifying-merge restart — the wrapper fast-forwards the orchestrator checkout to
  `origin/<ORCHESTRATOR_BASE_BRANCH>`. It skips the pull and warns to stderr if a non-base branch is checked out, and
  warns and continues (rather than exiting) if the fast-forward fails (diverged base branch, rebase in progress, network
  error); either way it launches the existing working tree. A clean fast-forward still updates the tree before launch,
  so the self-modifying-merge flow keeps picking up new code. This is deliberate: under the production systemd unit
  (`Restart=always`) exiting on a self-update failure silently crash-loops the service with the orchestrator never
  running, so a stale-but-running process plus a journal warning is preferred — the warning is the operator's signal
  to restore the checkout.
- **Signals**: SIGINT/SIGTERM set a flag and call `scheduler.shutdown(wait=False)` synchronously so the submit path is
  closed mid-tick; the loop then stops at the next tick boundary and drains. The drain terminates in-flight agent and
  verify subprocess groups up front (`agents.terminate_all_running`) so a worker parked in a long agent / verify run
  unwinds in seconds instead of holding the process for up to `AGENT_TIMEOUT`. A daemon watchdog backstops the drain: if
  it overruns, the watchdog terminates those same groups and hard-exits (`os._exit(128+signum)`) so total signal→exit
  stays within `SHUTDOWN_GRACE_SECONDS` no matter what a thread is blocked on. A second Ctrl+C hits the re-armed kernel
  default handler and kills immediately.

The coding agent runs as a **transient child subprocess**, not a daemon — spawned per tick when work is needed.

## Per-tick flow (`workflow.tick`)

Each tick the polling loop fans `workflow.tick(gh, spec, scheduler=...)` out across **every configured repo** via
`main._run_tick`: single-repo deployments stay in-thread, multi-repo deployments use a `ThreadPoolExecutor` sized to the
repo count. A single long-lived `IssueScheduler` (global cap `MAX_PARALLEL_ISSUES_GLOBAL`, per-repo cap
`MAX_PARALLEL_ISSUES_PER_REPO`) is shared across all `tick` calls.

The dispatch loop classifies each issue as family-aware (`decomposing` / `blocked` / `umbrella` / unlabeled — parent
↔ child writes) or fan-out (everything else). Fan-out submits go one callable per issue. Every family-aware issue this
tick is folded into ONE bucket submit per repo that drains them sequentially on a single executor worker so a stale
child cannot starve the parent umbrella issue. When every family-aware issue in the bucket runs a no-agent handler
(`blocked` or `umbrella`), the bucket is cap-exempt and runs on a dedicated executor pool so a pure label / dep-graph
walk cannot be blocked by ordinary implementation work. A bucket containing `decomposing` or unlabeled pickup stays
cap-counted.

Per-issue durable state lives in a single **pinned comment** on the issue (`<!--orchestrator-state {...json...}-->`).
The orchestrator process is stateless; the label and the pinned JSON are the entire dispatch input.

For the full per-tick sequence (eligible-issue enumeration, family vs. fan-out partitioning, the pre-PR rebase /
PR-having clean-rebase + push (with `resolving_conflict` reached on actual rebase conflicts, plus the `fixing`
worktree-drift dead-lock breaker that hands a stuck validating-route transient fix-loop to `resolving_conflict` when the
worktree is behind base or carries an unpushed rebase), the `question` skip, the per-tick
external-merge sweeps, and the complete pinned-state JSON schema), see
[`state-machine.md#per-tick-flow-workflowtick`](state-machine.md#per-tick-flow-workflowtick).

## Stage handlers

Each workflow label dispatches to a `_handle_<label>` function. The handlers live under `orchestrator/stages/` (see the
module map above) and are re-exported from `workflow.py` so test patches against `workflow.<helper>` keep intercepting
calls from inside a stage handler.

Most stage handlers run the user-content drift hook (`_compute_user_content_hash` → `_detect_user_content_change`) so
an out-of-band human edit re-routes the issue back to `decomposing` (when no dev session exists yet), resumes the locked
dev session with the updated body (implementing, validating, in_review, resolving_conflict), or unwinds back to
`validating` without resuming dev (documenting). `_handle_fixing` and `_handle_question` deliberately skip the drift
hook — see [`state-machine.md#user-content-drift-detection`](state-machine.md#user-content-drift-detection) for the
per-handler routing.

For per-stage internal flow — pickup, drift handling, decomposing, ready, blocked, umbrella, implementing,
documenting, validating, in_review, fixing, resolving_conflict, question — see
[`state-machine.md#stage-handlers`](state-machine.md#stage-handlers).

## Agent subprocess (`agents.run_agent`)

`run_agent(backend, prompt, cwd, ...)` dispatches to the per-backend runner (`_run_codex` / `_run_claude`); `backend` is
one of `"codex"` / `"claude"` and is re-validated at call time so a misuse fails loudly. Both runners return a unified
`AgentResult(session_id, last_message, exit_code, timed_out, stdout, stderr, interrupted, usage)`. `interrupted`
(default `False`) flags a run the runner observed exiting on SIGTERM/SIGKILL — the shape the orchestrator's
shutdown sweep (`terminate_all_running`) produces when it kills an in-flight agent group — and is distinct
from `timed_out` (the orchestrator's own `AGENT_TIMEOUT` firing). `usage` (default `None`) is the parsed
`usage.UsageMetrics` `analytics.record_agent_exit` attaches during a tracked run so callers can read token /
cost metrics off the result without re-parsing stdout; it stays `None` for a result that never flowed through
`_run_agent_tracked` or whose usage parse failed (fail-open). The developer (implementing), reviewer
(validating), decomposer (decomposing), and question handlers consume it: `workflow._accumulate_issue_usage` folds
each run's `usage` into the per-issue `issue_agent_runs` / `issue_total_tokens` / `issue_total_cost_usd` /
`issue_cost_sources` counters on the pinned state
([`state-machine.md#pinned-state`](state-machine.md#pinned-state)); at each terminal (PR merge / reject, umbrella
close, closed question) `workflow._format_issue_usage_verdict` reads those counters back into one visible receipt
comment — the sole read-side consumer, and nothing gates on the figure. `CodexResult` is kept as a
transitional alias.

The role command specs (`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT`), their parsing, the durable per-issue session
lock, and the resume mechanic are documented in [`workflow.md`](workflow.md). What follows is the subprocess shape only.

- **Codex command**:
  `codex exec [-C cwd | resume <sid>] --dangerously-bypass-approvals-and-sandbox --json -o <tempfile> <prompt>`. The
  `-o` path is a per-spawn `tempfile.mkstemp` outside the worktree (so target repos without `.codex-*` in `.gitignore`
  don't see it as untracked); `last_message` is read from it and the tempfile is cleaned up on any exit path by a
  per-spawn context manager (`_codex_last_message_file`).
- **Claude command**:
  `claude -p --dangerously-skip-permissions --output-format stream-json --include-partial-messages --verbose <prompt>`
  (with `--resume <sid>` when resuming). `last_message` is parsed from the stream-json: prefers the terminal
  `{"type":"result","result":...}` event (honored regardless of how the run ended), falls back to the last
  `assistant`/`message` text content for schema-drift forward-compat. The fallback is gated to clean, completed runs
  (`exit_code == 0`, not timed out, not interrupted); an interrupted or non-zero run with no terminal `result` event
  exposes an empty `last_message` rather than a partial transcript chunk.
- **Input**: prompt string; optional resume session id; timeout (`AGENT_TIMEOUT` / `REVIEW_TIMEOUT`).
- **Output**: `AgentResult(...)`. `session_id` is harvested by walking the JSONL events for any UUID-shaped value at
  `session_id` / `conversation_id` / etc. (shared between both backends).
- **Timeout cleanup** (`processes.terminate_process_group`): on timeout expiry the runner SIGTERMs the agent's whole
  process group (every spawn uses `start_new_session=True`), waits for the leader, then — mirroring the shutdown sweep
  (`terminate_all_running`) — probes the group with `killpg(_, 0)` and SIGKILLs any surviving descendant. Without the
  probe a build grandchild the agent forked (Maven, gradle, a JVM test runner) could keep mutating the worktree after
  the timeout was recorded — the failure mode that stranded a late clean commit behind the implementing-stage
  `agent_timeout` park.

### Environment filtering (`agents._filter_agent_env`)

The agent subprocess env is filtered to keep host secrets and the orchestrator's own GitHub credentials out of agent
reach. The same filter runs for the verify-command runner (with `allow_provider_auth=False`, which also strips provider
keys).

- **GitHub-token-bearing env vars** are stripped (`GITHUB_TOKEN`, `GH_TOKEN`, etc. — the `_FORBIDDEN_AGENT_ENV`
  exact-match set) so a prompt-injected agent cannot push or call the GitHub API.
- **Production-secret-shaped env vars** are stripped by name shape: anything matching `_AGENT_SECRET_SUFFIXES`
  (`_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD`, `_PAT`, `_CREDENTIAL`) or the bare-name set (`TOKEN`, `KEY`, `SECRET`,
  `PASSWORD`, `PAT`, `CREDENTIAL`). Without this a `STRIPE_API_KEY` / `DATABASE_PASSWORD` set on the host would ride
  into a sandbox-bypassed agent or into the operator-configured verify shell.
- **Credential-file locators** are stripped too (`*_TOKEN_FILE`, `*_KEY_FILE`, `*_SECRET_FILE`, `*_PASSWORD_FILE`,
  `*_CREDENTIAL_FILE`, `*_CREDENTIALS`, `*_CREDENTIALS_FILE`, plus bare `TOKEN_FILE` / `CREDENTIALS` /
  `CREDENTIALS_FILE`). The most important case is `ORCHESTRATOR_TOKEN_FILE`, the orchestrator's own write-credential
  locator.
- **Write-credential locators** (`_AGENT_WRITE_CREDENTIAL_LOCATORS`: `SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_ASKPASS`,
  `GIT_SSH_COMMAND`) are stripped by exact name. The orchestrator's own push path constructs its own `GIT_ASKPASS`
  tempfile.
- **Provider auth** required to reach the agent's own model is allowlisted by exact name in
  `_AGENT_PROVIDER_AUTH_ALLOWLIST` (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
  `OPENAI_API_KEY`) for agent subprocesses only. The verify runner passes `allow_provider_auth=False` and strips them
  too — a verify shell executes untrusted agent-produced code, and the verify-failure park comment publishes the
  offending command verbatim. Advanced deployments (Bedrock, Vertex, custom proxies) extend the allowlist explicitly.
- **`GIT_AUTHOR_*` / `GIT_COMMITTER_*`** are injected from `AGENT_GIT_NAME` / `AGENT_GIT_EMAIL` (default
  `agent-orchestrator <agent-orchestrator@users.noreply.github.com>`) so agent commits are stamped with the
  orchestrator's identity regardless of the host's `~/.gitconfig`.

## Push path (`workflow._push_branch`)

The orchestrator (not the agent) pushes. The push is hardened against the agent-controlled worktree:

- Token delivered via `GIT_ASKPASS` tempfile, never argv.
- Detaches from `~/.gitconfig` and `/etc/gitconfig` (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`).
- Disables `core.hooksPath`, `credential.helper`, `core.fsmonitor`.
- Refuses to push if the config the push resolves — the worktree's local config plus any `include.path` file or
  per-worktree `config.worktree` it pulls in, with global/system detached — carries any `url.*.insteadOf` /
  `pushInsteadOf` rewrite or any `http.*` proxy/TLS setting (e.g. `http.proxy`, `http.sslVerify=false`) that could
  tunnel the token-bearing push through an attacker proxy or disable certificate verification. Env-var proxies
  (`https_proxy`) are operator-set and stay honored — only agent-writable config-file transport is rejected.
- Pushes via explicit refspec `HEAD:refs/heads/<branch>` (no upstream stored).

## Observability

Four independent observability surfaces — an opt-in audit event log, a project-local analytics JSONL sink, an opt-in
(default-off) trajectory JSONL sink that `record_agent_exit` fills with redacted, head/tail-truncated per-run reasoning
trajectories — each carrying a denormalized run-level token-usage / cost summary (plus a claude-only per-turn
breakdown) alongside the step timeline — and an operator-deployed Postgres aggregation target (with a Streamlit
dashboard and the `orchestrator/usage.py` parser that feeds it). The trajectory sink has its own separate Streamlit page
— the file-backed trajectory viewer (`orchestrator/trajectory_dashboard.py` over the pure
`orchestrator/trajectory_reader.py`), which reads the JSONL directly (usage and cost included) and needs no Postgres.
None of them feed back into dispatch: workflow correctness keys off the pinned state JSON and the workflow label, so
every surface is observation-only and safe to truncate, rotate, or delete.

For the per-sink schema, event-kind tables, append / retention / rotation semantics, the analytics-DB compose layout,
the sync / read-model / dashboard wiring, and the usage parser's cost-precedence rules, see
[`observability.md`](observability.md).

## Summary of "what runs when"

- **`main` polling loop** — long-lived Python process. Trigger: manual start (or wrapper). Cadence: every
  `POLL_INTERVAL`s.
- **`workflow.tick(gh, spec)`** — function call. Trigger: each loop iteration. Cadence: once per tick per configured
  `RepoSpec`; multi-repo fans out across a `ThreadPoolExecutor`, single-repo stays in-thread.
- **`_refresh_base_and_worktrees(gh, spec)`** — function call. Trigger: start of each `workflow.tick`. Cadence: once
  per tick per repo: one `git fetch <spec.remote_name> <spec.base_branch>`, then per-worktree dispatch (pre-PR worktrees
  rebase directly; PR-having worktrees behind base are rebased + pushed in the refresh itself via
  `_sync_pr_worktree_to_base` and routed to `validating` on success, with `resolving_conflict` reached when the auto
  rebase actually leaves conflicted files).
- **`_handle_*` per issue** — function call. Trigger: issue's workflow label. Cadence: once per tick per open issue;
  concurrent up to `spec.parallel_limit` per repo and `MAX_PARALLEL_ISSUES_GLOBAL` across all repos. No-agent family
  buckets (`blocked` / `umbrella`) are cap-exempt.
- **decomposer agent (`DECOMPOSE_AGENT`)** — subprocess (fresh or resumed). Trigger: `_handle_decomposing` (retry
  budget OK) or HITL resume. Cadence: one shot per tick when needed.
- **implementer agent (`DEV_AGENT`)** — subprocess. Trigger: `_handle_implementing` (no commits yet, retry budget OK)
  or HITL resume. Cadence: one shot per tick when needed.
- **reviewer agent (`REVIEW_AGENT`)** — subprocess (fresh session). Trigger: `_handle_validating`, round < max.
  Cadence: one shot per tick.
- **dev-fix agent** — subprocess (resumed dev session). Trigger: reviewer says CHANGES_REQUESTED (dispatched from
  `_handle_validating` after the relabel to `fixing`), or fresh in_review PR feedback (dispatched from `_handle_fixing`
  after the quiet window) — both run with `stage="fixing"` and bounce back to `validating` for re-review. Cadence: one
  shot per tick.
- **`_handle_resolving_conflict`** — function call. Trigger: issue label `resolving_conflict` (operator relabel,
  refresh-time conflicted rebase, or the `fixing` worktree-drift dead-lock breaker when a stuck validating-route
  transient fix-loop is out of sync with the PR head — behind base or an unpushed local rebase); also fires on
  closed-`resolving_conflict` issues from the polling sweep. Cadence: once per tick per such issue.
- **dev-conflict agent** — subprocess (resumed dev session). Trigger: `_handle_resolving_conflict` and `git rebase`
  left conflicts. Cadence: one shot per tick.
- **`_handle_question`** — function call. Trigger: issue label `question` OR closed-`question` issue from the polling
  sweep. Cadence: once per tick per such issue.
- **question agent (`DECOMPOSE_AGENT` backend)** — subprocess (read-only). Trigger: `_handle_question` (no prior
  session OR new human comment on a parked Q&A). Cadence: one shot per tick when needed.
- **`git push`** — subprocess. Trigger: after dev produces clean commits. Cadence: per fix.
- **self-restart check** — git fetch + diff. Trigger: start of each tick. Cadence: every tick.

## Architecture schema

```
                     ┌──────────────────────────────────────┐
                     │   GitHub repo(s) (REPO or REPOS)     │
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
   │     startup: build per-spec [(spec, GitHubClient), ...] from         │
   │              config.default_repo_specs(); ensure_workflow_labels;    │
   │              build one shared IssueScheduler(global_cap, per_repo)   │
   │     loop every POLL_INTERVAL s:                                      │
   │       1. self-restart check (origin/<ORCHESTRATOR_BASE_BRANCH>       │
   │          moved & touches orchestrator/?)                             │
   │       2. _run_tick(clients, scheduler):                              │
   │            N == 1 → in-thread workflow.tick(gh, spec, scheduler)     │
   │            N  > 1 → ThreadPoolExecutor fans workflow.tick across     │
   │                     one worker thread per repo                       │
   │       3. scheduler.reap()  (drain completions; surface failures)     │
   │       4. analytics.prune_with_retention_logging()                    │
   │     shutdown: scheduler.shutdown(wait=True) drains workers on        │
   │               --once / self-restart; a signal stop first kills       │
   │               in-flight agent+verify groups, and a watchdog          │
   │               hard-exits within SHUTDOWN_GRACE_SECONDS on overrun    │
   │                    │                                                 │
   │                    ▼                                                 │
   │   workflow.tick(gh, spec, scheduler) →                               │
   │     _refresh_base_and_worktrees(gh, spec, scheduler): skip           │
   │       worktrees whose handler is still in flight in scheduler        │
   │     classify each pollable issue and submit to scheduler:            │
   │       family-aware (decomposing/blocked/umbrella/unlabeled) →        │
   │         ONE bucket submit per repo that drains sequentially          │
   │         (cap-exempt when every family issue is `blocked` or          │
   │         `umbrella`)                                                  │
   │       fan-out (everything else) →                                    │
   │         one submit per issue, concurrent up to per-repo / global     │
   │         caps                                                         │
   │     scheduler rejects duplicate active / cap hit / family-slot       │
   │       conflict → skipped this tick AND logged with reason            │
   │     accepted workers call gh._for_worker_thread() + refetch the      │
   │       Issue, then run _process_issue → dispatch by label             │
   │                                                                      │
   └─────────┬───────────────────────────────────────┬────────────────────┘
             │ subprocess                            │ subprocess (hardened)
             ▼                                       ▼
   ┌─────────────────────────────┐         ┌─────────────────────────────┐
   │  coding-agent CLI           │         │  git push                   │
   │  (codex or claude,          │         │  ─ GIT_ASKPASS tempfile     │
   │   per-issue worktree)       │         │  ─ no global/system config  │
   │  ─ env: GH tokens stripped  │         │  ─ hooks/helper disabled    │
   │  ─ env: GIT_AUTHOR/COMMITTER│         │  ─ refuses url/http cfg     │
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
   │  branch:        orchestrator/<owner>__<name>/issue-<n>              │
   │  ─ slug subdir + slug-namespaced branch keep two repos sharing a    │
   │    target_root from colliding on the same `orchestrator/issue-<n>`  │
   │  ─ created from <spec.remote_name>/<spec.base_branch>               │
   │    in spec.target_root                                              │
   │    (or reused if has unpushed commits)                              │
   └─────────────────────────────────────────────────────────────────────┘
```

## State transition (label lifecycle)

The compact label-lifecycle diagram for every forward, fix-loop, terminal, and HITL-park transition lives in
[`state-machine.md#state-transition-label-lifecycle`](state-machine.md#state-transition-label-lifecycle).
