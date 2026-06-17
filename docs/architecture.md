# Architecture

Single-process **polling orchestrator** that drives GitHub issues through a label-based state machine, delegating coding work to a configurable coding-agent CLI (`codex` or `claude`) running as a subprocess in isolated git worktrees.

State lives in GitHub: a workflow label exposes the current stage and a pinned JSON comment holds per-issue durable state. The orchestrator process is stateless and can restart at any time.

This file covers the high-level system: design constraints, the module map, the process model, the agent subprocess shape, the push path, and the observability surfaces. The label set, per-stage internals, per-tick flow, and pinned-state schema live in [`state-machine.md`](state-machine.md); agent roles and command-spec semantics live in [`workflow.md`](workflow.md).

## Design constraints

GitHub Issues are the orchestrator's task tracker and durable state surface. The process intentionally avoids an internal database: workflow labels expose the current stage, and the pinned JSON comment holds the per-issue state that the next tick needs. This keeps progress visible to humans on github.com and lets the process restart without reconstructing hidden local state.

The orchestrator is not fully autonomous. When a stage hits uncertainty, an unsafe repository state, a malformed agent response, or an exhausted retry cap, it parks with `awaiting_human` and mentions `HITL_HANDLE`; a later human issue comment is the resume signal for the parked agent session.

The workflow is deliberately fixed instead of planner-selected: decomposition, implementation, validation, and acceptance are mandatory phases. Routing is explicit and label-driven.

Agents run on the host as CLI subprocesses with broad local permissions (`codex --dangerously-bypass-approvals-and-sandbox`, `claude --dangerously-skip-permissions`). The host, container, or VM around the orchestrator is therefore the real sandbox boundary; token handling and hardened git operations are designed around that assumption.

## Top-level layout

The `orchestrator/` package is split between a slim facade (`workflow.py`), per-stage handler modules under `stages/`, and a small set of supporting modules. Stage modules call back into the facade via `from .. import workflow as _wf` at call time so test patches against `workflow.<helper>` still intercept calls made from inside a stage handler.

```
orchestrator/
  main.py               entry point, polling loop, self-restart guard
  config.py             env / token loading, secret handling, backend validation
  state_machine.py      typed label vocabulary, transition table, typo guard
                        and transition guard
  github.py             PyGithub wrapper, label bootstrap, pinned-state comment
  agents.py             coding-agent subprocess runner (codex/claude dispatch)
  scheduler.py          process-local IssueScheduler (global / per-repo caps,
                        duplicate-active gate, family-aware mutex, executor)
  workflow.py           per-repo tick loop, label dispatcher, pickup handler,
                        shared cross-stage helpers (park, finalize-on-merge,
                        finalize-on-close, drain-review-pr-terminals,
                        run-agent-tracked), re-exports of stage handlers and
                        cross-module helpers so existing test patches keep
                        working
  workflow_drift.py     user-content drift detection (hash, compute, route)
  workflow_messages.py  prompt builders, parsers, comment / marker helpers,
                        stderr redaction
  git_plumbing.py       hardened git subprocess layer: `_git` / `_git_hardened`,
                        per-target-root locks, authed fetch / push helpers
  verify.py             local-verify runner and worktree-state probes
  worktree_lifecycle.py worktree naming, layout, creation, restoration, cleanup
  branch_publication.py PR-branch publication helpers (conventional-subject
                        detection, ahead/behind probe, squash-and-force-push)
  base_sync.py          per-tick base refresh, PR-aware rebase + push, crash
                        recovery, and the conflict-only `resolving_conflict`
                        route (clean rebases route directly to `validating`)
  worktrees.py          compatibility re-export hub for the five worktree-
                        subsystem modules above
  stages/
    decomposition.py    decomposing / ready / blocked / umbrella handlers and
                        the decomposer-session lifecycle
    implementing.py     implementing handler and the developer-session
                        lifecycle (read / resume / retry budget / post-agent
                        dispositions)
    documenting.py      documenting handler — single docs pass on the existing
                        PR worktree, reached only via the final-docs handoff
    validating.py       validating handler and reviewer-session lifecycle,
                        plus the local-verify gate park helper
    in_review.py        in_review handler — manual-merge-only PR-watermark
                        primitives, fresh-feedback route to `fixing`, HITL
                        ping
    fixing.py           fixing handler — PR-feedback quiet window, dev resume,
                        hand-back-to-`validating`
    conflicts.py        resolving_conflict handler and the rebase / dev-resume
                        primitives
    question.py         question handler — read-only Q&A with no PR
```

`worktrees.py` is a compatibility re-export hub over the five focused modules above; every name is re-exported so existing imports and `patch.object(worktrees, "_foo", ...)` test patches keep working. Test patches that need to intercept a call from inside `_refresh_base_and_worktrees` / `_sync_worktree_with_base` / `_squash_and_force_push` / `_first_commit_subject` must target the owning module (`base_sync` / `branch_publication`) directly because the call graph lives there.

Stage-private helpers stay private to their stage module (`_bump_in_review_watermarks`, `_seed_legacy_in_review_watermarks`, `_emit_conflict_round_incremented`). Cross-stage helpers like `_comment_created_at` are re-exported from the facade because more than one stage reaches for them.

## Workflow labels

An issue should have at most one workflow label at a time. The set is `decomposing`, `ready`, `blocked`, `umbrella`, `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, `question`, and the two terminals `done` / `rejected`. The orchestrator also creates three non-workflow control labels: `hold_base_sync` pauses per-tick base sync and rebases while present, `backlog` makes per-tick handlers skip the issue entirely, and `community_contribution` is applied by the per-tick open-PR sweep to PRs from non-bot authors outside `ALLOWED_ISSUE_AUTHORS` so a human reviews them.

Label names are part of the public contract because live GitHub issues already carry them. For the meaning of each label, the control-label semantics, and the per-stage transitions they trigger, see [`state-machine.md#workflow-labels`](state-machine.md#workflow-labels).

## Process model

There is **only one long-lived process**: `python -m orchestrator.main`. It is wrapped by `run.sh` so the loop can self-exit and be restarted with new code.

- **Trigger**: started manually (or by a wrapper). Optional `--once` for a single tick.
- **Tick cadence**: every `POLL_INTERVAL` seconds (default 60).
- **Self-restart guard** (`main._self_modifying_merge_happened`): each tick fetches `origin/<ORCHESTRATOR_BASE_BRANCH>` (default `main`); if it advanced past the process's startup SHA *and* the new commits touch `orchestrator/`, the loop exits 0 so the wrapper can re-exec the new code. The branch is decoupled from `BASE_BRANCH` so a target repo with a different default branch does not interfere with self-update detection.
- **Signals**: SIGINT/SIGTERM set a flag; the current tick finishes, then the loop exits. The signal handler also calls `scheduler.shutdown(wait=False)` synchronously so the submit path is closed mid-tick.

The coding agent runs as a **transient child subprocess**, not a daemon — spawned per tick when work is needed.

## Per-tick flow (`workflow.tick`)

Each tick the polling loop fans `workflow.tick(gh, spec, scheduler=...)` out across **every configured repo** via `main._run_tick`: single-repo deployments stay in-thread, multi-repo deployments use a `ThreadPoolExecutor` sized to the repo count. A single long-lived `IssueScheduler` (global cap `MAX_PARALLEL_ISSUES_GLOBAL`, per-repo cap `MAX_PARALLEL_ISSUES_PER_REPO`) is shared across all `tick` calls.

The dispatch loop classifies each issue as family-aware (`decomposing` / `blocked` / `umbrella` / unlabeled — parent ↔ child writes) or fan-out (everything else). Fan-out submits go one callable per issue. Every family-aware issue this tick is folded into ONE bucket submit per repo that drains them sequentially on a single executor worker so a stale child cannot starve the parent umbrella issue. When every family-aware issue in the bucket runs a no-agent handler (`blocked` or `umbrella`), the bucket is cap-exempt and runs on a dedicated executor pool so a pure label / dep-graph walk cannot be blocked by ordinary implementation work. A bucket containing `decomposing` or unlabeled pickup stays cap-counted.

Per-issue durable state lives in a single **pinned comment** on the issue (`<!--orchestrator-state {...json...}-->`). The orchestrator process is stateless; the label and the pinned JSON are the entire dispatch input.

For the full per-tick sequence (eligible-issue enumeration, family vs. fan-out partitioning, the pre-PR rebase / PR-having clean-rebase + push (with `resolving_conflict` reached on actual rebase conflicts, plus the `fixing` worktree-drift dead-lock breaker that hands a stuck validating-route transient fix-loop to `resolving_conflict` when the worktree is behind base or carries an unpushed rebase), the `hold_base_sync` / `question` skips, the per-tick external-merge sweeps, and the complete pinned-state JSON schema), see [`state-machine.md#per-tick-flow-workflowtick`](state-machine.md#per-tick-flow-workflowtick).

## Stage handlers

Each workflow label dispatches to a `_handle_<label>` function. The handlers live under `orchestrator/stages/` (see the module map above) and are re-exported from `workflow.py` so test patches against `workflow.<helper>` keep intercepting calls from inside a stage handler.

Most stage handlers run the user-content drift hook (`_compute_user_content_hash` → `_detect_user_content_change`) so an out-of-band human edit re-routes the issue back to `decomposing` (when no dev session exists yet), resumes the locked dev session with the updated body (implementing, validating, in_review, resolving_conflict), or unwinds back to `validating` without resuming dev (documenting). `_handle_fixing` and `_handle_question` deliberately skip the drift hook — see [`state-machine.md#user-content-drift-detection`](state-machine.md#user-content-drift-detection) for the per-handler routing.

For per-stage internal flow — pickup, drift handling, decomposing, ready, blocked, umbrella, implementing, documenting, validating, in_review, fixing, resolving_conflict, question — see [`state-machine.md#stage-handlers`](state-machine.md#stage-handlers).

## Agent subprocess (`agents.run_agent`)

`run_agent(backend, prompt, cwd, ...)` dispatches to the per-backend runner (`_run_codex` / `_run_claude`); `backend` is one of `"codex"` / `"claude"` and is re-validated at call time so a misuse fails loudly. Both runners return a unified `AgentResult(session_id, last_message, exit_code, timed_out, stdout, stderr)`. `CodexResult` is kept as a transitional alias.

The role command specs (`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT`), their parsing, the durable per-issue session lock, and the resume mechanic are documented in [`workflow.md`](workflow.md). What follows is the subprocess shape only.

- **Codex command**: `codex exec [-C cwd | resume <sid>] --dangerously-bypass-approvals-and-sandbox --json -o <tempfile> <prompt>`. The `-o` path is a per-spawn `tempfile.mkstemp` outside the worktree (so target repos without `.codex-*` in `.gitignore` don't see it as untracked); `last_message` is read from it and the tempfile is unlinked in a `finally` block.
- **Claude command**: `claude -p --dangerously-skip-permissions --output-format stream-json --include-partial-messages --verbose <prompt>` (with `--resume <sid>` when resuming). `last_message` is parsed from the stream-json: prefers the terminal `{"type":"result","result":...}` event, falls back to the last `assistant`/`message` text content for schema-drift forward-compat.
- **Input**: prompt string; optional resume session id; timeout (`AGENT_TIMEOUT` / `REVIEW_TIMEOUT`).
- **Output**: `AgentResult(...)`. `session_id` is harvested by walking the JSONL events for any UUID-shaped value at `session_id` / `conversation_id` / etc. (shared between both backends).

### Environment filtering (`agents._filter_agent_env`)

The agent subprocess env is filtered to keep host secrets and the orchestrator's own GitHub credentials out of agent reach. The same filter runs for the verify-command runner (with `allow_provider_auth=False`, which also strips provider keys).

- **GitHub-token-bearing env vars** are stripped (`GITHUB_TOKEN`, `GH_TOKEN`, etc. — the `_FORBIDDEN_AGENT_ENV` exact-match set) so a prompt-injected agent cannot push or call the GitHub API.
- **Production-secret-shaped env vars** are stripped by name shape: anything matching `_AGENT_SECRET_SUFFIXES` (`_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD`, `_PAT`, `_CREDENTIAL`) or the bare-name set (`TOKEN`, `KEY`, `SECRET`, `PASSWORD`, `PAT`, `CREDENTIAL`). Without this a `STRIPE_API_KEY` / `DATABASE_PASSWORD` set on the host would ride into a sandbox-bypassed agent or into the operator-configured verify shell.
- **Credential-file locators** are stripped too (`*_TOKEN_FILE`, `*_KEY_FILE`, `*_SECRET_FILE`, `*_PASSWORD_FILE`, `*_CREDENTIAL_FILE`, `*_CREDENTIALS`, `*_CREDENTIALS_FILE`, plus bare `TOKEN_FILE` / `CREDENTIALS` / `CREDENTIALS_FILE`). The most important case is `ORCHESTRATOR_TOKEN_FILE`, the orchestrator's own write-credential locator.
- **Write-credential locators** (`_AGENT_WRITE_CREDENTIAL_LOCATORS`: `SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_ASKPASS`, `GIT_SSH_COMMAND`) are stripped by exact name. The orchestrator's own push path constructs its own `GIT_ASKPASS` tempfile.
- **Provider auth** required to reach the agent's own model is allowlisted by exact name in `_AGENT_PROVIDER_AUTH_ALLOWLIST` (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`) for agent subprocesses only. The verify runner passes `allow_provider_auth=False` and strips them too — a verify shell executes untrusted agent-produced code, and the verify-failure park comment publishes the offending command verbatim. Advanced deployments (Bedrock, Vertex, custom proxies) extend the allowlist explicitly.
- **`GIT_AUTHOR_*` / `GIT_COMMITTER_*`** are injected from `AGENT_GIT_NAME` / `AGENT_GIT_EMAIL` (default `agent-orchestrator <agent-orchestrator@users.noreply.github.com>`) so agent commits are stamped with the orchestrator's identity regardless of the host's `~/.gitconfig`.

## Push path (`workflow._push_branch`)

The orchestrator (not the agent) pushes. The push is hardened against the agent-controlled worktree:

- Token delivered via `GIT_ASKPASS` tempfile, never argv.
- Detaches from `~/.gitconfig` and `/etc/gitconfig` (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`).
- Disables `core.hooksPath`, `credential.helper`, `core.fsmonitor`.
- Refuses to push if the worktree's local config has any `url.*.insteadOf` / `pushInsteadOf` rewrite.
- Pushes via explicit refspec `HEAD:refs/heads/<branch>` (no upstream stored).

## Observability

Three independent observability surfaces — an opt-in audit event log, a project-local analytics JSONL sink, and an operator-deployed Postgres aggregation target (with a Streamlit dashboard and the `orchestrator/usage.py` parser that feeds it). None of them feed back into dispatch: workflow correctness keys off the pinned state JSON and the workflow label, so every surface is observation-only and safe to truncate, rotate, or delete.

For the per-sink schema, event-kind tables, append / retention / rotation semantics, the analytics-DB compose layout, the sync / read-model / dashboard wiring, and the usage parser's cost-precedence rules, see [`observability.md`](observability.md).

## Summary of "what runs when"

| Component | Type | Trigger | Cadence |
|---|---|---|---|
| `main` polling loop | long-lived Python process | manual start (or wrapper) | every `POLL_INTERVAL`s |
| `workflow.tick(gh, spec)` | function call | each loop iteration | once per tick per configured `RepoSpec`; multi-repo fans out across a `ThreadPoolExecutor`, single-repo stays in-thread |
| `_refresh_base_and_worktrees(gh, spec)` | function call | start of each `workflow.tick` | once per tick per repo: one `git fetch <spec.remote_name> <spec.base_branch>`, then per-worktree dispatch (pre-PR worktrees rebase directly; PR-having worktrees behind base are rebased + pushed in the refresh itself via `_sync_pr_worktree_to_base` and routed to `validating` on success, with `resolving_conflict` reached when the auto rebase actually leaves conflicted files) |
| `_handle_*` per issue | function call | issue's workflow label | once per tick per open issue; concurrent up to `spec.parallel_limit` per repo and `MAX_PARALLEL_ISSUES_GLOBAL` across all repos. No-agent family buckets (`blocked` / `umbrella`) are cap-exempt |
| decomposer agent (`DECOMPOSE_AGENT`) | subprocess (fresh or resumed) | `_handle_decomposing` (retry budget OK) or HITL resume | one shot per tick when needed |
| implementer agent (`DEV_AGENT`) | subprocess | `_handle_implementing` (no commits yet, retry budget OK) or HITL resume | one shot per tick when needed |
| reviewer agent (`REVIEW_AGENT`) | subprocess (fresh session) | `_handle_validating`, round < max | one shot per tick |
| dev-fix agent | subprocess (resumed dev session) | reviewer says CHANGES_REQUESTED (dispatched from `_handle_validating` after the relabel to `fixing`), or fresh in_review PR feedback (dispatched from `_handle_fixing` after the quiet window) — both run with `stage="fixing"` and bounce back to `validating` for re-review | one shot per tick |
| `_handle_resolving_conflict` | function call | issue label `resolving_conflict` (operator relabel, refresh-time conflicted rebase, or the `fixing` worktree-drift dead-lock breaker when a stuck validating-route transient fix-loop is out of sync with the PR head — behind base or an unpushed local rebase); also fires on closed-`resolving_conflict` issues from the polling sweep | once per tick per such issue |
| dev-conflict agent | subprocess (resumed dev session) | `_handle_resolving_conflict` and `git rebase` left conflicts | one shot per tick |
| `_handle_question` | function call | issue label `question` OR closed-`question` issue from the polling sweep | once per tick per such issue |
| question agent (`DECOMPOSE_AGENT` backend) | subprocess (read-only) | `_handle_question` (no prior session OR new human comment on a parked Q&A) | one shot per tick when needed |
| `git push` | subprocess | after dev produces clean commits | per fix |
| self-restart check | git fetch + diff | start of each tick | every tick |

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
   │     shutdown: scheduler.shutdown(wait=True) so in-flight workers     │
   │               complete cleanly on exit (signal / --once / restart)   │
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
   │  branch:        orchestrator/<owner>__<name>/issue-<n>              │
   │  ─ slug subdir + slug-namespaced branch keep two repos sharing a    │
   │    target_root from colliding on the same `orchestrator/issue-<n>`  │
   │  ─ created from <spec.remote_name>/<spec.base_branch>               │
   │    in spec.target_root                                              │
   │    (or reused if has unpushed commits)                              │
   └─────────────────────────────────────────────────────────────────────┘
```

## State transition (label lifecycle)

The compact label-lifecycle diagram for every forward, fix-loop, terminal, and HITL-park transition lives in [`state-machine.md#state-transition-label-lifecycle`](state-machine.md#state-transition-label-lifecycle).
