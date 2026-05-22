# Agent orchestration

[![CI](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml)

A GitHub-Issue-driven workflow runner that turns local coding-agent CLIs (`codex`, `claude`) into a hands-off implementer + reviewer loop.

Filing an issue is the only handle: the orchestrator decomposes it if needed, spawns the dev agent in an isolated git worktree, opens a PR, runs a fresh reviewer pass, and (optionally) auto-merges. State lives entirely in the issue itself — one workflow label plus one pinned JSON comment — so progress is observable on github.com and the orchestrator can be restarted without losing context.

It is meant for solo or small-team setups that already have a `codex` or `claude` login and want autonomy without standing up a separate planner, queue, or database.

For design and stage definitions, see [`docs/architecture.md`](docs/architecture.md). For agent roles and command specs, see [`docs/workflow.md`](docs/workflow.md). The implementation roadmap is in [`plans/roadmap.md`](plans/roadmap.md).

## Requirements

### System

- Linux (developed and tested on Ubuntu 24.04 / WSL2)
- Git
- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) — Python package and venv manager (alternative: `python3-venv` + `pip`)

### CLI agents

The orchestrator spawns these as subprocesses on the host. The defaults are `claude` for implementation and decomposition, `codex` for review — so most setups need both authenticated. A single-backend deployment can skip the other; the role mapping is configured via `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` (see [`docs/workflow.md`](docs/workflow.md)).

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

   On first start the orchestrator creates the workflow labels on the repo and begins polling open issues every 60 seconds. For other launch options (single-tick, debug logging) see [`docs/configuration.md#run-modes`](docs/configuration.md#run-modes).

6. **File a bootstrap test issue** to verify the path works end-to-end:

   > **Title:** Add a `hello()` function to the orchestrator package
   > **Body:** Add `hello()` to `orchestrator/__init__.py` returning the string `"hello, world"`. Add `tests/test_hello.py` asserting the return value. Don't change anything else.

   Within ~1 minute the orchestrator should comment "picking this up" and label the issue `decomposing`. The decomposer agent declares the task fits one context, the label flips to `ready` and then `implementing`, and the dev agent runs in a fresh worktree at `../wt-orchestrator/geserdugarov__agent-orchestrator/issue-N`. The orchestrator then pushes the branch, opens a PR, labels the issue `validating`, runs a fresh reviewer session against the diff, and on `VERDICT: APPROVED` moves the issue to `in_review`. With `AUTO_MERGE=on`, the orchestrator then merges the PR itself once GitHub reports it mergeable with green checks, flips the label to `done`, and closes the issue. With `AUTO_MERGE=off` (the default), review and merge the PR manually.

## Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `ruff check` and `pytest` on Python 3.12 for every push to `main` and every pull request. Lint rules are configured in [`pyproject.toml`](pyproject.toml) under `[tool.ruff.lint]`.

## Configuration

Common knobs live in [`.env.example`](.env.example). The full reference — required vars, target-repo config, agent role specs, cadence and budgets, auto-merge, observability, and run modes — is in [`docs/configuration.md`](docs/configuration.md).

## Managing multiple repositories

Set `REPOS` to drive several target repositories from one orchestrator process. Each tick iterates every configured spec; a failure in one repo's tick is logged and skipped so the remaining repos still advance. Worktrees are namespaced under `WORKTREES_DIR/<owner>__<name>/issue-N` so two repos that share an issue number cannot collide. For the entry syntax and the available per-entry fields, see [`docs/configuration.md#multi-repo-repos-syntax`](docs/configuration.md#multi-repo-repos-syntax).

## License

Licensed under the Apache License, Version 2.0. See [`LICENSE`](LICENSE) for the full text.
