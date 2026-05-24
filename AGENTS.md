# Repository guide for AI agents

This file is the entry point for AI coding agents (Codex, Claude, etc.) working on this repository. `CLAUDE.md` is a symlink to this file, so both conventions resolve to the same content.

## What this project is

`agent-orchestrator` is a GitHub-Issue-driven workflow that watches issues on configured repos, drives them through a label-based state machine, and spawns local CLI agents (`codex`, `claude`) in per-issue git worktrees to implement them and open PRs.

State lives entirely in GitHub (one workflow label + one pinned JSON comment per issue), so the orchestrator process is stateless.

The user-facing overview is in [`README.md`](README.md). The design, architecture, and stage definitions are in [`docs/architecture.md`](docs/architecture.md). The roadmap lives at [`plans/roadmap.md`](plans/roadmap.md).

Read these before making non-trivial changes to the state machine.

## Repository layout

- `orchestrator/` — the Python package
  - `main.py` — entry point; polling loop and `--once` mode
  - `workflow.py` — slim facade: per-repo tick loop, family-aware / fan-out label partitioning, `_process_issue` label dispatcher, `_handle_pickup`, `_park_awaiting_human`, `_run_agent_tracked`. Re-exports the cross-module helpers and the stage entry handlers from the modules below under their original names so `patch.object(workflow, "_foo", ...)` tests keep working. Stage-private helpers that no other module needs (e.g. `_bump_in_review_watermarks`, `_auto_merge_gates_pass`, `_seed_legacy_in_review_watermarks`, `_emit_conflict_round_incremented`) stay private to their stage module and are NOT re-exported.
  - `workflow_drift.py` — user-content drift detection helpers (`_compute_user_content_hash`, `_detect_user_content_change`, `_build_user_content_change_prompt`, `_route_drift_to_decomposing`, …).
  - `workflow_messages.py` — prompt builders (implement / review / decompose / conflict / PR-comment followup), parsers (manifest, review verdict, drift ACK), `_post_issue_comment` / `_post_pr_comment`, orchestrator-comment markers, stderr redaction. The drift / user-content-change prompt builder lives in `workflow_drift.py`, not here.
  - `worktrees.py` — git, branch, and worktree plumbing: branch naming, `_ensure_*_worktree` helpers, hardened git invocations, `_authed_fetch` / `_push_branch`, `_squash_and_force_push`, per-tick `_refresh_base_and_worktrees`, terminal cleanup.
  - `stages/` — per-stage handler bodies. Dispatcher routing still lives in `workflow.py`.
    - `decomposition.py` — `_handle_decomposing`, `_handle_ready`, `_handle_blocked`, `_handle_umbrella`, decomposer-session helpers.
    - `implementing.py` — `_handle_implementing`, dev-session lifecycle, retry budget, `_on_commits` (relabels to `documenting`, NOT directly to `validating`) / `_on_question` / `_on_dirty_worktree`.
    - `documenting.py` — `_handle_documenting`, docs pass on the PR worktree: fetch + ahead/behind guard, dirty-check before any outcome, advance to `validating` after push or `DOCS: NO_CHANGE` verdict.
    - `validating.py` — `_handle_validating`, reviewer-session lifecycle, dev-fix disposition, watermark seeding.
    - `in_review.py` — `_handle_in_review`, PR-watermark ratchet, auto-merge gate, route-to-`fixing` on fresh PR feedback.
    - `fixing.py` — `_handle_fixing` (stub; real fix-loop lands under parent #137). Entered when `_handle_in_review` detects fresh PR feedback and hands the issue off instead of spawning the dev itself.
    - `conflicts.py` — `_handle_resolving_conflict`, conflict-loop helpers.
  - `agents.py` — `codex` / `claude` subprocess invocation
  - `github.py` — `GitHubClient` wrapper around PyGithub
  - `config.py` — env parsing, `REPOS` multi-repo spec, validation
- `tests/` — pytest suite; `fakes.py` holds in-memory fakes used across tests
- `docs/` — workflow and architecture documentation
- `plans/roadmap.md` — implementation roadmap
- `run.sh` — production launcher that auto-restarts after self-modifying merges
- `.env.example` — annotated configuration reference

## Running and testing

The repo targets Python 3.12+. Local development uses [`uv`](https://github.com/astral-sh/uv) for the venv.

```sh
uv venv --python 3.12
uv pip install PyGithub
.venv/bin/python -m pytest          # run the test suite
.venv/bin/python -m orchestrator.main --once  # one polling tick then exit
.venv/bin/python -m orchestrator.main --log-level DEBUG
```

Tests are the primary correctness gate. Add or update tests for any behavioral change to `workflow.py`, the stage modules under `orchestrator/stages/`, the workflow helper modules (`workflow_drift.py`, `workflow_messages.py`, `worktrees.py`), `agents.py`, `github.py`, or `config.py`. Stage-handler tests live in per-stage files (`tests/test_workflow_decomposition.py`, `_implementing.py`, `_documenting.py`, `_validating.py`, `_in_review.py`, `_conflicts.py`) with shared helpers in `tests/workflow_helpers.py`; `tests/test_workflow.py` covers the facade-level dispatcher / tick / pickup behavior (plus the `fixing` stub's dispatcher / sweep / route wiring, which has no dedicated stage test file yet because the real handler is still pending).

## Code conventions

- **License headers.** Every source file (`*.py`, `*.sh`, `pyproject.toml`) starts with:
  ```
  # Copyright 2026 Geser Dugarov
  # SPDX-License-Identifier: Apache-2.0
  ```
  Match the existing style of neighbouring files when adding new ones.
- **Commits.** Conventional Commits: `<type>: <subject>` with types `feat`, `fix`, `chore`, `docs`, `refactor`, `test`. Subject line only — no body, no `Co-Authored-By` trailer. Imperative mood, short.
- **No comments unless the *why* is non-obvious.** The codebase keeps comments sparse; don't narrate what well-named code already says. Hidden constraints, race-window reasoning, or workarounds for specific GitHub quirks are worth a line.
- **Don't introduce dependencies casually.** `pyproject.toml` pins only `PyGithub`. Anything else needs justification.
- **Secrets.** `GITHUB_TOKEN` is deliberately *not* loaded from `.env` (the implementer agent can read the worktree). Tokens live in `~/.config/<owner>/<repo>/token` or the process environment. Don't change this without reading the rationale in `.env.example`.

## When working on the state machine

- Labels and stage names are part of the public contract — issues in flight carry them. Renaming or repurposing a label is a migration, not a refactor.
- The pinned JSON state comment is the only durable per-issue state. Schema changes need to stay backward-compatible with comments already on live issues.
- `workflow.py` is now a slim facade that owns the dispatcher (`_process_issue`), the tick loop, the unlabeled-pickup handler, `_park_awaiting_human`, and `_run_agent_tracked`. Stage handler bodies live under `orchestrator/stages/` — `decomposition.py` for `_handle_decomposing` / `_handle_ready` / `_handle_blocked` / `_handle_umbrella`, `implementing.py` for `_handle_implementing` (relabels to `documenting` after PR open), `documenting.py` for `_handle_documenting`, `validating.py` for `_handle_validating`, `in_review.py` for `_handle_in_review`, `fixing.py` for `_handle_fixing` (stub today; real fix-loop lands under parent #137 — `_handle_in_review` routes fresh PR feedback there), `conflicts.py` for `_handle_resolving_conflict`. Find the right stage module before changing dispatcher routing.
- Stage modules call back into the facade via `from .. import workflow as _wf` at call time so test patches against `workflow.<helper>` keep intercepting calls made from inside a stage handler. Adding a new stage helper that other stages also reach for? Re-export it from `workflow.py` (the existing pattern in `workflow.py` aliases each name with `as <name>`) and import it through `_wf` from the consumer, not directly from `workflow_drift` / `workflow_messages` / `worktrees`.
- Tests for stage handlers live in `tests/test_workflow_<stage>.py` (`_decomposition`, `_implementing`, `_documenting`, `_validating`, `_in_review`, `_conflicts`) with shared helpers in `tests/workflow_helpers.py`. Facade-level dispatcher / tick / pickup tests stay in `tests/test_workflow.py`. All of them exercise stages against in-memory fakes (`tests/fakes.py`). Prefer extending these fakes over mocking PyGithub directly.

## When working on agent invocation

- `codex` is invoked with `--dangerously-bypass-approvals-and-sandbox`; `claude` with `--dangerously-skip-permissions`. The host is the sandbox boundary, which is why secrets are kept off the worktree.
- Agent stdout/stderr handling matters: empty-output and timeout cases are deliberately distinguished (`park_reason`s are tagged transient vs. terminal). Look at `agents.py` and the per-stage `_on_question` / `_on_dirty_worktree` recovery paths (in `orchestrator/stages/implementing.py`, with stage-specific siblings in `validating.py` / `in_review.py` / `conflicts.py`) before adjusting subprocess plumbing. The `fixing` stage is a stub today (parks awaiting human via `_park_awaiting_human`) and does not spawn agents itself yet.

## Out of scope without explicit ask

- Adding new external dependencies, frameworks, or services.
- Reformatting unrelated files or churning whitespace.
- "Future-proofing" abstractions for hypothetical features. The roadmap drives feature work; design for what's on it, not what might be next.
