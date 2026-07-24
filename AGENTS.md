# Repository guide for AI agents

This file is the entry point for AI coding agents (Codex, Claude, etc.) working on this repository. `CLAUDE.md` is a
symlink to this file, so both conventions resolve to the same content.

It is loaded into every agent session — keep it short. For anything beyond a pointer, edit the linked docs instead.

## What this project is

`agent-orchestrator` is a GitHub-Issue-driven workflow that watches issues on configured repos, drives them through a
label-based state machine, and spawns local CLI agents (`codex`, `claude`) in per-issue git worktrees to implement
them and open PRs. State lives entirely in GitHub (one workflow label + one pinned JSON comment per issue), so the
orchestrator process is stateless.

- User-facing overview: [`README.md`](README.md)
- Architecture, module map, process / agent / push model: [`docs/architecture.md`](docs/architecture.md)
- Workflow state machine (labels, per-tick flow, stage handlers): [`docs/state-machine.md`](docs/state-machine.md)
- Agent roles, command specs, session lifecycles: [`docs/workflow.md`](docs/workflow.md)
- Configuration / env vars: [`docs/configuration.md`](docs/configuration.md) is the full reference; basic knobs in
  [`.env.example`](.env.example), common advanced overrides in [`.env.example.advanced`](.env.example.advanced)
- Observability (audit event log, analytics sink / database, usage parser):
  [`docs/observability.md`](docs/observability.md)
- Security checklist and operator-owned controls: [`docs/security.md`](docs/security.md)

## Repository layout

- `orchestrator/` — Python package: tick loop and label-dispatch compatibility facade (`workflow.py`), per-stage lazy
  facades (`stages/`), worktree-subsystem compatibility hub (`worktrees.py`), and the `base_sync.py`,
  `branch_publication.py`, `git_plumbing.py`, `verify.py`, `worktree_lifecycle.py`, `workflow_drift.py`, and
  `workflow_messages.py` subsystem facades. Their immutable `_export_manifest.py` inventories and `_exports.py` hooks
  route historical imports and patch points to responsibility-named private leaves (`_workflow_*`, `_base_sync_*`,
  `_branch_*`, `_git_*`, `_verify_*`, `_worktree_*`, and stage-specific prefixes). The package also contains per-tick
  repo skill-catalog analytics (`skill_catalog.py`), lazy analytics/read and dashboard facades backed by focused
  recording, query, rendering, usage-provider, and trajectory leaves, the process-local scheduler (`scheduler.py`),
  the configuration package (`config/`, whose `__init__.py` binds each setting resolved by the `environment.py`
  `_SettingsResolver`, which draws on the `_dotenv.py` / `credentials.py` / `models.py` / `repositories.py` leaves),
  the agents package (`agents/`, whose `__init__.py` is the stable runner facade over the `models.py` /
  `environment.py` / `sessions.py` / `processes.py` / `runner.py` owners -- `processes.py` owning the shared process
  registry and subprocess-group lifecycle (the facade re-exports only its `terminate_all_running`) and `runner.py`
  owning shared agent dispatch, result assembly, and spawn logging (re-exported as `run_agent`) -- the Codex backend
  in the `backends/` subpackage (`backends/codex.py`), and the retained `_agent_claude.py` / `_agent_api.py` leaves),
  and stable runtime-core facades
  (`main.py`, `github.py`, `state_machine.py`).
  Full module-by-module map: [`docs/architecture.md`](docs/architecture.md#top-level-layout).
- `tests/` — pytest suite. In-memory fakes in `tests/fakes.py`. Stage-handler tests in
  `tests/test_workflow_<stage>*.py` (the validating stage is split across review, controls, drift, handoff, pause,
  squash, verify, and watermark modules in `tests/test_workflow_validating_*.py`, with shared fixtures in
  `tests/validating_*_test_support.py`; the in_review stage is split across
  `tests/test_workflow_in_review_*.py`; the implementing stage across
  `tests/test_workflow_implementing_*.py`, and the decomposition, question, and documenting stages across their
  respective focused modules, with shared fixtures in `tests/decomposition*_support.py`,
  `tests/question_*_support.py`, and `tests/documenting_*_support.py`; the resolving-conflict stage is split across
  `tests/test_workflow_conflicts_*.py` — infrastructure tests (`_authed_fetch`, `_target_fetch`,
  `_worktree_restore`, `_event_emission`, `_git_identity`, `_list_pollable`, `_routing`) plus the
  `_handle_resolving_conflict` handler scenarios in focused modules (`_clean_rebase` for clean rebase routing,
  `_agent` for agent execution, `_resume` for awaiting-human resume paths, `_dirty` for dirty / rebase-in-progress
  parking, `_recovery` for recovery pushes, `_diverged` for stale / diverged worktree handling, `_publish` for
  already-rebased force-publish scenarios, `_publish_guard` for the publish-guard probe unit tests, `_drift` for
  hash-drift resume behavior), with resume fixtures in `tests/conflict_resume_test_support.py`); scheduler, base-sync,
  cleanup, and worktree-subsystem tests are split across
  `tests/test_scheduler_*.py`, `tests/test_workflow_scheduler_*.py`, `tests/test_workflow_base_sync_*.py`,
  `tests/test_workflow_cleanup*.py`, and `tests/test_workflow_worktree_*.py`, with subsystem-specific support in
  `tests/scheduler_*.py`, `tests/base_sync_*.py`, and `tests/worktree_*.py`; other facade-level helper tests include
  (`tests/test_workflow_verdict_parsing.py`, `tests/test_workflow_prompt_redaction.py`,
  `tests/test_workflow_branch_publication*.py`, `tests/test_workflow_pickup.py`,
  `tests/test_workflow_event_emission.py`, `tests/test_workflow_agent_analytics.py`,
  `tests/test_workflow_model_extraction.py`, `tests/test_workflow_pr_lifecycle.py`,
  `tests/test_workflow_list_pollable.py`, `tests/test_workflow_tick_parallel.py`,
  `tests/test_workflow_drift.py`,
  `tests/test_workflow_backlog_routing.py`, `tests/test_workflow_question_routing.py`,
  `tests/test_workflow_documenting_routing.py`, `tests/test_workflow_fixing_routing.py`,
  `tests/test_workflow_in_review_fresh_feedback.py`, `tests/test_workflow_community_contribution.py`,
  `tests/test_workflow_stage_analytics.py`, `tests/test_workflow_finalize_pr_merged.py`,
  `tests/test_workflow_drain_terminals.py`); shared helpers in `tests/workflow_helpers.py`. Configuration-package
  tests live in `tests/config/` and agent-package owner / import-cycle tests in `tests/agents/`.
- `docs/` — architecture, workflow, and configuration references.
- `run.sh` — production launcher that auto-restarts after self-modifying merges.
- `.env.example` / `.env.example.advanced` — basic and advanced configuration templates; full reference is in
  [`docs/configuration.md`](docs/configuration.md).

## Running and testing

The repo targets Python 3.12+. Local development uses [`uv`](https://github.com/astral-sh/uv) and installs from the
lockfile.

```sh
uv sync --locked                              # creates .venv/ and installs runtime + dev deps from uv.lock
uv run ruff check orchestrator tests          # run Ruff
uv run flake8 orchestrator tests --select=WPS # run wemake-python-styleguide
uv run pytest tests                           # run the test suite
uv run python -m orchestrator.main --once     # one polling tick then exit
uv run python -m orchestrator.main --log-level DEBUG
```

`analytics-db/data/` is the operator-owned Docker bind mount holding the local analytics Postgres volume. It is
runtime state, not source: **never traverse, read, modify, permission-repair, delete, or re-run any command against it
with elevated privileges.** If a tool reports it as unreadable, that is expected — target `tests` explicitly (the
default `pytest` config already ignores the directory) rather than escalating access.

Dev tools (`pytest`, `ruff`, and `wemake-python-styleguide`, which supplies the WPS Flake8 plugin) live in the `dev`
dependency group in `pyproject.toml`; exact versions are pinned in `uv.lock`. CI installs the same set via
`uv sync --locked`.

Tests are the primary correctness gate. Add or update tests for any behavioral change. Prefer extending the in-memory
fakes in `tests/fakes.py` over mocking PyGithub directly.

## Code conventions

- **License headers.** Every source file (`*.py`, `*.sh`, `pyproject.toml`) starts with:
  ```
  # Copyright 2026 Geser Dugarov
  # SPDX-License-Identifier: Apache-2.0
  ```
- **Commits.** Conventional Commits: `<type>: <subject>` with types `feat`, `fix`, `chore`, `docs`, `refactor`,
  `test`. Subject line only — no body, no `Co-Authored-By` trailer. Imperative mood, short.
- **Comments.** Sparse — only when the *why* is non-obvious (hidden constraint, race window, GitHub quirk).
- **Dependencies.** `pyproject.toml` pins `PyGithub` and `psycopg[binary]` as runtime deps; `pytest`, `ruff`, and
  `wemake-python-styleguide` live in the `dev` group; the analytics dashboard's `streamlit` and `plotly` live in the
  separate `dashboard` group so the default `uv sync --locked` stays minimal. `uv.lock` is the source of truth for
  exact versions and is committed — regenerate it (`uv lock`) whenever `pyproject.toml` changes. Anything else needs
  justification.
- **Secrets.** `GITHUB_TOKEN` is deliberately *not* loaded from `.env`. Tokens live in
  `~/.config/<owner>/<repo>/token` or the process environment. Rationale:
  [`docs/configuration.md#github-pat`](docs/configuration.md#github-pat).

## Out of scope without explicit ask

- New external dependencies, frameworks, or services.
- Reformatting unrelated files or churning whitespace.
- "Future-proofing" abstractions for hypothetical features. Implement what the issue asks for and stop.

When touching the state machine, agent invocation, or stage handlers, read
[`docs/state-machine.md`](docs/state-machine.md) and [`docs/workflow.md`](docs/workflow.md) first — labels and the
pinned-state JSON schema are part of the public contract that live issues already carry.
