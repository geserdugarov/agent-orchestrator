# Agent orchestration

[![CI](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml)

A GitHub-Issue-driven workflow runner that turns local coding-agent CLIs (`codex`, `claude`) into a hands-off implementer + reviewer loop.

Filing an issue is the only handle: the orchestrator decomposes it if needed, spawns the dev agent in an isolated git worktree, opens a PR, runs a fresh reviewer pass, and (optionally) auto-merges.

State lives entirely in the issue itself — one workflow label plus one pinned JSON comment — so progress is observable on github.com and the orchestrator can be restarted without losing context.

It is meant for solo or small-team setups that already have a `codex` or `claude` login and want autonomy without standing up a separate planner, queue, or database.

For design and stage definitions, see [`docs/architecture.md`](docs/architecture.md). For agent roles and command specs, see [`docs/workflow.md`](docs/workflow.md). The implementation roadmap is in [`plans/roadmap.md`](plans/roadmap.md).

## Requirements

### System

- Linux (developed and tested on Ubuntu 24.04 / WSL2)
- Git
- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) — Python package and venv manager (alternative: `python3-venv` + `pip`)

### CLI agents

The orchestrator spawns these as subprocesses on the host. The defaults are `claude` for implementation and decomposition, `codex` for review — so most setups need both authenticated.

A single-backend deployment can skip the other; the role mapping is configured via `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` (see [`docs/workflow.md`](docs/workflow.md)).

- [`codex`](https://github.com/openai/codex) — invoked with `--dangerously-bypass-approvals-and-sandbox`. Run `codex login` once. The host is the sandbox boundary.
- [`claude`](https://docs.anthropic.com/en/docs/claude-code) — invoked with `--dangerously-skip-permissions`. Authenticate via `claude` once.

### GitHub

- A repository the orchestrator will manage (default: this one).
- A **fine-grained Personal Access Token** scoped to that repository, with these permissions:
  - **Contents**: Read and write — push branches
  - **Issues**: Read and write — read issues, post comments, set/create labels
  - **Pull requests**: Read and write — open PRs
  - **Checks**: Read-only — required when enabling `AUTO_MERGE` so Actions-only PRs are not parked with `check_state='none'`
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
   path. The default token path is derived from `REPO` (`~/.config/<owner>/<repo>/token`):

   ```sh
   install -d -m 700 ~/.config/geserdugarov/agent-orchestrator
   printf %s "$YOUR_PAT" > ~/.config/geserdugarov/agent-orchestrator/token
   chmod 600 ~/.config/geserdugarov/agent-orchestrator/token
   ```

   Or export `GITHUB_TOKEN` in the orchestrator's launch environment. Putting the PAT in `.env` is rejected at startup.

4. **Verify the agents are authenticated**

   ```sh
   codex --version
   claude --version
   ```

   If a backend is not logged in, run its login flow (`codex login` / `claude /login`). Only the backends you actually route to (the first token of `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT`) need to be authenticated.

5. **Run**

   ```sh
   ./run.sh
   ```

   On first start the orchestrator creates its workflow/control labels on the repo and begins polling open issues every 60 seconds. For other launch options (single-tick, debug logging) see [`docs/configuration.md#run-modes`](docs/configuration.md#run-modes). For a supervised production deployment (systemd user service, linger, log inspection) see [`docs/configuration.md#running-under-systemd-user-service`](docs/configuration.md#running-under-systemd-user-service).

6. **File a bootstrap test issue** to verify the path works end-to-end:

   > **Title:** Add a `hello()` function to the orchestrator package
   > **Body:** Add `hello()` to `orchestrator/__init__.py` returning the string `"hello, world"`. Add `tests/test_hello.py` asserting the return value. Don't change anything else.

   Within ~1 minute the orchestrator should comment "picking this up" and label the issue `decomposing`. The decomposer agent declares the task fits one context, the label flips to `ready` and then `implementing`, and the dev agent runs in a fresh worktree at `../wt-orchestrator/geserdugarov__agent-orchestrator/issue-N`.

   The orchestrator then pushes the branch, opens a PR, labels the issue `documenting`, and runs a docs pass on the same PR worktree (updating `README.md`, `docs/`, or `plans/` to match what landed, or emitting an explicit `DOCS: NO_CHANGE` verdict when nothing needs updating). On the next tick the label advances to `validating`, the reviewer agent runs a fresh session against the diff, and on `VERDICT: APPROVED` the optional local `VERIFY_COMMANDS` (default empty) run in the worktree before the issue moves to `in_review`. A verify failure parks on `validating` with a typed `park_reason` and the failing command's output; GitHub CI remains the later auto-merge gate. See [`docs/configuration.md#local-verification-gate`](docs/configuration.md#local-verification-gate) for the verify-command reference.

   With `AUTO_MERGE=on`, the orchestrator then merges the PR itself once GitHub reports it mergeable with green checks, flips the label to `done`, and closes the issue. With `AUTO_MERGE=off` (the default), review and merge the PR manually.

   If a human posts PR feedback (issue thread, PR conversation, inline review, or PR review summary) while the issue is `in_review`, the orchestrator flips the label to `fixing` and queues the comments. After the `IN_REVIEW_DEBOUNCE_SECONDS` quiet window expires (newer comments arriving in the meantime reset the timer), the dev agent resumes against the unread feedback, pushes the fix, and the label routes through `documenting` (so the docs pass runs against the new head and refreshes any README / docs / plans touched by the fix) and then back through `validating` so the reviewer re-approves before auto-merge can proceed. If the rescan finds no unread feedback at all (the watermarks already covered the bookmarked comments), the label bounces directly back to `validating` without the documenting hop, since there is no fix work for the docs pass to react to.

## Asking the orchestrator a question

Apply the workflow label `question` to any open issue to get a read-only answer instead of an implementation. The orchestrator spawns the configured `DECOMPOSE_AGENT` in the issue's `issue-N` worktree, posts the agent's answer (or its own clarifying follow-up) as an issue comment that pings `HITL_HANDLE`, and parks awaiting a human reply. No branch is pushed, no PR is opened, and a commit / dirty tree from the agent is treated as a read-only-violation park. Subsequent human comments resume the same locked agent session for a multi-turn conversation; **closing the issue** is the terminal signal — `_handle_question` then flips the label to `done` and tears the worktree down.

## Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `ruff check` and `pytest` on Python 3.12 for every push to `main` and every pull request. Lint rules are configured in [`pyproject.toml`](pyproject.toml) under `[tool.ruff.lint]`.

## Configuration

Common knobs live in [`.env.example`](.env.example). The full reference — required vars, target-repo config, agent role specs, cadence and budgets, parallel processing, auto-merge, observability, and run modes — is in [`docs/configuration.md`](docs/configuration.md).

## Managing multiple repositories

Set `REPOS` to drive several target repositories from one orchestrator process. Worktrees are namespaced under `WORKTREES_DIR/<owner>__<name>/issue-N` so two repos that share an issue number cannot collide. For the entry syntax (including the optional fifth `parallel_limit` field) and the available per-entry fields, see [`docs/configuration.md#multi-repo-repos-syntax`](docs/configuration.md#multi-repo-repos-syntax). For how multi-repo ticks fan out and the per-repo / global concurrency caps, see [`docs/configuration.md#parallel-processing`](docs/configuration.md#parallel-processing).

## License

Licensed under the Apache License, Version 2.0. See [`LICENSE`](LICENSE) for the full text.
