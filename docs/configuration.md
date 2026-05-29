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
| `DECOMPOSE_AGENT`    | `claude`               | decomposer command spec (validated even when `DECOMPOSE=off`). Also drives the `question` stage; see [`workflow.md#question-stage--read-only-qa-on-the-question-label`](workflow.md#question-stage--read-only-qa-on-the-question-label) |
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

## Local verification gate

When the reviewer agent emits `VERDICT: APPROVED`, `_handle_validating` runs the configured `VERIFY_COMMANDS` in the per-issue worktree **before** posting the approval comment, squashing, seeding watermarks, or relabeling to `in_review`. A clean run advances the issue as usual; any failure parks the issue on `validating` with `awaiting_human=True` and a typed `park_reason`, so an operator can fix the breakage and resume.

The verify gate is the **first** gate after the reviewer agent. GitHub CI remains the later auto-merge gate consulted by `_handle_in_review` — the verify gate does not replace it, it catches regressions locally so an obviously-broken branch never reaches the PR-side merge path.

The verify shell shares the agent's environment filter (`agents._filter_agent_env`, called with `allow_provider_auth=False`): GitHub-token aliases (`GITHUB_TOKEN`, `GH_TOKEN`, …), production-secret-shaped vars (anything matching `*_TOKEN` / `*_KEY` / `*_SECRET` / `*_PASSWORD` / `*_PAT` / `*_CREDENTIAL`, plus the bare names `TOKEN` / `KEY` / `SECRET` / `PASSWORD` / `PAT` / `CREDENTIAL`), credential-file locators (`*_TOKEN_FILE`, `*_KEY_FILE`, `*_SECRET_FILE`, `*_PASSWORD_FILE`, `*_CREDENTIAL_FILE`, `*_CREDENTIALS`, `*_CREDENTIALS_FILE`, plus bare `TOKEN_FILE` / `CREDENTIALS` / `CREDENTIALS_FILE`), write-credential locators (`SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_ASKPASS`, `GIT_SSH_COMMAND` — these aren't secret-shaped but let the subprocess push or authenticate as the operator), AND the agent's own provider-auth keys (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`) are **not** inherited from the orchestrator process. The provider-key strip is stricter than the agent-subprocess case: the agent CLI needs its provider key to reach its model, but a verify command runs operator-configured shell against agent-produced code, and a hostile dependency reading `$ANTHROPIC_API_KEY` would gain billable access to the operator's model account. The locator strip explicitly covers `ORCHESTRATOR_TOKEN_FILE`, `GOOGLE_APPLICATION_CREDENTIALS`, and `AWS_SHARED_CREDENTIALS_FILE` — the verify shell runs as the same OS user, so leaving the pointer in env would let a hostile dependency `cat` the target file.

**Do not embed secret literals in `VERIFY_COMMANDS`.** Verify failures park `awaiting_human` with the offending command string published *verbatim* in the GitHub issue comment (`_park_verify_failure` quotes `verify.command` so the operator can triage), so an inline `ANTHROPIC_API_KEY=sk-… pytest` entry would leak the literal secret to GitHub on the first failure. If a verify command legitimately needs a secret-shaped var (advanced provider auth, a service-account key for an integration test, …), load it from disk inside a wrapper script and reference the script from `VERIFY_COMMANDS` — `VERIFY_COMMANDS=./scripts/run-verify.sh` where the script reads the value from a file outside the worktree (`~/.config/<provider>/key`) and exports it before running tests. The script path is what gets published on failure, not the secret value.

| Variable          | Default | Purpose                                                                                                                |
| ----------------- | ------- | ---------------------------------------------------------------------------------------------------------------------- |
| `VERIFY_COMMANDS` | _(empty — no verification)_ | Ordered shell commands run sequentially in the per-issue worktree on `VERDICT: APPROVED`. Entries are separated by `;` or newlines; blank lines and `#`-comment lines are skipped. Each entry runs via the shell so quoting, pipes, and `&&` work; stdout and stderr are merged into one captured block. Default empty preserves the legacy behavior (no local verification — go straight to `in_review`). |
| `VERIFY_TIMEOUT`  | `600`   | Per-command wall-clock cap in seconds. A single slow command parks with `verify_timeout` instead of burning the orchestrator's tick budget. Ignored when `VERIFY_COMMANDS` is empty.                                                                                                                                                                  |

### Failure modes and `park_reason` tokens

The park comment names the failing command, its exit code (or timeout), and a redacted / truncated tail (last 4096 bytes) of the captured output. `park_reason` is set to one of these stable tokens so dashboards and recovery logic can branch on the failure mode:

| `park_reason`           | Trigger                                                                                                                                            |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `verify_failed`         | Command exited non-zero.                                                                                                                            |
| `verify_timeout`        | Command exceeded `VERIFY_TIMEOUT`.                                                                                                                  |
| `verify_dirty`          | Command exited 0 but left uncommitted changes in the worktree; handing off a dirty tree would let AUTO_MERGE land state the dev never committed.    |
| `verify_head_changed`   | Command exited 0 and the tree is clean but the command moved `HEAD` (e.g. ran `git commit` on its own). The subsequent squash + force-push would otherwise publish an unreviewed commit; the park comment surfaces the before / after SHAs so the operator can inspect, keep, or revert. |

Output is redacted via `_redact_secrets` **before** truncation so a secret straddling the cut cannot leak a partial value.

### Examples

```dotenv
# single command
VERIFY_COMMANDS=python3 -m pytest -q

# multiple commands (semicolon-separated because the .env loader cannot
# represent newlines inside a value)
VERIFY_COMMANDS=python3 -m pytest -q;ruff check .

# raise the per-command cap to 20 min for a slow test suite
VERIFY_TIMEOUT=1200
```

When exporting in a shell instead of `.env`, prefer one command per line — the parser accepts both `;` and newlines as separators.

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
- **Fan-out labels** (`ready`, `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, `question`) only touch their own per-issue state and worktree, so each one is submitted as its own future and runs concurrently up to `parallel_limit`.

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
| `IN_REVIEW_DEBOUNCE_SECONDS` | `600`   | quiet window the `fixing` stage honours before resuming the dev on PR feedback. Newer comments arriving while already labeled `fixing` reset the window; `_handle_in_review` itself routes fresh feedback to `fixing` immediately and does NOT apply the debounce |

`AUTO_MERGE=on` requires the `Checks: Read` permission on the PAT — without it the orchestrator sees `check_state='none'` for Actions-only PRs and parks awaiting a human even when CI is green.

## Observability

| Variable                   | Default                          | Purpose                                                                                                                       |
| -------------------------- | -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `EVENT_LOG_PATH`           | _(unset)_                        | optional JSONL audit sink; one event per line, no built-in rotation. See the [audit event log section in `architecture.md`](architecture.md#audit-event-log-event_log_path) for schema, event kinds, and the pinned-state-is-authoritative precedence rule. |
| `ANALYTICS_LOG_PATH`       | `LOG_DIR/analytics.jsonl`        | project-local analytics sink for raw metric records (`{ts, repo, issue, event, optional stage, ...}`). Records today: `stage_enter` (label transitions), `stage_evaluation` (per-dispatch timing with `duration_s` and `result=ok\|error`), and `agent_exit` (token / model / cost details). The raw JSONL is intended for later ingestion into a structured database; one record per line keeps that path streaming. Filesystem only — no PostgreSQL, Streamlit, or external services in-process. Set to `` (empty) or to `off` / `disabled` / `none` to disable writes entirely. See the [analytics sink section in `architecture.md`](architecture.md#analytics-sink-analytics_log_path) for the per-event schema and prune semantics. |
| `ANALYTICS_RETENTION_DAYS` | `90`                             | retention window for `ANALYTICS_LOG_PATH`. The polling loop calls `analytics.prune_old_records(...)` once per tick to remove records whose `ts` is older than this window without touching pinned GitHub state. Set to `0` (or any non-positive value) to keep raw data indefinitely — the prune helper becomes a no-op. |
| `ANALYTICS_DB_URL`         | _(unset)_                        | libpq connection string for the analytics Postgres service defined in [`../analytics-db/compose.yml`](../analytics-db/compose.yml). Consumed by the operator-driven CLI `python -m orchestrator.analytics_sync`, which replays records from `ANALYTICS_LOG_PATH` into the database with `INSERT ... ON CONFLICT (content_hash) DO NOTHING` so repeated runs are idempotent. NOT read by the polling loop — orchestrator correctness does not depend on database availability. Empty value and the sentinels `off` / `disabled` / `none` (case-insensitive) disable the sync, matching `ANALYTICS_LOG_PATH`'s disable knob. See the [analytics database section in `architecture.md`](architecture.md#analytics-database-analytics-db) for the service contract, schema, malformed-line tolerance, and operator workflow. |

### Local analytics database

`analytics-db/compose.yml` runs a single Postgres 16 container on the orchestrator host as the aggregation target for the JSONL sink. The port is bound to `127.0.0.1` and credentials default to `orchestrator` / `orchestrator`; override `POSTGRES_PASSWORD` (and any other field) via `analytics-db/.env` before exposing the port off-host or storing real data — `docker compose` reads `.env` from the compose-file directory, not the orchestrator root. The endpoint is deliberately shaped as a single libpq URL (`ANALYTICS_DB_URL`) so moving the database to a remote managed Postgres later is a one-line config change.

```sh
cd analytics-db
docker compose up -d                  # start the local service (data lives in ./data, gitignored)
docker compose down                   # stop the container; data on the ./data bind mount is preserved
docker compose down && rm -rf ./data  # stop and wipe history (the ./data bind is a host directory, not a docker volume, so `down -v` does NOT remove it)
```

The init script at [`../analytics-db/init/01-schema.sql`](../analytics-db/init/01-schema.sql) runs once when the data volume is empty. It is idempotent (`CREATE TABLE / INDEX IF NOT EXISTS` plus trailing `ALTER TABLE ADD COLUMN IF NOT EXISTS` / `CREATE UNIQUE INDEX IF NOT EXISTS` for `content_hash`), so re-running against an existing instance via `psql -f` is safe — and an instance created before the `content_hash` column existed picks up the new dedup key without dropping the data volume.

To apply or re-apply the schema against an already-running compose service:

```sh
cd analytics-db
docker compose exec -T analytics-db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/01-schema.sql'
```

Run the sync on demand:

```sh
uv run python -m orchestrator.analytics_sync               # uses the configured env vars
uv run python -m orchestrator.analytics_sync --log-path /path/to/rotated.jsonl --db-url postgresql://other/db
```

The sync inserts each record with `INSERT ... ON CONFLICT (content_hash) DO NOTHING`, so repeated runs are idempotent — even after `analytics.prune_old_records` rewrites the JSONL file and shifts source-line numbering. Malformed lines (blank, non-JSON, non-object, or missing the required `ts` / `repo` / `issue` / `event` keys) are logged and counted but never abort the sync; the JSONL file is treated as read-only. A driver-level error mid-stream rolls the transaction back and propagates, so the CLI exits non-zero rather than reporting "success" on a half-inserted batch. With either env var unset (or the JSONL file absent) the sync is a no-op — no connection is attempted, so the CLI is safe to schedule before the Postgres service is deployed.

## Continuous integration

[`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs `ruff check` and `pytest` on Python 3.12 for every push to `main` and every pull request. CI installs from the committed [`../uv.lock`](../uv.lock) via `uv sync --locked`, so the exact runtime and dev versions are reproducible. Lint rules are configured in [`../pyproject.toml`](../pyproject.toml) under `[tool.ruff.lint]`; dev tools (`pytest`, `ruff`) are declared in its `[dependency-groups]` table.

The workflow declares `permissions: contents: read` at the top level so the `GITHUB_TOKEN` minted for each run is read-only and cannot publish artifacts, push tags, or comment on PRs. The job uses no repository secrets, so PRs from forks run safely under the same scope.

[`../.github/dependabot.yml`](../.github/dependabot.yml) opens weekly update PRs for the `github-actions` and `uv` (Python `pyproject.toml` + `uv.lock`) ecosystems. [`../.github/workflows/dependency-review.yml`](../.github/workflows/dependency-review.yml) runs `actions/dependency-review-action` on every pull request and fails the check when a PR introduces a vulnerable or non-compliant dependency.

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

## Applying `.env` changes

`.env` is read once, when `python -m orchestrator.main` starts. The orchestrator process never reloads it, so most edits take effect on the **next fresh Python start** — there is no signal you can send to a running process to make it re-read configuration. `run.sh` is the usual restart mechanism: each loop iteration launches a new Python process (and `git pull --ff-only`s the orchestrator checkout to `ORCHESTRATOR_BASE_BRANCH` along the way, see [Run modes](#run-modes)).

### What survives a restart

Per-issue progress lives in the issue's pinned JSON comment on GitHub and in the per-issue worktree on disk. Restarting between ticks loses nothing — the next tick picks each issue back up from its label and pinned state. Two restart-time hazards are worth knowing:

- **A live `codex` / `claude` child.** Stage handlers spawn agent subprocesses that may run for as long as `AGENT_TIMEOUT`. Killing the orchestrator while a child is mid-session also kills the child, which can leave the issue parked on `awaiting_human`, routed through timeout recovery on the next tick, or sitting on a dirty worktree that needs manual cleanup.
- **In-flight agent spec is pinned.** When a `codex` / `claude` session starts, the orchestrator writes the full `DEV_AGENT` / `DECOMPOSE_AGENT` spec into pinned state and re-parses it (not the current `.env`) on every resume. Flipping `DEV_AGENT` or `DECOMPOSE_AGENT` after a session is locked does nothing for that issue until it reaches `done` or `rejected`. The same applies to the `question` stage, which seeds from `DECOMPOSE_AGENT` on the first spawn and pins to `question_agent` for the rest of the Q&A. `REVIEW_AGENT` is not pinned — the reviewer spawns fresh each round, so a new value applies on the next reviewer spawn after restart.

### Safe restart guidance

- **Idle / between ticks — safe.** Restart freely; the next tick resumes from GitHub state.
- **Issue mid-stage with no agent child — generally safe.** Workflow state is on GitHub and in the worktree, so the next tick resumes from the same label and pinned state.
- **Live `codex` / `claude` child — avoid.** Wait for the agent to exit. Forcing a restart here can park the issue or leave a dirty worktree behind.

Useful inspection commands before restarting:

```sh
pgrep -af 'python -m orchestrator.main|codex|claude|run.sh'
tail -f logs/orchestrator.log
journalctl --user -u agent.service -f   # systemd users
```

If `pgrep` lists a `codex` or `claude` process under the orchestrator, an agent session is live — wait it out unless you are deliberately discarding that work.

### Per launch style

**Foreground terminal (`./run.sh` in a shell).**

1. Edit `.env`.
2. Confirm no agent child is running (`pgrep -af 'codex|claude'`).
3. Ctrl+C the terminal. `run.sh` exits with code 130 and skips the restart loop.
4. Re-run `./run.sh`.

A second Ctrl+C while `run.sh` is mid-shutdown terminates immediately.

**`tmux` / `screen` session.**

1. Attach (`tmux attach -t orchestrator`, or `screen -r`).
2. Check the live output for an in-flight stage handler; cross-check with `pgrep -af 'codex|claude'` from another shell.
3. At a safe point, Ctrl+C the orchestrator and re-run `./run.sh` inside the session.
4. Detach (Ctrl+B then D for tmux, Ctrl+A then D for screen).

The session keeps its shell environment, so any `GITHUB_TOKEN` exported there persists across the restart.

**systemd user service.**

1. Edit `.env` in the orchestrator's working directory (the unit's `WorkingDirectory=`).
2. **Skip `systemctl --user daemon-reload`** unless the `.service` unit file itself changed — `daemon-reload` reloads unit definitions, not the orchestrator's `.env`, so running it is a no-op for config edits.
3. When safe (no live agent child), restart the service:
   ```sh
   systemctl --user restart agent.service
   ```
4. Tail logs to confirm the new process started cleanly:
   ```sh
   journalctl --user -u agent.service -f
   ```

When `GITHUB_TOKEN` is supplied via the unit's `EnvironmentFile=` directive, edit that file and restart the service — systemd reads the file's contents at service start, so no `daemon-reload` is needed. When the token is hard-coded in an inline `Environment=` line in the unit (or a drop-in), changing the value requires editing the unit *and then* a `daemon-reload` before the restart, because systemd only re-reads unit directives when unit definitions are reloaded.

**Direct `python -m orchestrator.main --once`.**

Each `--once` invocation is a fresh Python process and reads the current `.env` on every call. There is no long-running process to restart — edit `.env` and rerun the command.

### Setting-by-setting expectations

| Setting | When the change takes effect |
| ------- | ---------------------------- |
| `POLL_INTERVAL`, `AGENT_TIMEOUT`, `REVIEW_TIMEOUT`, `MAX_REVIEW_ROUNDS`, `MAX_CONFLICT_ROUNDS`, `MAX_RETRIES_PER_DAY`, `AUTO_MERGE`, `IN_REVIEW_DEBOUNCE_SECONDS`, `DECOMPOSE`, `VERIFY_COMMANDS`, `VERIFY_TIMEOUT`, `EVENT_LOG_PATH`, `ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`, `REPO` / `REPOS` / `TARGET_REPO_ROOT` / `BASE_BRANCH` / `REMOTE_NAME`, `HITL_HANDLE`, `ALLOWED_ISSUE_AUTHORS` | next Python start |
| `ANALYTICS_DB_URL` | next `python -m orchestrator.analytics_sync` invocation. The polling loop does not read this setting, so changing it does not require restarting the long-running orchestrator |
| `MAX_PARALLEL_ISSUES_PER_REPO`, `MAX_PARALLEL_ISSUES_GLOBAL` | next Python start. Per-`REPOS` `parallel_limit` overrides take precedence over `MAX_PARALLEL_ISSUES_PER_REPO`, so editing the default only affects entries that omit the fifth field |
| `DEV_AGENT`, `DECOMPOSE_AGENT` | next Python start, **except** for issues whose pinned state already names a `dev_agent` / `decomposer_agent` / `question_agent` — those keep the pinned spec until the issue reaches `done` or `rejected` (`DECOMPOSE_AGENT` also seeds the question stage on first spawn) |
| `REVIEW_AGENT` | next reviewer spawn after the next Python start (not pinned per issue) |
| `GITHUB_TOKEN` | not loaded from `.env`. Update the process environment (foreground/tmux: re-export before relaunch; systemd `EnvironmentFile=`: edit the file and restart the service; inline systemd `Environment=`: edit the unit, `daemon-reload`, then restart) or rewrite the file at `ORCHESTRATOR_TOKEN_FILE` (default `~/.config/<owner>/<repo>/token`) before the next start |
| `ORCHESTRATOR_BASE_BRANCH` | `run.sh` captures this once before its restart loop, so editing it only takes effect after `run.sh` itself is restarted (Ctrl+C the wrapper or `systemctl --user restart` the service, then relaunch). The Python process picks it up on the same next start |

## Control labels

| Label | Purpose |
| ----- | ------- |
| `hold_base_sync` | Apply to an issue to pause per-tick base merges, `in_review` auto-merge/unmergeable handling, and `resolving_conflict` base merges. Remove it when prerequisite PRs have landed; the next tick performs the accumulated base sync once. |
| `backlog` | Apply to an issue (typically at creation) to keep the orchestrator from picking it up. The dispatcher skips the issue entirely while the label is present; remove the label to release the issue for processing. |
