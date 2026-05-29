# Repository guide for AI agents

This file is the entry point for AI coding agents (Codex, Claude, etc.) working on this repository. `CLAUDE.md` is a symlink to this file, so both conventions resolve to the same content.

It is loaded into every agent session — keep it short. For anything beyond a pointer, edit the linked docs instead.

## What this project is

`agent-orchestrator` is a GitHub-Issue-driven workflow that watches issues on configured repos, drives them through a label-based state machine, and spawns local CLI agents (`codex`, `claude`) in per-issue git worktrees to implement them and open PRs. State lives entirely in GitHub (one workflow label + one pinned JSON comment per issue), so the orchestrator process is stateless.

- User-facing overview: [`README.md`](README.md)
- Architecture, stages, module map: [`docs/architecture.md`](docs/architecture.md)
- Agent roles, command specs, session lifecycles: [`docs/workflow.md`](docs/workflow.md)
- Configuration / env vars: [`docs/configuration.md`](docs/configuration.md) (and [`.env.example`](.env.example))
- Security checklist and operator-owned controls: [`docs/security.md`](docs/security.md)
- Roadmap: [`plans/roadmap.md`](plans/roadmap.md)

## Repository layout

- `orchestrator/` — Python package: tick loop and entry point, label dispatcher / facade (`workflow.py`), per-stage handlers (`stages/`), git and worktree plumbing (`worktrees.py`), drift detection (`workflow_drift.py`), prompt builders and parsers (`workflow_messages.py`), agent subprocess runner (`agents.py`), GitHub client (`github.py`), config (`config.py`). Full module-by-module map: [`docs/architecture.md`](docs/architecture.md#top-level-layout).
- `tests/` — pytest suite. In-memory fakes in `tests/fakes.py`. Stage-handler tests in `tests/test_workflow_<stage>.py`; facade-level dispatcher / tick / pickup tests in `tests/test_workflow.py`; shared helpers in `tests/workflow_helpers.py`.
- `docs/` — architecture, workflow, and configuration references.
- `plans/roadmap.md` — implementation roadmap.
- `run.sh` — production launcher that auto-restarts after self-modifying merges.
- `.env.example` — annotated configuration reference.

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
- **Dependencies.** `pyproject.toml` pins only `PyGithub` as a runtime dep; `pytest` and `ruff` live in the `dev` group. `uv.lock` is the source of truth for exact versions and is committed — regenerate it (`uv lock`) whenever `pyproject.toml` changes. Anything else needs justification.
- **Secrets.** `GITHUB_TOKEN` is deliberately *not* loaded from `.env`. Tokens live in `~/.config/<owner>/<repo>/token` or the process environment. Rationale: [`.env.example`](.env.example).

## Out of scope without explicit ask

- New external dependencies, frameworks, or services.
- Reformatting unrelated files or churning whitespace.
- "Future-proofing" abstractions for hypothetical features. The roadmap drives feature work.

When touching the state machine, agent invocation, or stage handlers, read [`docs/architecture.md`](docs/architecture.md) and [`docs/workflow.md`](docs/workflow.md) first — labels and the pinned-state JSON schema are part of the public contract that live issues already carry.
