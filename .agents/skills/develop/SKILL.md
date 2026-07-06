---
name: develop
description: >-
  Project conventions and recurring gotchas for implementer agents working on
  agent-orchestrator. Use before committing any change in orchestrator/,
  tests/, or docs/.
---

# Developer skill — agent-orchestrator

## Commits

- Conventional Commits: `<type>: <subject>` with one of `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.
- Subject line only — no body, no `Co-Authored-By` trailer, no extended description. One `-m` flag.
- Imperative mood, short and specific. Match the style in `git log --oneline -20`.

## Pre-push checklist

Before committing, run each of these and fix what they report:

- `.venv/bin/python -m ruff check orchestrator tests` — recurring CI breakers:
  - **F401** (unused import): if the name is meant to be a re-export from `workflow.py`, alias it with
    `... as <name>` so ruff treats it as an explicit re-export instead of dead code.
  - **F541** (f-string without placeholders): use a plain string.
  - **F841** (unused local).
  - **E402** (module-level import not at top of file).
- `git diff --check origin/main...HEAD` — catches trailing whitespace and stray blank lines at EOF.
- `.venv/bin/python -m pytest` — full suite must pass. Do not assume any "known" failure is
  acceptable; if a test fails on your branch, first reproduce it on `origin/main` at the same SHA
  you branched from, and only then call it out in the PR as a baseline failure with the reproduction
  steps. Otherwise fix it.

## Refactoring `workflow.py` and the stage modules

The facade pattern in `orchestrator/workflow.py` is load-bearing for tests. Get the boundary right:

- `workflow.py` re-exports stage handlers and cross-module helpers under their original names so
  `patch.object(workflow, "_foo", ...)` in tests keeps intercepting calls. **Every re-export must be
  aliased with `as <name>`** — bare `from .stages.implementing import _handle_implementing` will be
  stripped by ruff F401; `from .stages.implementing import _handle_implementing as _handle_implementing`
  survives.
- Stage modules call back into the facade via `from .. import workflow as _wf` **at call time**, not
  at module import. Top-level `from ..workflow import _foo` defeats
  `patch.object(workflow, "_foo", ...)` because the stage module captures the original reference.
- Stage-private helpers (only used inside one stage module — e.g. `_bump_in_review_watermarks`,
  `_seed_legacy_in_review_watermarks`, `_emit_conflict_round_incremented`) stay private to that stage
  module. Do **not** re-export them from `workflow.py`. Re-exports are an intentional surface, not a
  blanket.
- Preserve the public contract verbatim across a refactor: workflow labels, pinned-state JSON keys,
  comment marker text, watermark fields, event-emission shape. Live issues already carry these — a
  "harmless rename" is a migration, not a refactor.

## Tests

- When you move a helper to a new module, either update the test's patch target to the new module
  boundary, or keep the compatibility alias on `workflow.py` and patch through the facade. Pick one
  approach per PR and be consistent.
- Stage-handler tests live in `tests/test_workflow_<stage>.py` (`_conflicts`); the validating stage
  is split into focused `tests/test_workflow_validating_*.py` files (review loops + retry caps,
  handoff, squash, watermarks, drift, verify, terminal), the in_review stage into focused
  `tests/test_workflow_in_review_*.py` files (routing, watermarks, filtering, parked, migration,
  checks, drift, fresh-feedback fixing route), the implementing stage into focused
  `tests/test_workflow_implementing_*.py` files (fresh runs, PR reuse + conventional-commit helpers,
  retry / backend behavior, user-content drift, full-spec persistence, terminal merges / closed
  issues), and the decomposition stage into focused `tests/test_workflow_decomposition_*.py` files
  (manifest parsing, decomposing/ready/blocked/umbrella stage handlers, child issue creation, hash
  drift, stale manifest cleanup, child merged-PR finalize). Per-label dispatcher / routing tests
  live in `tests/test_workflow_<label>_routing.py` (backlog, question, documenting, fixing) and the
  remaining facade-level helpers (worktree serialization, drain-terminals, finalize-if-pr-merged,
  stage analytics) live in their own focused modules. Shared fixtures go in `tests/workflow_helpers.py`.
- Prefer extending the in-memory fakes in `tests/fakes.py` over mocking PyGithub directly. New
  behavior should land with tests in the matching stage file.
- Before finalizing tests, do a redundancy pass:
  - List each added/modified test and the distinct behavior it protects.
  - Merge tests that differ only by input shape or branch case into `pytest.mark.parametrize` cases
    or a small named loop, unless separate setup materially improves clarity.
  - Prefer one focused helper/unit test that covers sibling branches over multiple tests with repeated setup.
  - Keep end-to-end tests only when they exercise an integration boundary that helper tests cannot cover.
  - Ensure assertions observe the behavior being fixed. For resource-usage bugs (over-fetching,
    redundant API calls, retained state), add a direct assertion at the helper/producer level when
    final-result checks could pass for the wrong reason.
  - Remove incidental low-level assertions when existing tests already cover that behavior.

## Comments

Write every comment against the current state of the code, as if it had always been this way:

- Prefer stating why the code below exists — the invariant it protects, the non-local consumer it
  serves, the failure it prevents — over describing what it does. If a comment paraphrases an
  already-readable line (`# cap the page size at what we still need` above `min(remaining, page_size)`)
  or the assert below it, delete it or replace it with the reason.
- Exception: a plain-language summary of genuinely dense code (tricky offset math, a multi-step
  comprehension or iterator chain, subtle ordering constraints) is fine even though it "restates" the
  code. The test is whether the comment is faster to understand than the code below it, not whether
  it repeats it.
- No diff-relative wording: "previously", "the old X", "instead of a `set`", "no longer", "now sized
  to". Those sentences address the reviewer and go stale the moment the PR merges — put the
  before/after story in the commit message or PR description instead.
- Same rule for test docstrings: describe the behavior the test pins down, not the bug or implementation it replaced.

## Documentation drift

When you move a handler, helper, or constant, grep for the symbol across these files and update them in the same commit:

- `AGENTS.md` (and its `CLAUDE.md` symlink)
- `docs/architecture.md`
- `docs/state-machine.md`
- `docs/workflow.md`
- the module docstrings at the top of `orchestrator/workflow.py`, `workflow_drift.py`,
  `workflow_messages.py`, `worktrees.py`, and `orchestrator/stages/*.py`

Be precise about what is and isn't re-exported — overstated claims like "every helper is re-exported" get flagged.

## `plans/` is working notes, not spec

Files under `plans/` (roadmap, design explorations, proposal write-ups) are human working notes, not
authoritative implementation requirements. Implement what the **issue** asks for; do not treat a
`plans/` document — or a numbered "Proposal N" inside one — as a spec to satisfy, and do not cite one
in code, comments, docstrings, or commit messages (that reference outlives the note and goes stale the
moment it is revised or deleted). Leave files under `plans/` untouched unless the current issue
explicitly asks you to edit or remove one.

## Out of scope without explicit ask

- Adding dependencies (`pyproject.toml` pins only PyGithub).
- Reformatting unrelated files or churning whitespace.
- "Future-proofing" abstractions for hypothetical features. Implement what the issue asks for and stop.
