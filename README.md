# Agent orchestration

[![CI](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/geserdugarov/agent-orchestrator/actions/workflows/ci.yml)

A GitHub-Issue-driven workflow runner that turns local coding-agent CLIs (`codex`, `claude`) into a hands-off implementer + reviewer loop.

Filing an issue is the only handle: the orchestrator decomposes it if needed, spawns the dev agent in an isolated git worktree, opens a PR, runs a fresh reviewer pass, and pings the HITL handles when the PR is ready for a human to merge.

State lives entirely in the issue itself — one workflow label plus one pinned JSON comment — so progress is observable on github.com and the orchestrator can be restarted without losing context.

It is meant for solo or small-team setups that already have a `codex` or `claude` login and want autonomy without standing up a separate planner, queue, or database.

For the high-level design (process / agent / push model and module map), see [`docs/architecture.md`](docs/architecture.md). For the label-based state machine and per-stage handlers, see [`docs/state-machine.md`](docs/state-machine.md). For agent roles and command specs, see [`docs/workflow.md`](docs/workflow.md). For env vars, run modes, and project CI, see [`docs/configuration.md`](docs/configuration.md). For the audit event log, analytics sink / database, and usage parser, see [`docs/observability.md`](docs/observability.md). For the security checklist and operator-owned GitHub / org settings, see [`docs/security.md`](docs/security.md).

## Requirements

- Linux (developed on Ubuntu 24.04 / WSL2), Git, Python 3.12+, and [`uv`](https://github.com/astral-sh/uv) (or `python3-venv` + `pip`).
- The CLI agents you actually route to must be authenticated on the host. Defaults: [`claude`](https://docs.anthropic.com/en/docs/claude-code) for decomposition + implementation, [`codex`](https://github.com/openai/codex) for review; either can be remapped via `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` (see [`docs/workflow.md`](docs/workflow.md)). They are spawned with `--dangerously-bypass-approvals-and-sandbox` / `--dangerously-skip-permissions`, so the host is the sandbox boundary.
- A GitHub repository to manage plus a fine-grained PAT scoped to it (read/write on Contents, Issues, Pull requests; Metadata read-only). Full rationale and the generation URL are in [`docs/configuration.md`](docs/configuration.md).
- Runtime deps are `PyGithub` and `psycopg[binary]` (the latter for the optional analytics Postgres surface), declared in [`pyproject.toml`](pyproject.toml). Dev tools (`pytest`, `ruff`) live in a `dev` dependency group; the optional analytics dashboard's `streamlit` and `plotly` live in a separate `dashboard` group, so `uv sync --locked` keeps the default install minimal. Exact versions are pinned in [`uv.lock`](uv.lock); CI installs from it.

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

   This creates `.venv/` and installs the exact runtime and dev versions
   recorded in `uv.lock`. For a runtime-only install (no `pytest` / `ruff`),
   add `--no-dev`.

3. **Configure environment**

   ```sh
   cp .env.example .env
   ```

   Edit `.env` and set at minimum:
   - `HITL_HANDLE` — comma-separated GitHub logins (the users the orchestrator @-mentions on questions)
   - `REPO` — leave default unless pointing at a different repo
   - `TARGET_REPO_ROOT` — uncomment and set when `REPO` points at a different repo (path to its local clone)
   - `ALLOWED_ISSUE_AUTHORS` — uncomment and set on any public repo to gate auto-pickup; an empty allowlist lets anyone spend the orchestrator's compute budget. When set, the per-tick sweep also labels open PRs from anyone outside the list with `community_contribution` and @-mentions `HITL_HANDLE` once per PR so a human reviews community-submitted work.

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

   Within ~1 minute the orchestrator should comment "picking this up" and label the issue `decomposing`, then walk it through `implementing` → `validating` → `documenting` → `in_review`, opening a PR along the way. The `documenting` label appears exactly once per reviewer-approval handoff — a single final-docs pass on the squashed head between reviewer approval and `in_review`, with no pre-approval docs run on the dev's commits, fixes, or conflict resolutions. If PR feedback later flips the issue back to `fixing` and the dev pushes a new fix, the next reviewer approval triggers another final-docs pass before the PR is re-advertised as ready for review/merge. The orchestrator is manual-merge-only: a mergeable PR whose current head has completed the reviewer-approved final-docs handoff earns a one-shot HITL ping so you know it is ready, and you click Merge by hand. A formal GitHub APPROVED review on the current head can also satisfy this ping gate. For the full state-machine narrative — including the local verify gate, conflict resolution, and the split-decomposition path — see [`docs/state-machine.md`](docs/state-machine.md).

## Asking the orchestrator a question

Apply the `question` label to any open issue to get a read-only answer instead of an implementation. The orchestrator spawns the configured `DECOMPOSE_AGENT` in the issue's worktree with a read-only prompt and posts the answer as an issue comment that pings `HITL_HANDLE`; subsequent human replies resume the same locked session, and closing the issue is the terminal signal. See [`docs/workflow.md#question-stage--read-only-qa-on-the-question-label`](docs/workflow.md#question-stage--read-only-qa-on-the-question-label) for the full lifecycle and the read-only-violation park reasons.

## Configuration

Basic knobs live in [`.env.example`](.env.example); common advanced overrides and opt-in examples are in [`.env.example.advanced`](.env.example.advanced). The full reference — every setting, every default, required vars, target-repo config, agent role specs, cadence and budgets, parallel processing, in-review behavior, observability, and run modes — is in [`docs/configuration.md`](docs/configuration.md).

## Managing multiple repositories

Set `REPOS` to drive several target repositories from one orchestrator process. Worktrees and PR branches are both namespaced by the sanitized repo slug (`WORKTREES_DIR/<owner>__<name>/issue-N` and `orchestrator/<owner>__<name>/issue-N`) so two repos that share an issue number cannot collide on disk or on the branch ref, even when they share a `target_root`. For the entry syntax (including the optional fifth `parallel_limit` field) and the available per-entry fields, see [`docs/configuration.md#multi-repo-repos-syntax`](docs/configuration.md#multi-repo-repos-syntax). For how multi-repo ticks fan out and the per-repo / global concurrency caps, see [`docs/configuration.md#parallel-processing`](docs/configuration.md#parallel-processing).

## License

Licensed under the Apache License, Version 2.0. See [`LICENSE`](LICENSE) for the full text.
