---
name: develop
description: Project conventions and recurring gotchas for implementer agents working on agent-orchestrator. Use before opening a PR for any change in orchestrator/, tests/, or docs/.
---

# Developer skill — agent-orchestrator

## Commits

- Conventional Commits: `<type>: <subject>` with one of `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.
- Subject line only — no body, no `Co-Authored-By` trailer, no extended description. One `-m` flag.
- Imperative mood, short and specific. Match the style in `git log --oneline -20`.

## Pre-push checklist

Before committing, run each of these and fix what they report:

- `.venv/bin/python -m ruff check orchestrator tests` — recurring CI breakers:
  - **F401** (unused import): if the name is meant to be a re-export from `workflow.py`, alias it with `... as <name>` so ruff treats it as an explicit re-export instead of dead code.
  - **F541** (f-string without placeholders): use a plain string.
  - **F841** (unused local).
  - **E402** (module-level import not at top of file).
- `git diff --check origin/main...HEAD` — catches trailing whitespace and stray blank lines at EOF.
- `.venv/bin/python -m pytest` — full suite must pass. Do not assume any "known" failure is acceptable; if a test fails on your branch, first reproduce it on `origin/main` at the same SHA you branched from, and only then call it out in the PR as a baseline failure with the reproduction steps. Otherwise fix it.

## Refactoring `workflow.py` and the stage modules

The facade pattern in `orchestrator/workflow.py` is load-bearing for tests. Get the boundary right:

- `workflow.py` re-exports stage handlers and cross-module helpers under their original names so `patch.object(workflow, "_foo", ...)` in tests keeps intercepting calls. **Every re-export must be aliased with `as <name>`** — bare `from .stages.implementing import _handle_implementing` will be stripped by ruff F401; `from .stages.implementing import _handle_implementing as _handle_implementing` survives.
- Stage modules call back into the facade via `from .. import workflow as _wf` **at call time**, not at module import. Top-level `from ..workflow import _foo` defeats `patch.object(workflow, "_foo", ...)` because the stage module captures the original reference.
- Stage-private helpers (only used inside one stage module — e.g. `_bump_in_review_watermarks`, `_seed_legacy_in_review_watermarks`, `_emit_conflict_round_incremented`) stay private to that stage module. Do **not** re-export them from `workflow.py`. Re-exports are an intentional surface, not a blanket.
- Preserve the public contract verbatim across a refactor: workflow labels, pinned-state JSON keys, comment marker text, watermark fields, event-emission shape. Live issues already carry these — a "harmless rename" is a migration, not a refactor.

## Tests

- When you move a helper to a new module, either update the test's patch target to the new module boundary, or keep the compatibility alias on `workflow.py` and patch through the facade. Pick one approach per PR and be consistent.
- Stage-handler tests live in `tests/test_workflow_<stage>.py` (`_conflicts`); the validating stage is split into focused `tests/test_workflow_validating_*.py` files (review loops + retry caps, handoff, squash, watermarks, drift, verify, terminal), the in_review stage into focused `tests/test_workflow_in_review_*.py` files (routing, watermarks, filtering, parked, migration, checks, drift, fresh-feedback fixing route), the implementing stage into focused `tests/test_workflow_implementing_*.py` files (fresh runs, PR reuse + conventional-commit helpers, retry / backend behavior, user-content drift, full-spec persistence, terminal merges / closed issues), and the decomposition stage into focused `tests/test_workflow_decomposition_*.py` files (manifest parsing, decomposing/ready/blocked/umbrella stage handlers, child issue creation, hash drift, stale manifest cleanup, child merged-PR finalize). Per-label dispatcher / routing tests live in `tests/test_workflow_<label>_routing.py` (backlog, question, documenting, fixing) and the remaining facade-level helpers (worktree serialization, drain-terminals, finalize-if-pr-merged, stage analytics) live in their own focused modules. Shared fixtures go in `tests/workflow_helpers.py`.
- Prefer extending the in-memory fakes in `tests/fakes.py` over mocking PyGithub directly. New behavior should land with tests in the matching stage file.

## Documentation drift

When you move a handler, helper, or constant, grep for the symbol across these files and update them in the same commit:

- `AGENTS.md` (and its `CLAUDE.md` symlink)
- `docs/architecture.md`
- `docs/state-machine.md`
- `docs/workflow.md`
- the module docstrings at the top of `orchestrator/workflow.py`, `workflow_drift.py`, `workflow_messages.py`, `worktrees.py`, and `orchestrator/stages/*.py`

Be precise about what is and isn't re-exported — overstated claims like "every helper is re-exported" get flagged.

## Out of scope without explicit ask

- Adding dependencies (`pyproject.toml` pins only PyGithub).
- Reformatting unrelated files or churning whitespace.
- "Future-proofing" abstractions for hypothetical features. Implement what the issue asks for and stop.
