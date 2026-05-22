# Agent orchestration

[![CI](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml)

Orchestrator for automatic issues resolving utilizing agents.

The orchestrator watches GitHub Issues, drives them through a label-based state machine, and spawns local CLI agents (`codex`, `claude`) to implement them and open PRs. State lives in GitHub Issues themselves (one workflow label + one pinned JSON comment), so the orchestrator stays stateless and progress is observable on github.com.

For the design and stage definitions, see [`docs/architecture.md`](docs/architecture.md).
For the implementation roadmap, see [`plans/roadmap.md`](plans/roadmap.md).

## Requirements

### System

- Linux (developed and tested on Ubuntu 24.04 / WSL2)
- Git
- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) — Python package and venv manager (alternative: `python3-venv` + `pip`)

### CLI agents

The orchestrator spawns these as subprocesses. Only the backends actually selected by the first token of `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` need to be installed and authenticated on the host before the orchestrator starts — the defaults (`claude` implements and decomposes, `codex` reviews) use both, so most setups need both, but a single-backend deployment can skip the other. Roles are configurable via those three shell-like command specs; see [Agent command specs](#agent-command-specs) below for the format and the in-flight session lock semantics.

- [`codex`](https://github.com/openai/codex) — invoked with `--dangerously-bypass-approvals-and-sandbox`. Run `codex login` once. The host is the sandbox boundary.
- [`claude`](https://docs.anthropic.com/en/docs/claude-code) — invoked with `--dangerously-skip-permissions`. Authenticate via `claude` once.

### GitHub

- A repository the orchestrator will manage (default: this one).
- A **fine-grained Personal Access Token** scoped to that repository, with these repository permissions:
  - **Contents**: Read and write — push branches
  - **Issues**: Read and write — read issues, post comments, set/create labels
  - **Pull requests**: Read and write — open PRs
  - **Checks**: Read-only — required for `AUTO_MERGE` to evaluate Actions-only PRs (without it, the orchestrator sees `check_state='none'` and parks waiting for a human even when CI is green)
  - **Metadata**: Read-only — required and forced on

  Generate at <https://github.com/settings/personal-access-tokens>.

### Python dependencies

Pinned in [`pyproject.toml`](pyproject.toml):

- `PyGithub >= 2.1`

## Quick start

1. **Clone and enter the repo**

   ```sh
   git clone https://github.com/geserdugarov/agent-orchestrator.git
   cd agent-orchestrator
   ```

2. **Create a venv and install dependencies**

   ```sh
   uv venv --python 3.12
   uv pip install PyGithub
   ```

3. **Configure environment**

   ```sh
   cp .env.example .env
   ```

   Edit `.env` and set at minimum:
   - `HITL_HANDLE` — comma-separated GitHub logins (the users the orchestrator @-mentions on questions)
   - `REPO` — leave default unless pointing at a different repo

   Then store the PAT **outside** the repo so the implementer agent (which runs
   in a sibling worktree with sandbox bypass) cannot read it via a relative
   path:

   The default token path is derived from `REPO` (`~/.config/<owner>/<repo>/token`).
   For the default repo:

   ```sh
   install -d -m 700 ~/.config/geserdugarov/agent-orchestrator
   printf %s "$YOUR_PAT" > ~/.config/geserdugarov/agent-orchestrator/token
   chmod 600 ~/.config/geserdugarov/agent-orchestrator/token
   ```

   Or export `GITHUB_TOKEN` in the orchestrator's launch environment. Putting
   the PAT in `.env` is rejected at startup. Override the file path with
   `ORCHESTRATOR_TOKEN_FILE` if you want a different location — pick one the
   agent worktree cannot reach via known relatives.

4. **Verify the agents are authenticated**

   ```sh
   codex --version
   claude --version
   ```

   If a backend is not logged in, run its `login` flow (`codex login` / `claude /login`). Only the backends you actually route to via `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` (the first token of each spec) need to be authenticated, but the defaults use both.

5. **Run**

   ```sh
   ./run.sh
   ```

   The wrapper does `git pull --ff-only origin main` and re-launches the orchestrator after each clean exit (so a self-modifying merge picks up the new code automatically). Ctrl+C (or `SIGTERM`) stops the wrapper too: the orchestrator exits with `128 + signum` and `run.sh` skips the restart loop. A second Ctrl+C terminates immediately.

   On first start the orchestrator creates the 10 workflow labels on the repo and begins polling open issues every 60 seconds.

6. **File a bootstrap test issue** to verify the path works end-to-end:

   > **Title:** Add a `hello()` function to the orchestrator package
   > **Body:** Add `hello()` to `orchestrator/__init__.py` returning the string `"hello, world"`. Add `tests/test_hello.py` asserting the return value. Don't change anything else.

   Within ~1 minute the orchestrator should comment "picking this up", label the issue `implementing`, run the dev agent (`DEV_AGENT`, default `claude`) in a fresh worktree at `../wt-orchestrator/issue-N`, push the branch, open a PR, label the issue `validating`, run a fresh reviewer session (`REVIEW_AGENT`, default `codex`) against the diff, and on `VERDICT: APPROVED` move the issue to `in_review`. With `AUTO_MERGE=on`, the orchestrator then merges the PR itself once GitHub reports it mergeable with green checks, flips the label to `done`, and closes the issue. With `AUTO_MERGE=off` (the default), review and merge the PR manually.

## Run modes

- `./run.sh` — production: continuous polling with auto-restart on self-modifying merges
- `python -m orchestrator.main --once` — single tick then exit, useful for testing
- `python -m orchestrator.main --log-level DEBUG` — verbose logs

## Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `ruff check` and `pytest` on Python 3.12 for every push to `main` and every pull request. Lint rules are configured in [`pyproject.toml`](pyproject.toml) under `[tool.ruff.lint]`.

## Configuration reference

All settings load from `.env` (or process environment). See [`.env.example`](.env.example) for the full list with defaults. Key knobs:

| Variable                  | Default                                       | Purpose                                                   |
| ------------------------- | --------------------------------------------- | --------------------------------------------------------- |
| `GITHUB_TOKEN`            | _(required, env-only — not read from `.env`)_ | fine-grained PAT                                          |
| `ORCHESTRATOR_TOKEN_FILE` | `~/.config/<owner>/<repo>/token` (from `REPO`) | path to PAT file (used when `GITHUB_TOKEN` is not in env) |
| `REPO`                    | `geserdugarov/agent-orchestrator`  | `owner/name` of the single repo to manage (ignored when `REPOS` is set) |
| `REPOS`                   | _(unset)_                                     | multi-repo configuration, see [Managing multiple repositories](#managing-multiple-repositories) |
| `POLL_INTERVAL`           | `60`                                          | seconds between polling ticks                             |
| `AGENT_TIMEOUT`           | `1800`                                        | wall-clock cap per agent invocation, seconds              |
| `REVIEW_TIMEOUT`          | (= `AGENT_TIMEOUT`)                           | wall-clock cap per reviewer invocation, seconds           |
| `MAX_REVIEW_ROUNDS`       | `3`                                           | review/fix iterations before parking on `awaiting_human` |
| `MAX_CONFLICT_ROUNDS`     | `3`                                           | auto-conflict-resolution rounds before parking on `awaiting_human` |
| `MAX_RETRIES_PER_DAY`     | `3`                                           | fresh implementer spawns per issue per 24h window (`0` = unbounded) |
| `DEV_AGENT`               | `claude`                                      | implementer command spec; first token `codex` / `claude`, remaining tokens forwarded as backend-CLI args (see [Agent command specs](#agent-command-specs)) |
| `REVIEW_AGENT`            | `codex`                                       | reviewer command spec; first token `codex` / `claude`, remaining tokens forwarded as backend-CLI args |
| `DECOMPOSE_AGENT`         | `claude`                                      | decomposer command spec; first token `codex` / `claude`, remaining tokens forwarded as backend-CLI args (validated even when `DECOMPOSE=off`) |
| `DECOMPOSE`               | `on`                                          | enable the `decomposing` stage; `off` reverts to the legacy "no label → implementing" pickup |
| `HITL_HANDLE`             | `geserdugarov`                                | comma-separated GitHub logins to @-mention when a human is needed |
| `WORKTREES_DIR`           | `../wt-orchestrator`                          | where per-issue git worktrees are created; per-repo subdir keeps them isolated, so the on-disk layout is `WORKTREES_DIR/<owner>__<name>/issue-N` |
| `CODEX_BIN`               | `codex`                                       | executable launched when a role's first token is `codex`; override only if `codex` is not on `$PATH` |
| `CLAUDE_BIN`              | `claude`                                      | executable launched when a role's first token is `claude`; override only if `claude` is not on `$PATH` |
| `AGENT_GIT_NAME`          | `agent-orchestrator`                          | `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` injected into agent spawns |
| `AGENT_GIT_EMAIL`         | `agent-orchestrator@users.noreply.github.com` | `GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_EMAIL` injected into agent spawns |
| `BASE_BRANCH`             | `main`                                        | branch PRs target                                         |
| `AUTO_MERGE`              | `off`                                         | merge approved PRs (green CI + mergeable) from `in_review`; flip to `on` once dogfooded |
| `IN_REVIEW_DEBOUNCE_SECONDS` | `600`                                       | quiet window after the latest PR/issue comment before resuming the dev session |
| `EVENT_LOG_PATH`          | _(unset)_                                     | optional JSONL audit sink; one event per line, no built-in rotation. See [Audit event log](#audit-event-log) |

## Agent command specs

`DEV_AGENT`, `REVIEW_AGENT`, and `DECOMPOSE_AGENT` are not bare backend names — they are full shell-like command specs parsed with `shlex.split`. The **first token** names the backend and must match `codex` or `claude` **case-insensitively** (`CODEX`, `Claude`, and `codex` all parse to the same backend; the lowercased form is used only for dispatch in `agents.py`, while pinned state keeps the raw full spec verbatim, so `DEV_AGENT=CODEX -m gpt-5.5` is stored as the literal `CODEX -m gpt-5.5` and re-lowercased on every resume by `_parse_agent_spec`). Any remaining tokens are forwarded verbatim as backend-CLI args on every spawn for that role. Anything else (unknown first token, empty value, unbalanced quotes) aborts at startup so a typo cannot silently fall back.

Examples:

```dotenv
# bare backends (defaults)
DEV_AGENT=claude
REVIEW_AGENT=codex
DECOMPOSE_AGENT=claude

# claude with model / effort selection
DEV_AGENT=claude --model claude-opus-4-7
REVIEW_AGENT=claude --model claude-sonnet-4-6 --effort high

# codex with model and reasoning effort
DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'
REVIEW_AGENT=codex -m gpt-5.5-codex
DECOMPOSE_AGENT=codex -m gpt-5.5
```

`CODEX_BIN` / `CLAUDE_BIN` interact with the first token as a backend selector: the first token only picks the codex vs. claude runner, while the actual executable launched is `CODEX_BIN` for `codex` and `CLAUDE_BIN` for `claude`. Override those when the CLI is not on `$PATH`; writing the full path as the first token of `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` is rejected.

**In-flight issues keep using the pinned full spec until the agent session ends.** The dev/decomposer spec (backend + args) is persisted to the issue's pinned state (`dev_agent` / `decomposer_agent`) on the first spawn, and `_handle_implementing` / `_handle_decomposing` re-parse that stored spec on every resume. Flipping `DEV_AGENT` or `DECOMPOSE_AGENT` in env therefore only affects fresh issues — any issue with a live session keeps the original backend AND args (including model / effort flags) until it reaches a terminal label (`done` / `rejected`). The reviewer is spawned fresh every round, so `REVIEW_AGENT` changes take effect on the next validating round; the most recent value is recorded in `review_agent` for traceability. `DECOMPOSE_AGENT` is validated at import even when `DECOMPOSE=off`, so toggling `DECOMPOSE` back on never surfaces a fresh "that env var was always invalid" failure.

## Audit event log

Setting `EVENT_LOG_PATH` enables an opt-in JSONL audit sink: the orchestrator appends one JSON object per workflow event to that file. Leave it unset (default) and no file is opened — observable behavior is identical to a deployment without the sink. The parent directory is created on demand; writes are synchronous so order matches the tick.

Every record has the same envelope:

```json
{"event":"stage_enter","issue":42,"repo":"acme/api","stage":"implementing","ts":"2026-05-22T14:03:11+00:00"}
```

`ts` is UTC at second precision, keys are emitted sorted, and optional fields are omitted entirely when their value is `None` (so a stage-less event has no `"stage":null` and a record without a `session_id` simply lacks the key).

Event kinds:

| `event` | Emitted when | Notable extras |
|---|---|---|
| `stage_enter` | `set_workflow_label` flips an issue to a workflow label | `stage` |
| `agent_spawn` / `agent_exit` | bookend every decomposer / implementer / reviewer / resume / conflict-resolution `run_agent` call | both: `agent`, `agent_role`, `review_round`, `retry_count`. `agent_spawn` only on resumes: `session_id` (the resume id; omitted for fresh spawns since `None` extras are dropped). `agent_exit` always: `session_id` (the result id), `duration_s`, `exit_code`, `timed_out` |
| `review_verdict` | `_handle_validating` parses the reviewer's final message | `verdict` (`approved` / `changes_requested` / `unknown`), `review_round`, `pr_number`, `session_id` |
| `park_awaiting_human` | every `_park_awaiting_human` / `_on_question` / `_on_dirty_worktree` call site | `stage`, `reason` (`agent_timeout`, `push_failed`, `failed_checks`, `agent_question`, `agent_silent`, `dirty_worktree`, `reviewer_*`, `missing_pr_number`, …) |
| `pr_opened` | implementer's clean-tree push opens the PR | `pr_number`, `branch`, `sha`, `retry_count` |
| `pr_merged` | PR merged externally or by AUTO_MERGE | `pr_number`, `sha`, `merge_method` (`external` / `squash`), `check_state`, `review_round`, `conflict_round` |
| `pr_closed_without_merge` | PR closed unmerged from `in_review` or `resolving_conflict` (issue lands on `rejected`) | `pr_number`, `sha`, `review_round`, `conflict_round` |
| `merge_attempt` | AUTO_MERGE `gh.merge_pr` call, or a `git merge origin/<base>` inside `_handle_resolving_conflict` | `method` (`squash` / `base_merge`), `result` (`success` / `failed` / `conflict`), `pr_number`, `sha`, `conflict_round` |
| `conflict_round` | entered `resolving_conflict` (`action="entered"`) or bumped the per-PR counter (`action="incremented"` with `outcome`) | `pr_number`, `conflict_round`, `review_round`, `retry_count` |

**No built-in rotation.** The orchestrator only appends; it never truncates, renames, or compresses the file. External rotation and recreation are operator-managed — pair `EVENT_LOG_PATH` with `logrotate` (or your platform's equivalent) if you leave the orchestrator running long enough that file size matters.

**Pinned state is authoritative.** The append-only log is for audit and observability only. Per-issue state lives in the pinned `<!--orchestrator-state ...-->` comment on each issue (see [`docs/architecture.md`](docs/architecture.md)), and that is the only source the orchestrator reads on the next tick. If the log and pinned state disagree — a write failed and was logged-and-swallowed, the file was truncated by rotation, the disk filled, or events landed out-of-order during a crash — trust pinned state and treat the log as a lossy tail.

## Managing multiple repositories

Set `REPOS` to drive several target repositories from one orchestrator process. Each entry is `owner/name|target_root|base_branch`; entries are separated by newlines or `;` (the latter so the value fits on a single `.env` line). Example `.env` snippet:

```dotenv
REPOS=acme/api|/srv/clones/acme-api|main;acme/web|/srv/clones/acme-web|master
```

When `REPOS` is set the legacy `REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH` trio is ignored. Validation happens at import — a malformed entry, empty owner/name, empty base branch, or a duplicate slug aborts startup with a clear error. A `target_root` that does not exist on disk is warned to stderr but does not block startup, so a partial deploy can still surface the misconfiguration on the first tick.

Each tick iterates every configured spec and runs `workflow.tick(gh, spec)` once per repo with a per-spec `GitHubClient`; a failure in one repo's tick is logged and skipped so the remaining repos still advance. Worktrees are namespaced under `WORKTREES_DIR/<owner>__<name>/issue-N` (and `decompose-N`) so two repos that share an issue number cannot collide on disk.

Tokens are resolved per slug: `GitHubClient` reads `GITHUB_TOKEN` from the environment first (works for any repo the PAT has access to), and otherwise falls back to `~/.config/<owner>/<repo>/token` derived from the spec's slug — so each repo can have its own fine-grained PAT in its own file. Override the file path globally with `ORCHESTRATOR_TOKEN_FILE` if you need a non-default location.

## Current scope

The orchestrator currently drives (no label) → `decomposing` → `ready`/`blocked`/`umbrella` → `implementing` → `validating` → `in_review` → `resolving_conflict` (optional) → `done`/`rejected`, with configurable dev/review/decompose backend splits, a per-issue retry budget (`MAX_RETRIES_PER_DAY`), a review/fix loop capped by `MAX_REVIEW_ROUNDS`, and a debounced PR-comment-resume loop in `in_review`. The decomposer asks the agent for a fenced `orchestrator-manifest` JSON block; on `decision=single` the parent flips straight to `ready`, on `decision=split` it creates child issues, persists the dep graph, and parks on `blocked` or `umbrella` until the child issues resolve. Auto-merge on approve+green-CI is gated by `AUTO_MERGE` (default `off`); enable it once dogfooded. The decomposer can be disabled with `DECOMPOSE=off`, which reverts to the legacy direct-to-`implementing` pickup; the same flag also routes any issue already labeled `decomposing` (e.g. parked there awaiting a human) to `implementing` on the next tick, so the kill switch applies to in-flight issues, not just new ones. See [`plans/roadmap.md`](plans/roadmap.md).

## License

Licensed under the Apache License, Version 2.0. See [`LICENSE`](LICENSE) for the full text.
