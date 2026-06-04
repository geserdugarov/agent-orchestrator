# Configuration reference

All settings load from `.env` (or the process environment). [`../.env.example`](../.env.example) holds the basic parameters needed for a first run; [`../.env.example.advanced`](../.env.example.advanced) carries common advanced overrides and illustrative examples for opt-in settings. This page is the source of truth — every setting and every default lives here, and both `.env.example*` files keep their inline comments terse and link back for the full rationale.

The orchestrator is deliberately stateless: every setting here selects backends and budgets at startup, or names files/paths outside the repo. Per-issue state lives in the issue's pinned JSON comment on GitHub.

## Required

| Variable                  | Default                                       | Purpose                                                                 |
| ------------------------- | --------------------------------------------- | ----------------------------------------------------------------------- |
| `GITHUB_TOKEN`            | _(required, env-only — not read from `.env`)_ | fine-grained PAT. Putting it in `.env` is rejected at startup.          |
| `ORCHESTRATOR_TOKEN_FILE` | `~/.config/<owner>/<repo>/token` (from `REPO`) | path to the PAT file (used when `GITHUB_TOKEN` is not in env)          |
| `HITL_HANDLE`             | `geserdugarov`                                | comma-separated GitHub logins to @-mention when a human is needed      |

### GitHub PAT

`GITHUB_TOKEN` is the fine-grained PAT the orchestrator uses for every GitHub call. Required scopes on the target repository:

- **Contents** — read/write (worktree branches and squash commits)
- **Issues** — read/write (label transitions, pinned-state comments, `HITL_HANDLE` @-mentions)
- **Pull requests** — read/write (opening PRs and posting PR comments; the orchestrator never merges PRs)
- **Metadata** — read-only (issue / PR enumeration)

Create the PAT at <https://github.com/settings/personal-access-tokens>.

The token is deliberately NOT loaded from `.env`. The implementer agent runs in a sibling worktree with sandbox bypass, so anything readable inside `REPO_ROOT` (including `.env`) is recoverable by a prompt-injected agent via a relative-path read like `cat ../agent-orchestrator/.env`. `GITHUB_TOKEN` (and the aliases `GH_TOKEN`, `GITHUB_PAT`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, `GIT_TOKEN`) found in `.env` is logged-and-skipped at startup.

Token resolution order:

1. `GITHUB_TOKEN` exported in the orchestrator's launch environment.
2. The file at `~/.config/<owner>/<repo>/token` — path derived from `REPO`, override with `ORCHESTRATOR_TOKEN_FILE`. Pick a path the agent worktree cannot reach via known relatives, and `chmod 600` it.

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

- fourth `|remote_name` — defaults to `origin`;
- fifth `|parallel_limit` — defaults to `MAX_PARALLEL_ISSUES_PER_REPO`. Positional: to override `parallel_limit` you must also write the `remote_name` (use `origin` explicitly to keep the default).

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
| `DECOMPOSE_AGENT`    | `claude`               | decomposer command spec (validated even when `DECOMPOSE=off`); also drives the `question` stage         |
| `DECOMPOSE`          | `on`                   | enable the `decomposing` stage; `off` reverts to the legacy "no label → implementing" pickup           |
| `CODEX_BIN`          | `codex`                | executable launched when a role's first token is `codex`; override only if `codex` is not on `$PATH`   |
| `CLAUDE_BIN`         | `claude`               | executable launched when a role's first token is `claude`; override only if `claude` is not on `$PATH` |
| `ALLOWED_ISSUE_AUTHORS` | _(unset)_           | comma-separated GitHub logins; when set, only auto-pick-up unlabeled issues from those authors          |

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
| `SQUASH_ON_APPROVAL`       | `on`        | after the reviewer emits `VERDICT: APPROVED`, squash the dev's commits on the PR branch into a single conventional-commit-shaped commit and force-push with lease. `off` leaves the per-step commit history intact (useful when downstream tooling depends on it). Parsed as a boolean: `1` / `true` / `on` / `yes` enable, anything else disables. |

## Local verification gate

When the reviewer agent emits `VERDICT: APPROVED`, `_handle_validating` runs the configured `VERIFY_COMMANDS` in the per-issue worktree **before** posting the approval comment, squashing, seeding watermarks, or relabeling to `documenting`. A clean run advances the issue as usual; any failure parks the issue on `validating` with `awaiting_human=True` and a typed `park_reason`, so an operator can fix the breakage and resume.

The verify gate is the first gate after the reviewer agent — it catches regressions locally so an obviously-broken branch never reaches `in_review`. GitHub CI still runs against the PR; the human merging the PR is the consumer of CI's verdict, since the orchestrator never merges from `in_review` itself.

### Secret stripping

The verify shell shares the agent's environment filter (`agents._filter_agent_env`, called with `allow_provider_auth=False`). Stripped from the verify environment:

- GitHub-token aliases (`GITHUB_TOKEN`, `GH_TOKEN`, …).
- Secret-shaped vars: anything matching `*_TOKEN` / `*_KEY` / `*_SECRET` / `*_PASSWORD` / `*_PAT` / `*_CREDENTIAL`, plus the bare names `TOKEN` / `KEY` / `SECRET` / `PASSWORD` / `PAT` / `CREDENTIAL`.
- Credential-file locators: `*_TOKEN_FILE`, `*_KEY_FILE`, `*_SECRET_FILE`, `*_PASSWORD_FILE`, `*_CREDENTIAL_FILE`, `*_CREDENTIALS`, `*_CREDENTIALS_FILE`, plus bare `TOKEN_FILE` / `CREDENTIALS` / `CREDENTIALS_FILE`. Explicitly covers `ORCHESTRATOR_TOKEN_FILE`, `GOOGLE_APPLICATION_CREDENTIALS`, `AWS_SHARED_CREDENTIALS_FILE`.
- Write-credential locators: `SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_ASKPASS`, `GIT_SSH_COMMAND`.
- The agent's own provider-auth keys: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY` (stricter than the agent-subprocess case — a verify command runs operator-configured shell against agent-produced code, and a hostile dependency reading `$ANTHROPIC_API_KEY` would gain billable access to the operator's model account).

**Do not embed secret literals in `VERIFY_COMMANDS`.** Verify failures park `awaiting_human` with the offending command string published *verbatim* in the GitHub issue comment, so an inline `ANTHROPIC_API_KEY=sk-… pytest` entry would leak the literal secret on the first failure. If a verify command legitimately needs a secret-shaped var, load it from disk inside a wrapper script and reference the script from `VERIFY_COMMANDS` — `VERIFY_COMMANDS=./scripts/run-verify.sh` where the script reads the value from a file outside the worktree (`~/.config/<provider>/key`) and exports it before running tests.

### Settings

| Variable          | Default | Purpose                                                                                                                |
| ----------------- | ------- | ---------------------------------------------------------------------------------------------------------------------- |
| `VERIFY_COMMANDS` | _(empty — no verification)_ | Ordered shell commands run sequentially in the per-issue worktree on `VERDICT: APPROVED`. Entries are separated by `;` or newlines; blank lines and `#`-comment lines are skipped. Each entry runs via the shell so quoting, pipes, and `&&` work; stdout and stderr are merged into one captured block. |
| `VERIFY_TIMEOUT`  | `600`   | Per-command wall-clock cap in seconds. A single slow command parks with `verify_timeout`. Ignored when `VERIFY_COMMANDS` is empty. |

### Failure modes and `park_reason` tokens

The park comment names the failing command, its exit code (or timeout), and a redacted / truncated tail (last 4096 bytes) of the captured output. Output is redacted via `_redact_secrets` **before** truncation so a secret straddling the cut cannot leak a partial value. `park_reason` is set to one of:

| `park_reason`           | Trigger                                                                                                                                            |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `verify_failed`         | Command exited non-zero.                                                                                                                            |
| `verify_timeout`        | Command exceeded `VERIFY_TIMEOUT`.                                                                                                                  |
| `verify_dirty`          | Command exited 0 but left uncommitted changes in the worktree (handing off a dirty tree would advertise the PR as ready for human merge with state the dev never committed). |
| `verify_head_changed`   | Command exited 0, tree clean, but the command moved `HEAD` (e.g. ran `git commit` on its own). The subsequent squash + force-push would otherwise publish an unreviewed commit; the park comment surfaces the before / after SHAs. |

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

- **Across repos.** When `REPOS` lists more than one entry, `main._run_tick` fans the per-repo `workflow.tick(gh, spec)` calls out across a `ThreadPoolExecutor` (one worker per repo). The legacy single-repo mode (`REPOS` unset) stays in-thread.
- **Within a repo.** Per-issue handlers are dispatched to a long-lived `IssueScheduler`. Fan-out issues (`ready` / `implementing` / `documenting` / `validating` / `in_review` / `fixing` / `resolving_conflict`) are submitted one callable per issue. Family-aware issues (`decomposing` / `blocked` / `umbrella` / unlabeled pickup) are folded into ONE bucket submit per repo that drains them sequentially.

The two caps below are the levers:

| Variable                       | Default | Purpose                                                                                              |
| ------------------------------ | ------- | ---------------------------------------------------------------------------------------------------- |
| `MAX_PARALLEL_ISSUES_PER_REPO` | `1`     | per-repo cap on concurrent in-flight per-issue handlers. Each `REPOS` entry can override via its fifth pipe-separated field. Must be a positive integer. |
| `MAX_PARALLEL_ISSUES_GLOBAL`   | `3`     | global cap across all configured repos. Must be a positive integer; raise only with the CPU / memory headroom to run that many agent CLIs at once. Umbrella-only family buckets are cap-exempt and run on a dedicated executor. |

Both caps are enforced by a single `IssueScheduler` (`orchestrator/scheduler.py`) built once at startup and threaded through every `workflow.tick` call. A submit is skipped this tick (and retried next pass) when:

- the `(repo_slug, issue_number)` pair is already in flight (duplicate-active gate),
- the global or per-repo cap is reached,
- another family worker on the same repo is already in flight (family mutex).

**Umbrella exemption.** When every family-aware issue in this tick's bucket carries the `umbrella` label, the dispatcher submits the bucket as cap-exempt: it does not consume cap slots and runs on a dedicated executor pool. The family mutex still applies. Mixed buckets (umbrella alongside `decomposing` / `blocked` / unlabeled pickup) stay cap-counted.

**Family vs fan-out labels:**

- **Family-aware** (`decomposing`, `blocked`, `umbrella`, unlabeled): read and write cross-issue state (parent ↔ child) and must never run two at a time on the same repo.
- **Fan-out** (`ready`, `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `resolving_conflict`, `question`): only touch per-issue state; fan out concurrently up to the caps.

The pre-tick base refresh (`_refresh_base_and_worktrees`) is scheduler-aware: per-issue worktrees whose handler is currently in flight are skipped this tick, so a base advance cannot rebase a pre-PR worktree under a still-running agent. The skip is conditional on active state.

`shutdown(wait=True)` runs on process exit (normal `--once` return, `SIGINT` / `SIGTERM`, or self-modifying-merge restart) so any in-flight workers complete cleanly. The signal handler also calls `scheduler.shutdown(wait=False)` synchronously the instant the signal lands, so the submit path is closed mid-tick.

`main._run_tick` calls `scheduler.reap()` exactly once per polling pass (right before `analytics.prune_with_retention_logging()`) so worker failure-completion records drain before the next iteration. `_dispatch_via_scheduler` deliberately does NOT reap.

Non-positive or non-integer values for either cap (or for a per-entry `parallel_limit`) abort startup with a clear error.

## Workspace and agent identity

| Variable           | Default                                       | Purpose                                                                                                       |
| ------------------ | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `WORKTREES_DIR`    | `../wt-orchestrator`                          | where per-issue git worktrees are created; layout is `WORKTREES_DIR/<owner>__<name>/issue-N`                  |
| `LOG_DIR`          | `<REPO_ROOT>/logs`                            | directory `main.py` attaches its `FileHandler` under (`orchestrator.log`, rotated ~10 MiB × 5). Also the default parent for `ANALYTICS_LOG_PATH` (`LOG_DIR/analytics.jsonl`). Already covered by the `*.log` `.gitignore` rule. |
| `AGENT_GIT_NAME`   | `agent-orchestrator`                          | `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` injected into agent spawns                                             |
| `AGENT_GIT_EMAIL`  | `agent-orchestrator@users.noreply.github.com` | `GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_EMAIL` injected into agent spawns                                           |

## In-review behavior

The orchestrator is permanently manual-merge-only: humans click Merge. `_handle_in_review` routes fresh PR feedback to `fixing`, pings the HITL handles once per head SHA when the PR is mergeable and the current head completed the reviewer-approved final-docs handoff (or carries a real GitHub APPROVED review), and parks awaiting human attention for an unmergeable PR.

| Variable                     | Default | Purpose                                                                                              |
| ---------------------------- | ------- | ---------------------------------------------------------------------------------------------------- |
| `IN_REVIEW_DEBOUNCE_SECONDS` | `600`   | quiet window the `fixing` stage honours before resuming the dev on PR feedback. Newer comments arriving while already labeled `fixing` reset the window. `_handle_in_review` itself routes fresh feedback to `fixing` immediately and does NOT apply the debounce. |

## Observability

| Variable                   | Default                          | Purpose                                                                                                                       |
| -------------------------- | -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `EVENT_LOG_PATH`           | _(unset)_                        | optional JSONL audit sink, one event per line, no built-in rotation. See [`observability.md#audit-event-log`](observability.md#audit-event-log-event_log_path). |
| `ANALYTICS_LOG_PATH`       | `LOG_DIR/analytics.jsonl`        | project-local analytics JSONL sink. Records `stage_enter`, `stage_evaluation`, and `agent_exit` events. Set to empty / `off` / `disabled` / `none` to disable. See [`observability.md#analytics-sink`](observability.md#analytics-sink-analytics_log_path). |
| `ANALYTICS_RETENTION_DAYS` | `90`                             | retention window for `ANALYTICS_LOG_PATH`. The polling loop calls `analytics.prune_with_retention_logging()` once per tick. Set to `0` (or any non-positive value) to keep raw data indefinitely. |
| `ANALYTICS_DB_URL`         | _(unset)_                        | libpq connection string for the analytics Postgres service in [`../analytics-db/compose.yml`](../analytics-db/compose.yml). NOT read by the polling loop — orchestrator correctness does not depend on database availability. Empty / `off` / `disabled` / `none` disables both the sync CLI and dashboard reads. See [`observability.md#analytics-database`](observability.md#analytics-database-analytics-db). |
| `DASHBOARD_PARALLEL_READS` | _(unset, off)_                   | opt-in switch for the Streamlit dashboard's parallel read fan-out. `1` / `true` / `on` / `yes` (case-insensitive) flips the dashboard's widget reads from sequential to a `ThreadPoolExecutor` (eight workers). Parsed at dashboard import; the polling loop never reads it. |

`ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`, and `ANALYTICS_DB_URL` are parsed at import inside `orchestrator/analytics/__init__.py` (the package owns its own configuration surface). `EVENT_LOG_PATH` is parsed in `orchestrator/config.py` because the audit event log is a general-purpose audit surface rather than analytics-specific.

### Analytics dashboard quickstart

The pipeline is opt-in and layered: the orchestrator writes JSONL (`ANALYTICS_LOG_PATH`), a local Postgres aggregates it (`ANALYTICS_DB_URL`), and Streamlit reads from Postgres. Each layer is independent — the polling loop never touches Postgres or Streamlit, so deferring or disabling the dashboard never affects workflow correctness.

1. **Confirm the JSONL sink is producing records.** `ANALYTICS_LOG_PATH` defaults to `logs/analytics.jsonl`. `wc -l logs/analytics.jsonl` and `tail -1 logs/analytics.jsonl | python -m json.tool` sanity-check it.
2. **Start the local Postgres service.** From `analytics-db/`, run `docker compose up -d`. The init script ([`../analytics-db/init/01-schema.sql`](../analytics-db/init/01-schema.sql)) creates the `analytics_events` table on first start; the data volume lives at `analytics-db/data/` (gitignored). The port binding is pinned to `127.0.0.1` and credentials default to `orchestrator` / `orchestrator`; override `POSTGRES_PASSWORD` (and any other field) in `analytics-db/.env` before exposing the port off-host or storing real data.
3. **Point the orchestrator at the database.** Set `ANALYTICS_DB_URL` in `.env`:

   ```sh
   ANALYTICS_DB_URL=postgresql://orchestrator:orchestrator@127.0.0.1:5432/orchestrator_analytics
   ```

   Putting the database password in `.env` is acceptable — the URL is the only credential, it is scoped to local-only Postgres, and never grants write access to GitHub. The polling loop does not re-read this setting.
4. **Populate Postgres from JSONL.** Run the sync on demand:

   ```sh
   uv run python -m orchestrator.analytics.sync
   ```

   Inserts dedupe by `content_hash`, so re-running is idempotent. No-op when `ANALYTICS_DB_URL` is unset/disabled, `ANALYTICS_LOG_PATH` is explicitly disabled, or the JSONL file is absent. Schedule on whatever cadence you prefer.
5. **Launch the dashboard.** Install the optional `dashboard` group once, then run Streamlit:

   ```sh
   uv sync --group dashboard
   uv run streamlit run orchestrator/dashboard.py
   ```

   Streamlit prints a `http://localhost:8501` URL. The dashboard is independent of the polling tick and can be killed and relaunched without affecting workflow progress. Re-run step 4 to pick up new records.

See [`observability.md#analytics-database`](observability.md#analytics-database-analytics-db) for the schema, sync internals, read-model split, dashboard layout, and the in-app empty / error banners.

## Continuous integration

[`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs `ruff check orchestrator tests` and `pytest` on Python 3.12 for every push to `main` and every pull request, installing from the committed [`../uv.lock`](../uv.lock) via `uv sync --locked`. Lint rules live in [`../pyproject.toml`](../pyproject.toml) under `[tool.ruff.lint]`; dev tools are declared in `[dependency-groups]`.

The workflow declares `permissions: contents: read` so the run's `GITHUB_TOKEN` is read-only and cannot publish artifacts, push tags, or comment on PRs. The job uses no repository secrets, so PRs from forks run safely under the same scope.

[`../.github/dependabot.yml`](../.github/dependabot.yml) opens weekly update PRs for the `github-actions` and `uv` (Python `pyproject.toml` + `uv.lock`) ecosystems with a 30-day `cooldown.default-days` window. [`../.github/workflows/dependency-review.yml`](../.github/workflows/dependency-review.yml) runs `actions/dependency-review-action` on every PR and fails the check when a PR introduces a vulnerable or non-compliant dependency.

## Run modes

- `./run.sh` — production. Continuous polling. `run.sh` does `git pull --ff-only origin "$ORCHESTRATOR_BASE_BRANCH"` (read from `.env`, default `main`) and re-launches the orchestrator after each clean exit, so a self-modifying merge picks up new code automatically. If the pull fails, the wrapper prints the failing command and exits non-zero instead of relaunching stale code.

  Ctrl+C (or `SIGTERM`) stops the wrapper: the orchestrator exits with `128 + signum` and `run.sh` skips the restart loop. A second Ctrl+C terminates immediately.
- `python -m orchestrator.main --once` — single tick then exit. Useful for tests and debugging.
- `python -m orchestrator.main --log-level DEBUG` — verbose logs.

On first start the orchestrator creates the workflow labels and the `hold_base_sync` / `backlog` control labels on the repo, then begins polling open issues every `POLL_INTERVAL` seconds.

## Running under systemd (user service)

`run.sh` does not survive a reboot, a `tty` logout, or the user manager being torn down. The recommended production deployment is a systemd **user** service that supervises `run.sh` directly.

A detached `screen` / `tmux` session wrapped in a `Type=forking` unit looks similar but is the wrong shape: systemd ends up supervising `screen`, not the orchestrator; `ExecStop` races the screen session's own lifecycle; logs split; and the unit silently does nothing at boot unless linger is enabled. Keep `screen` / `tmux` for interactive debugging.

### Unit file

Drop this at `~/.config/systemd/user/agent.service`, replacing the working directory and the `PATH` entries:

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

- `Type=simple` because `run.sh` stays in the foreground — systemd tracks the wrapper PID, and `SIGTERM` from `systemctl stop` propagates to the wrapper, then to the orchestrator (exit `143`, no restart loop).
- `Restart=always` covers machine-level events (reboot, OOM, host crash). Application-level self-restart after a self-modifying merge is still handled inside `run.sh`.
- A non-interactive systemd service does not inherit your shell's `PATH`. If `codex` or `claude` lives under `~/.local/bin`, add it to `Environment=PATH=…`, or set `CODEX_BIN` / `CLAUDE_BIN` to absolute paths via additional `Environment=` lines.

### Enabling

```sh
systemctl --user daemon-reload
systemctl --user enable --now agent.service
loginctl enable-linger <user>
```

`enable-linger` is **required for boot-time start**: without it the per-user systemd manager only runs while the user has an active login session.

### Operating

```sh
systemctl --user status agent.service        # current state and last log lines
systemctl --user restart agent.service       # bounce the orchestrator
systemctl --user stop agent.service          # SIGTERM the wrapper (exits 143, no restart)
journalctl --user-unit agent.service -f      # tail the wrapper's stdout/stderr
```

systemd's journal captures `run.sh` and orchestrator stdout/stderr (process lifecycle, exit codes, restart messages). The orchestrator's own structured log lives at `logs/orchestrator.log` under `WorkingDirectory` (rotated, ~10 MiB × 5). Check the journal first for "did it start / did it die", then `logs/orchestrator.log` for per-issue handler detail.

## Applying `.env` changes

`.env` is read once, when `python -m orchestrator.main` starts. The orchestrator process never reloads it, so most edits take effect on the **next fresh Python start** — there is no signal to make a running process re-read configuration. `run.sh` is the usual restart mechanism: each loop iteration launches a new Python process (and `git pull --ff-only`s the orchestrator checkout to `ORCHESTRATOR_BASE_BRANCH` along the way).

### What survives a restart

Per-issue progress lives in the issue's pinned JSON comment on GitHub and in the per-issue worktree on disk. Restarting between ticks loses nothing — the next tick picks each issue back up from its label and pinned state. Two restart-time hazards are worth knowing:

- **A live `codex` / `claude` child.** Stage handlers spawn agent subprocesses that may run for as long as `AGENT_TIMEOUT`. Killing the orchestrator while a child is mid-session also kills the child, which can leave the issue parked on `awaiting_human`, routed through timeout recovery on the next tick, or sitting on a dirty worktree.
- **In-flight agent spec is pinned.** When a `codex` / `claude` session starts, the orchestrator writes the full `DEV_AGENT` / `DECOMPOSE_AGENT` spec into pinned state and re-parses it (not the current `.env`) on every resume. Flipping `DEV_AGENT` or `DECOMPOSE_AGENT` after a session is locked does nothing for that issue until it reaches `done` or `rejected`. The question stage seeds from `DECOMPOSE_AGENT` on first spawn and pins to `question_agent` for the rest of the Q&A. `REVIEW_AGENT` is not pinned — the reviewer spawns fresh each round.

### Safe restart guidance

- **Idle / between ticks — safe.** Restart freely; the next tick resumes from GitHub state.
- **Issue mid-stage with no agent child — generally safe.** Workflow state is on GitHub and in the worktree.
- **Live `codex` / `claude` child — avoid.** Wait for the agent to exit. Forcing a restart can park the issue or leave a dirty worktree behind.

Useful inspection commands:

```sh
pgrep -af 'python -m orchestrator.main|codex|claude|run.sh'
tail -f logs/orchestrator.log
journalctl --user -u agent.service -f   # systemd users
```

### Per launch style

**Foreground terminal (`./run.sh` in a shell).**

1. Edit `.env`.
2. Confirm no agent child is running (`pgrep -af 'codex|claude'`).
3. Ctrl+C the terminal (`run.sh` exits with code 130 and skips the restart loop).
4. Re-run `./run.sh`.

A second Ctrl+C while `run.sh` is mid-shutdown terminates immediately.

**`tmux` / `screen` session.**

1. Attach (`tmux attach -t orchestrator`, or `screen -r`).
2. Check live output for an in-flight stage handler; cross-check with `pgrep -af 'codex|claude'`.
3. At a safe point, Ctrl+C the orchestrator and re-run `./run.sh`.
4. Detach (Ctrl+B then D for tmux, Ctrl+A then D for screen).

**systemd user service.**

1. Edit `.env` in the unit's `WorkingDirectory=`.
2. **Skip `systemctl --user daemon-reload`** unless the `.service` unit file itself changed — `daemon-reload` reloads unit definitions, not `.env`.
3. When safe (no live agent child), `systemctl --user restart agent.service`.
4. Tail logs: `journalctl --user -u agent.service -f`.

When `GITHUB_TOKEN` is supplied via the unit's `EnvironmentFile=`, edit that file and restart the service. When the token is hard-coded in an inline `Environment=` line, changing the value requires editing the unit *and* a `daemon-reload` before the restart.

**Direct `python -m orchestrator.main --once`.**

Each `--once` invocation is a fresh Python process and reads the current `.env` on every call.

### Setting-by-setting expectations

| Setting | When the change takes effect |
| ------- | ---------------------------- |
| `POLL_INTERVAL`, `AGENT_TIMEOUT`, `REVIEW_TIMEOUT`, `MAX_REVIEW_ROUNDS`, `MAX_CONFLICT_ROUNDS`, `MAX_RETRIES_PER_DAY`, `IN_REVIEW_DEBOUNCE_SECONDS`, `DECOMPOSE`, `SQUASH_ON_APPROVAL`, `VERIFY_COMMANDS`, `VERIFY_TIMEOUT`, `LOG_DIR`, `EVENT_LOG_PATH`, `ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`, `REPO` / `REPOS` / `TARGET_REPO_ROOT` / `BASE_BRANCH` / `REMOTE_NAME`, `HITL_HANDLE`, `ALLOWED_ISSUE_AUTHORS` | next Python start |
| `ANALYTICS_DB_URL` | next `python -m orchestrator.analytics.sync` invocation, and next `streamlit run orchestrator/dashboard.py` start (the dashboard reads it from the imported analytics module, so a browser reload is not enough — relaunch Streamlit). The polling loop does not read this setting. |
| `DASHBOARD_PARALLEL_READS` | next `streamlit run orchestrator/dashboard.py` start. Parsed at dashboard import. |
| `MAX_PARALLEL_ISSUES_PER_REPO`, `MAX_PARALLEL_ISSUES_GLOBAL` | next Python start. Per-`REPOS` `parallel_limit` overrides take precedence over `MAX_PARALLEL_ISSUES_PER_REPO`. |
| `DEV_AGENT`, `DECOMPOSE_AGENT` | next Python start, **except** for issues whose pinned state already names a `dev_agent` / `decomposer_agent` / `question_agent` — those keep the pinned spec until the issue reaches `done` or `rejected` |
| `REVIEW_AGENT` | next reviewer spawn after the next Python start (not pinned per issue) |
| `GITHUB_TOKEN` | not loaded from `.env`. Update the process environment or rewrite the file at `ORCHESTRATOR_TOKEN_FILE` (default `~/.config/<owner>/<repo>/token`) before the next start |
| `ORCHESTRATOR_BASE_BRANCH` | `run.sh` captures this once before its restart loop, so editing it only takes effect after `run.sh` itself is restarted. The Python process picks it up on the same next start. |

## Control labels

| Label | Purpose |
| ----- | ------- |
| `hold_base_sync` | Apply to an issue to pause per-tick base rebases (pre-PR worktrees rebase onto `origin/<base>` directly; PR-having worktrees detour to `resolving_conflict` for a rebase), the `in_review` HITL ping / unmergeable park, and `resolving_conflict` base rebases. Remove it when prerequisite PRs have landed; the next tick performs the accumulated base sync once. |
| `backlog` | Apply to an issue (typically at creation) to keep the orchestrator from picking it up. The dispatcher skips the issue entirely while the label is present; remove the label to release the issue for processing. |
