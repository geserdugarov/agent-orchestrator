# Configuration reference

All settings load from `.env` (or the process environment). [`../.env.example`](../.env.example) is the annotated source of truth — this page summarizes the same knobs grouped by topic and points at the deeper docs for the things that have one.

The orchestrator is deliberately stateless: every setting here either selects backends and budgets at startup, or names files/paths outside the repo. Per-issue state lives in the issue's pinned JSON comment on GitHub.

## Required

| Variable                  | Default                                       | Purpose                                                                 |
| ------------------------- | --------------------------------------------- | ----------------------------------------------------------------------- |
| `GITHUB_TOKEN`            | _(required, env-only — not read from `.env`)_ | fine-grained PAT. Putting it in `.env` is rejected at startup.          |
| `ORCHESTRATOR_TOKEN_FILE` | `~/.config/<owner>/<repo>/token` (from `REPO`) | path to the PAT file (used when `GITHUB_TOKEN` is not in env)          |
| `HITL_HANDLE`             | `geserdugarov`                                | comma-separated GitHub logins to @-mention when a human is needed      |

## Target repository

Use `REPO` for a single repo (the default), or `REPOS` to drive several from one process. When `REPOS` is set, the legacy single-repo quartet (`REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH` / `REMOTE_NAME`) is ignored.

| Variable           | Default                                       | Purpose                                                                 |
| ------------------ | --------------------------------------------- | ----------------------------------------------------------------------- |
| `REPO`             | `geserdugarov/agent-orchestrator`             | `owner/name` of the single repo to manage (ignored when `REPOS` is set) |
| `TARGET_REPO_ROOT` | `REPO_ROOT` (self-bootstrap)                  | path to the local clone of `REPO` — worktrees are `git worktree add`-ed from here |
| `BASE_BRANCH`      | `main`                                        | branch PRs target                                                       |
| `REMOTE_NAME`      | `origin`                                      | git remote in `TARGET_REPO_ROOT` that points at `REPO` on GitHub        |
| `REPOS`            | _(unset)_                                     | multi-repo configuration, entries separated by newlines or `;`          |

### Multi-repo `REPOS` syntax

Each entry is `owner/name|target_root|base_branch`, with two optional trailing fields:

- fourth `|remote_name` — defaults to `origin` when omitted;
- fifth `|parallel_limit` — defaults to `MAX_PARALLEL_ISSUES_PER_REPO` when omitted. Positional: to override `parallel_limit` you must also write the `remote_name` (use `origin` explicitly to keep the default).

```dotenv
REPOS=acme/api|/srv/clones/acme-api|main;acme/web|/srv/clones/acme-web|master|private|2
```

Validation happens at import — a malformed entry, empty owner/name, empty base branch, empty `remote_name`, a non-integer or non-positive `parallel_limit`, or a duplicate slug aborts startup with a clear error. A `target_root` that does not exist on disk warns to stderr but does not block startup.

Each repo can have its own PAT at `~/.config/<owner>/<repo>/token`, or a single `GITHUB_TOKEN` covering every listed repo. Worktrees are namespaced `WORKTREES_DIR/<owner>__<name>/issue-N` so two repos with the same issue number cannot collide on disk.

## Agent roles

The first token of each role spec selects the backend (`codex` / `claude`); any remaining tokens are forwarded as backend-CLI args (model, reasoning effort, etc.). See [`workflow.md`](workflow.md) for the spec format, in-flight session lock, and full examples.

| Variable             | Default                | Purpose                                                                                                 |
| -------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------- |
| `DEV_AGENT`          | `claude`               | implementer command spec                                                                                |
| `REVIEW_AGENT`       | `codex`                | reviewer command spec                                                                                   |
| `DECOMPOSE_AGENT`    | `claude`               | decomposer command spec (validated even when `DECOMPOSE=off`)                                          |
| `DECOMPOSE`          | `on`                   | enable the `decomposing` stage; `off` reverts to the legacy "no label → implementing" pickup           |
| `CODEX_BIN`          | `codex`                | executable launched when a role's first token is `codex`; override only if `codex` is not on `$PATH`   |
| `CLAUDE_BIN`         | `claude`               | executable launched when a role's first token is `claude`; override only if `claude` is not on `$PATH` |
| `ALLOWED_ISSUE_AUTHORS` | _(unset)_           | comma-separated GitHub logins; when set, the orchestrator only auto-picks-up unlabeled issues from those authors |

## Cadence and budgets

| Variable                   | Default     | Purpose                                                                                          |
| -------------------------- | ----------- | ------------------------------------------------------------------------------------------------ |
| `POLL_INTERVAL`            | `60`        | seconds between polling ticks                                                                    |
| `AGENT_TIMEOUT`            | `1800`      | wall-clock cap per agent invocation, seconds                                                     |
| `REVIEW_TIMEOUT`           | (= `AGENT_TIMEOUT`) | wall-clock cap per reviewer invocation, seconds                                          |
| `MAX_REVIEW_ROUNDS`        | `3`         | review/fix iterations before parking on `awaiting_human`                                         |
| `MAX_CONFLICT_ROUNDS`      | `3`         | auto-conflict-resolution rounds before parking on `awaiting_human`                               |
| `MAX_RETRIES_PER_DAY`      | `3`         | fresh implementer spawns per issue per 24h window (`0` = unbounded)                              |
| `ORCHESTRATOR_BASE_BRANCH` | `main`      | base branch of the orchestrator's own repo, used by the self-update path                          |

## Parallel processing

Each polling tick advances issues concurrently along two axes:

- **Across repos.** When `REPOS` lists more than one entry, `main._run_tick` fans the per-repo `workflow.tick(gh, spec)` calls out across a `ThreadPoolExecutor` (one worker thread per configured repo) so a slow repo cannot delay the others. The legacy single-repo mode (`REPOS` unset) stays in-thread, so deployments without `REPOS` see no behavior change.
- **Within a repo.** When `parallel_limit > 1` for a given repo, `workflow.tick` materializes its eligible-issue set and dispatches the per-issue handlers across a bounded `ThreadPoolExecutor` capped at `parallel_limit`. `parallel_limit == 1` (the default) keeps the legacy sequential, streaming loop with no executor.

The two caps below are the levers:

| Variable                       | Default | Purpose                                                                                              |
| ------------------------------ | ------- | ---------------------------------------------------------------------------------------------------- |
| `MAX_PARALLEL_ISSUES_PER_REPO` | `1`     | per-repo cap on concurrent in-flight per-issue handlers within one repo on a single tick. Default `1` keeps the legacy one-at-a-time behavior. Each `REPOS` entry can override this via its optional fifth pipe-separated field. Must be a positive integer. |
| `MAX_PARALLEL_ISSUES_GLOBAL`   | `3`     | global cap across all configured repos. Bounds the total concurrent agent fan-out regardless of any one repo's `parallel_limit`. Must be a positive integer; raise only on hosts with the CPU / memory headroom to run that many agent CLIs at once. |

`MAX_PARALLEL_ISSUES_GLOBAL` is enforced by a single `threading.BoundedSemaphore` built once at startup and threaded through every `workflow.tick(gh, spec, global_semaphore=...)` call. Each tick acquires it around every `_process_issue` invocation, so workers from different repos contend on the same semaphore — total in-flight per-issue handlers across all repos never exceeds the global cap regardless of how many `parallel_limit` slots each repo declares.

Inside a single `workflow.tick`, the parallel path partitions pollable issues by workflow label before submitting work to the executor:

- **Family-aware labels** (`decomposing`, `blocked`, `umbrella`, plus unlabeled issues) read and write cross-issue state (parent ↔ child) and must never run two at a time. They are folded into one drain task that processes them sequentially on a single worker thread.
- **Fan-out labels** (`ready`, `implementing`, `validating`, `in_review`, `resolving_conflict`) only touch their own per-issue state and worktree, so each one is submitted as its own future and runs concurrently up to `parallel_limit`.

The drain task occupies exactly one executor slot regardless of how many family-aware issues exist, leaving the other `parallel_limit - 1` slots free for fan-out work in the same tick.

Non-positive or non-integer values for either cap (or for a per-entry `parallel_limit`) abort startup with a clear error so a typo cannot silently disable all work.

## Workspace and agent identity

| Variable           | Default                                       | Purpose                                                                                                       |
| ------------------ | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `WORKTREES_DIR`    | `../wt-orchestrator`                          | where per-issue git worktrees are created; layout is `WORKTREES_DIR/<owner>__<name>/issue-N`                  |
| `AGENT_GIT_NAME`   | `agent-orchestrator`                          | `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` injected into agent spawns                                             |
| `AGENT_GIT_EMAIL`  | `agent-orchestrator@users.noreply.github.com` | `GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_EMAIL` injected into agent spawns                                           |

## Auto-merge

| Variable                     | Default | Purpose                                                                                              |
| ---------------------------- | ------- | ---------------------------------------------------------------------------------------------------- |
| `AUTO_MERGE`                 | `off`   | merge approved PRs (green CI + mergeable) from `in_review`; flip to `on` once dogfooded             |
| `IN_REVIEW_DEBOUNCE_SECONDS` | `600`   | quiet window after the latest PR/issue comment before resuming the dev session                       |

`AUTO_MERGE=on` requires the `Checks: Read` permission on the PAT — without it the orchestrator sees `check_state='none'` for Actions-only PRs and parks awaiting a human even when CI is green.

## Observability

| Variable          | Default     | Purpose                                                                                                                       |
| ----------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `EVENT_LOG_PATH`  | _(unset)_   | optional JSONL audit sink; one event per line, no built-in rotation. See the [audit event log section in `architecture.md`](architecture.md#audit-event-log-event_log_path) for schema, event kinds, and the pinned-state-is-authoritative precedence rule. |

## Run modes

- `./run.sh` — production. Continuous polling. `run.sh` does `git pull --ff-only origin "$ORCHESTRATOR_BASE_BRANCH"` (read from `.env`, default `main`) and re-launches the orchestrator after each clean exit, so a self-modifying merge picks up the new code automatically.

  Ctrl+C (or `SIGTERM`) stops the wrapper too: the orchestrator exits with `128 + signum` and `run.sh` skips the restart loop. A second Ctrl+C terminates immediately.
- `python -m orchestrator.main --once` — single tick then exit. Useful for tests and debugging.
- `python -m orchestrator.main --log-level DEBUG` — verbose logs.

On first start (any mode) the orchestrator creates the workflow labels and the `hold_base_sync` / `backlog` control labels on the repo, then begins polling open issues every `POLL_INTERVAL` seconds.

## Running under systemd (user service)

`run.sh` is meant to be a continuously-running process: it already restarts the orchestrator after self-modifying merges and after non-signal crashes. It does **not** survive a reboot, a `tty` logout, or the user manager being torn down, so the recommended production deployment is a systemd **user** service that supervises `run.sh` directly.

A detached `screen` / `tmux` session wrapped in a `Type=forking` unit (`ExecStart=screen -dmS agent run.sh`) looks similar but is the wrong shape: systemd ends up supervising `screen`, not the orchestrator; `ExecStop` races the screen session's own lifecycle; logs split across systemd, screen's scrollback, and `logs/orchestrator.log`; and the unit silently does nothing at boot unless linger is enabled. Keep `screen` / `tmux` for interactive debugging and let systemd supervise `run.sh` itself.

### Unit file

Drop this at `~/.config/systemd/user/agent.service`, replacing the working directory and the `PATH` entries with the values for your host:

```ini
[Unit]
Description=Agent orchestrator
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/agent-orchestrator
ExecStart=/path/to/agent-orchestrator/run.sh
Restart=always
RestartSec=5
Environment=PATH=/home/<user>/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
```

- `Type=simple` because `run.sh` stays in the foreground — systemd tracks the wrapper PID directly, and a `SIGTERM` from `systemctl stop` propagates to the wrapper, which then propagates to the orchestrator (exit `143`, no restart loop).
- `Restart=always` covers machine-level events (reboot, OOM, host crash). Application-level self-restart after a self-modifying merge is still handled inside `run.sh`, so the two layers do not fight.
- A non-interactive systemd service does not inherit your shell's `PATH`. If `codex` or `claude` lives under `~/.local/bin` (or any other shell-only path), add it to the `Environment=PATH=…` line, or set `CODEX_BIN` / `CLAUDE_BIN` to the absolute paths via additional `Environment=` lines. Without this the orchestrator will fail to spawn agents even though `run.sh` works fine in an interactive shell.

### Enabling

```sh
systemctl --user daemon-reload
systemctl --user enable --now agent.service
loginctl enable-linger <user>
```

`enable-linger` is **required for boot-time start**: without it the per-user systemd manager only runs while the user has an active login session, so the "enabled" service still waits for the next login before it starts. Linger keeps the user manager running across logouts and reboots.

### Operating

```sh
systemctl --user status agent.service        # current state and last log lines
systemctl --user restart agent.service       # bounce the orchestrator
systemctl --user stop agent.service          # SIGTERM the wrapper (exits 143, no restart)
journalctl --user-unit agent.service -f      # tail the wrapper's stdout/stderr
```

systemd's journal captures `run.sh` and orchestrator stdout/stderr (process lifecycle, exit codes, restart messages). The orchestrator's own structured log lives at `logs/orchestrator.log` under `WorkingDirectory` (rotated, ~10 MiB × 5). Check the journal first for "did it start / did it die", then `logs/orchestrator.log` for per-issue handler detail.

## Control labels

| Label | Purpose |
| ----- | ------- |
| `hold_base_sync` | Apply to an issue to pause per-tick base merges, `in_review` auto-merge/unmergeable handling, and `resolving_conflict` base merges. Remove it when prerequisite PRs have landed; the next tick performs the accumulated base sync once. |
| `backlog` | Apply to an issue (typically at creation) to keep the orchestrator from picking it up. The dispatcher skips the issue entirely while the label is present; remove the label to release the issue for processing. |
