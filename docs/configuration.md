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

Each entry is `owner/name|target_root|base_branch`, with an optional fourth `|remote_name` field (defaults to `origin` when omitted):

```dotenv
REPOS=acme/api|/srv/clones/acme-api|main;acme/web|/srv/clones/acme-web|master|private
```

Validation happens at import — a malformed entry, empty owner/name, empty base branch, or a duplicate slug aborts startup with a clear error. A `target_root` that does not exist on disk warns to stderr but does not block startup. Each repo can have its own PAT at `~/.config/<owner>/<repo>/token`, or a single `GITHUB_TOKEN` covering every listed repo. Worktrees are namespaced `WORKTREES_DIR/<owner>__<name>/issue-N` so two repos with the same issue number cannot collide on disk.

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

- `./run.sh` — production. Continuous polling. `run.sh` does `git pull --ff-only origin "$ORCHESTRATOR_BASE_BRANCH"` (read from `.env`, default `main`) and re-launches the orchestrator after each clean exit, so a self-modifying merge picks up the new code automatically. Ctrl+C (or `SIGTERM`) stops the wrapper too: the orchestrator exits with `128 + signum` and `run.sh` skips the restart loop. A second Ctrl+C terminates immediately.
- `python -m orchestrator.main --once` — single tick then exit. Useful for tests and debugging.
- `python -m orchestrator.main --log-level DEBUG` — verbose logs.

On first start (any mode) the orchestrator creates the workflow labels on the repo and begins polling open issues every `POLL_INTERVAL` seconds.
