# GitHub issue-driven workflow runner

[![CI](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml)

This agent orchestrator turns local coding-agent CLIs (`codex`, `claude`) into a hands-off implementer + reviewer loop. File an issue, and the orchestrator decomposes it if needed, spawns the dev agent in an isolated git worktree, opens a PR, runs a fresh reviewer pass, and pings the HITL handles when the PR is ready for a human to merge.

State lives entirely in the issue itself — one workflow label plus one pinned JSON comment — so progress is visible on GitHub and the orchestrator can be restarted without losing context. It is meant for solo or small-team setups that already have a `codex` or `claude` login and want autonomy without standing up a separate planner, queue, or database.

The analytics dashboard shows every tick, agent run, verification, and PR outcome, so you can see what the orchestrator is doing and why. Built-in usage and cost reporting show which repos, issues, models, and workflow stages drive spend.

![Analytics page](./pics/analytics_page.png)

For deeper implementation details, use the references below.

| Topic | Link | Covers |
|---|---|---|
| Architecture | [`docs/architecture.md`](docs/architecture.md) | Process model, agent model, push model, module map |
| State machine | [`docs/state-machine.md`](docs/state-machine.md) | Labels, workflow states, stage handlers |
| Workflow | [`docs/workflow.md`](docs/workflow.md) | Agent roles, command specs, session lifecycle |
| Configuration | [`docs/configuration.md`](docs/configuration.md) | Env vars, run modes, CI |
| Observability | [`docs/observability.md`](docs/observability.md) | Audit log, analytics database, usage parser |
| Security | [`docs/security.md`](docs/security.md) | Checklist, GitHub and org settings |

## Requirements

- Linux host, Git, Python 3.12+, and [`uv`](https://github.com/astral-sh/uv) (or `python3-venv` + `pip`).
- The CLI agents you actually route to must be authenticated on the host. Defaults: [`claude`](https://docs.anthropic.com/en/docs/claude-code) for decomposition + implementation, [`codex`](https://github.com/openai/codex) for review; either can be remapped via `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` (see [`docs/workflow.md`](docs/workflow.md)). They are spawned with `--dangerously-bypass-approvals-and-sandbox` / `--dangerously-skip-permissions`, so the host is the sandbox boundary.
- A GitHub repository to manage plus a fine-grained personal access token scoped to that repository (read/write on Contents, Issues, Pull requests; Metadata read-only). Full rationale and the generation URL are in [`docs/configuration.md`](docs/configuration.md).
- Runtime dependencies are `PyGithub` and `psycopg[binary]` (the latter for the optional analytics Postgres surface), declared in [`pyproject.toml`](pyproject.toml). Dev tools (`pytest`, `ruff`) live in a `dev` dependency group; the optional analytics dashboard's `streamlit` and `plotly` live in a separate `dashboard` group, so `uv sync --locked` keeps the default install minimal. Exact versions are pinned in [`uv.lock`](uv.lock); CI installs from it.

## Quick start

1. **Clone and enter the repo**

   ```sh
   git clone https://github.com/geserdugarov/agent-orchestrator.git
   cd agent-orchestrator
   ```

2. **Install from the lockfile**

   ```sh
   uv sync --locked
   ```

   If `uv` is not installed yet, use the official [installation guide](https://docs.astral.sh/uv/getting-started/installation/).

   This creates `.venv/` and installs the exact runtime and dev versions
   recorded in `uv.lock`. For a runtime-only install (no `pytest` / `ruff`),
   add `--no-dev`.

3. **Configure environment**

   ```sh
   cp .env.example .env
   ```

   Edit `.env` and review these basics:
   - `HITL_HANDLE` — comma-separated GitHub logins (the users the orchestrator @-mentions on questions)
   - `REPO` — leave default unless pointing at a different repo
   - `TARGET_REPO_ROOT` — uncomment and set when `REPO` points at a different repo (path to its local clone)
   - `ALLOWED_ISSUE_AUTHORS` — uncomment and set on any public repo to gate auto-pickup; an empty allowlist lets anyone spend the orchestrator's compute budget and makes prompt-injection attacks easier to attempt. When set, the per-tick sweep also labels open PRs from anyone outside the list (bot accounts such as Dependabot excepted) with `community_contribution` and @-mentions `HITL_HANDLE` once per PR so a human reviews community-submitted work.

   Then store the personal access token **outside** the repo so the implementer agent (which runs
   in a sibling worktree with sandbox bypass enabled) cannot read it via a relative
   path. The default token path is derived from `REPO` (`~/.config/<owner>/<repo>/token`):

   ```sh
   OWNER=geserdugarov
   REPO=agent-orchestrator
   install -d -m 700 "$HOME/.config/$OWNER/$REPO"
   printf %s "$YOUR_PERSONAL_ACCESS_TOKEN" > "$HOME/.config/$OWNER/$REPO/token"
   chmod 600 "$HOME/.config/$OWNER/$REPO/token"
   ```

   Alternatively, export `GITHUB_TOKEN` in the orchestrator's launch environment. Putting the personal access token in `.env` is rejected at startup.

   Basic settings live in [`.env.example`](.env.example); common advanced overrides and opt-in examples are in [`.env.example.advanced`](.env.example.advanced). The full reference — every setting, every default, required vars, target-repo config, agent role specs, cadence and budgets, parallel processing, in-review behavior, observability, and run modes — is in [`docs/configuration.md`](docs/configuration.md).

4. **Verify the agents are authenticated**

   ```sh
   codex --version
   claude --version
   ```

   If a backend is not logged in, run its login flow (`codex login` / `claude /login`). Only the backends you actually route to (the first token of `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT`) need to be authenticated.

   To check configuration of agents see [`docs/configuration.md#agent-roles`](docs/configuration.md#agent-roles). Examples of advanced configuration of models and efforts to use could be found in [`docs/workflow.md#examples`](docs/workflow.md#examples).

5. **Run**

   ```sh
   ./run.sh
   ```

   On first start, the orchestrator creates its workflow and control labels on the repo and begins polling open issues every 60 seconds. For other launch options (single-tick, debug logging) see [`docs/configuration.md#run-modes`](docs/configuration.md#run-modes). For a supervised production deployment (systemd user service, linger, log inspection) see [`docs/configuration.md#running-under-systemd-user-service`](docs/configuration.md#running-under-systemd-user-service).

6. **File a bootstrap test issue** to verify the path works end-to-end:

   > **Title:** Add a `hello()` function to the orchestrator package
   > **Body:** Add `hello()` to `orchestrator/__init__.py` returning the string `"hello, world"`. Add `tests/test_hello.py` asserting the return value. Don't change anything else.

   Within about one minute, the orchestrator should comment "picking this up" and label the issue `decomposing`, then walk it through `implementing` → `validating` → `documenting` → `in_review`, opening a PR along the way. The orchestrator is manual-merge-only: a mergeable PR whose current head has completed the reviewer-approved final-docs handoff earns a one-shot HITL ping so you know it is ready. You can then click Merge by hand, or leave review comments for the orchestrator to address automatically. For the full state-machine narrative — including the local verify gate, conflict resolution, and the split-decomposition path — see [`docs/state-machine.md`](docs/state-machine.md).

## Asking the orchestrator a question

Apply the `question` label to any open issue to get a read-only answer instead of an implementation. The orchestrator spawns the configured `DECOMPOSE_AGENT` in the issue's worktree with a read-only prompt and posts the answer as an issue comment that pings `HITL_HANDLE`; subsequent human replies resume the same locked session, and closing the issue is the terminal signal. See [`docs/workflow.md#question-stage--read-only-qa-on-the-question-label`](docs/workflow.md#question-stage--read-only-qa-on-the-question-label) for the full lifecycle and the read-only-violation park reasons.

## Observability

The workflow state lives on GitHub, but local logs explain what happened between label transitions. `logs/orchestrator.log` records process and per-issue handler activity, while `logs/analytics.jsonl` records stage transitions, handler timing, agent exits, token use, and cost estimates by default. Set `EVENT_LOG_PATH` when you also want an operator-owned audit JSONL file outside the repo.

For dashboard views, point `ANALYTICS_DB_URL` at the optional Postgres service, sync the JSONL sink, then launch Streamlit:

```sh
uv run python -m orchestrator.analytics.sync
uv sync --group dashboard
uv run streamlit run orchestrator/dashboard.py
```

See [`docs/observability.md`](docs/observability.md) for the event schemas, retention behavior, database setup, and dashboard details.

## Managing multiple repositories

Set `REPOS` to drive several target repositories from one orchestrator process. Worktrees and PR branches are both namespaced by the sanitized repo slug (`WORKTREES_DIR/<owner>__<name>/issue-N` and `orchestrator/<owner>__<name>/issue-N`). This allows one local repository to manage multiple remotes, such as public and private repositories that share the same codebase. Identical issue numbers cannot collide on disk or on the branch ref, even when those repositories share a `target_root`.

For the entry syntax (including the optional fifth `parallel_limit` field) and the available per-entry fields, see [`docs/configuration.md#multi-repo-repos-syntax`](docs/configuration.md#multi-repo-repos-syntax). For how multi-repo ticks fan out and the per-repo / global concurrency caps, see [`docs/configuration.md#parallel-processing`](docs/configuration.md#parallel-processing).

## License

Licensed under the Apache License, Version 2.0. See [`LICENSE`](LICENSE) for the full text.

