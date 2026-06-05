# Repository guide for AI agents

This file is the entry point for AI coding agents (Codex, Claude, etc.) working on this repository. `CLAUDE.md` is a symlink to this file, so both conventions resolve to the same content.

It is loaded into every agent session — keep it short. For anything beyond a pointer, edit the linked docs instead.

## What this project is

`agent-orchestrator` is a GitHub-Issue-driven workflow that watches issues on configured repos, drives them through a label-based state machine, and spawns local CLI agents (`codex`, `claude`) in per-issue git worktrees to implement them and open PRs. State lives entirely in GitHub (one workflow label + one pinned JSON comment per issue), so the orchestrator process is stateless.

- User-facing overview: [`README.md`](README.md)
- Architecture, module map, process / agent / push model: [`docs/architecture.md`](docs/architecture.md)
- Workflow state machine (labels, per-tick flow, stage handlers): [`docs/state-machine.md`](docs/state-machine.md)
- Agent roles, command specs, session lifecycles: [`docs/workflow.md`](docs/workflow.md)
- Configuration / env vars: [`docs/configuration.md`](docs/configuration.md) is the full reference; basic knobs in [`.env.example`](.env.example), common advanced overrides in [`.env.example.advanced`](.env.example.advanced)
- Observability (audit event log, analytics sink / database, usage parser): [`docs/observability.md`](docs/observability.md)
- Security checklist and operator-owned controls: [`docs/security.md`](docs/security.md)

## Repository layout

- `orchestrator/` — Python package: tick loop and entry point, label dispatcher / facade (`workflow.py`), per-stage handlers (`stages/`), worktree-subsystem compatibility re-export hub (`worktrees.py`), worktree naming / layout / creation / cleanup helpers (`worktree_lifecycle.py`, re-exported from `worktrees.py`), hardened git subprocess layer (`git_plumbing.py`, re-exported from `worktrees.py`), local-verify runner (`verify.py`, re-exported from `worktrees.py`), PR branch publication helpers (`branch_publication.py`, re-exported from `worktrees.py`), per-tick base refresh and rebase routing (`base_sync.py`, re-exported from `worktrees.py`), process-local issue work scheduler (`scheduler.py`), drift detection (`workflow_drift.py`), prompt builders and parsers (`workflow_messages.py`), agent subprocess runner (`agents.py`), GitHub client (`github.py`), typed workflow states + transition guard (`state_machine.py`), config (`config.py`). Full module-by-module map: [`docs/architecture.md`](docs/architecture.md#top-level-layout).
- `tests/` — pytest suite. In-memory fakes in `tests/fakes.py`. Stage-handler tests in `tests/test_workflow_<stage>.py` (the validating stage is split across `tests/test_workflow_validating_*.py`, the in_review stage across `tests/test_workflow_in_review_*.py`, the implementing stage across `tests/test_workflow_implementing_*.py`, and the decomposition stage across `tests/test_workflow_decomposition_*.py`); facade-level helper tests are split into focused modules (`tests/test_workflow_verdict_parsing.py`, `tests/test_workflow_prompt_redaction.py`, `tests/test_workflow_branch_publication.py`, `tests/test_workflow_pickup.py`, `tests/test_workflow_event_emission.py`, `tests/test_workflow_agent_analytics.py`, `tests/test_workflow_model_extraction.py`, `tests/test_workflow_pr_lifecycle.py`, `tests/test_workflow_list_pollable.py`, `tests/test_workflow_worktree_paths.py`, `tests/test_workflow_cleanup.py`, `tests/test_workflow_base_sync_unit.py`, `tests/test_workflow_tick_parallel.py`, `tests/test_workflow_scheduler_routing.py`, `tests/test_workflow_base_sync_real_git.py`, `tests/test_workflow_drift.py`, `tests/test_workflow_worktree_serialization.py`, `tests/test_workflow_backlog_routing.py`, `tests/test_workflow_question_routing.py`, `tests/test_workflow_documenting_routing.py`, `tests/test_workflow_fixing_routing.py`, `tests/test_workflow_in_review_fresh_feedback.py`, `tests/test_workflow_community_contribution.py`, `tests/test_workflow_stage_analytics.py`, `tests/test_workflow_finalize_pr_merged.py`, `tests/test_workflow_drain_terminals.py`); shared helpers in `tests/workflow_helpers.py`.
- `docs/` — architecture, workflow, and configuration references.
- `run.sh` — production launcher that auto-restarts after self-modifying merges.
- `.env.example` / `.env.example.advanced` — basic and advanced configuration templates; full reference is in [`docs/configuration.md`](docs/configuration.md).

## Running and testing

The repo targets Python 3.12+. Local development uses [`uv`](https://github.com/astral-sh/uv) and installs from the lockfile.

```sh
uv sync --locked                              # creates .venv/ and installs runtime + dev deps from uv.lock
uv run pytest                                 # run the test suite
uv run python -m orchestrator.main --once     # one polling tick then exit
uv run python -m orchestrator.main --log-level DEBUG
```

Dev tools (`pytest`, `ruff`) live in the `dev` dependency group in `pyproject.toml`; exact versions are pinned in `uv.lock`. CI installs the same set via `uv sync --locked`.

Tests are the primary correctness gate. Add or update tests for any behavioral change. Prefer extending the in-memory fakes in `tests/fakes.py` over mocking PyGithub directly.

## Code conventions

- **License headers.** Every source file (`*.py`, `*.sh`, `pyproject.toml`) starts with:
  ```
  # Copyright 2026 Geser Dugarov
  # SPDX-License-Identifier: Apache-2.0
  ```
- **Commits.** Conventional Commits: `<type>: <subject>` with types `feat`, `fix`, `chore`, `docs`, `refactor`, `test`. Subject line only — no body, no `Co-Authored-By` trailer. Imperative mood, short.
- **Comments.** Sparse — only when the *why* is non-obvious (hidden constraint, race window, GitHub quirk).
- **Dependencies.** `pyproject.toml` pins `PyGithub` and `psycopg[binary]` as runtime deps; `pytest` and `ruff` live in the `dev` group; the analytics dashboard's `streamlit` and `plotly` live in the separate `dashboard` group so the default `uv sync --locked` stays minimal. `uv.lock` is the source of truth for exact versions and is committed — regenerate it (`uv lock`) whenever `pyproject.toml` changes. Anything else needs justification.
- **Secrets.** `GITHUB_TOKEN` is deliberately *not* loaded from `.env`. Tokens live in `~/.config/<owner>/<repo>/token` or the process environment. Rationale: [`docs/configuration.md#github-pat`](docs/configuration.md#github-pat).

## Out of scope without explicit ask

- New external dependencies, frameworks, or services.
- Reformatting unrelated files or churning whitespace.
- "Future-proofing" abstractions for hypothetical features. Implement what the issue asks for and stop.

When touching the state machine, agent invocation, or stage handlers, read [`docs/state-machine.md`](docs/state-machine.md) and [`docs/workflow.md`](docs/workflow.md) first — labels and the pinned-state JSON schema are part of the public contract that live issues already carry.
