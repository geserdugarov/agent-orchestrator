---
name: develop
description: >-
  Project conventions and recurring gotchas for implementer agents working on
  agent-orchestrator. Use before committing any change in orchestrator/,
  tests/, or docs/.
---

# Developer skill — agent-orchestrator

## Environment and commands

The repo targets Python 3.12+ and installs from the lockfile with [`uv`](https://github.com/astral-sh/uv):

```sh
uv sync --locked                              # creates .venv/ and installs runtime + dev deps from uv.lock
uv run ruff check orchestrator tests          # run Ruff
uv run flake8 orchestrator tests --select=WPS # run wemake-python-styleguide
uv run pytest tests                           # run the test suite
uv run python -m orchestrator --once          # one polling tick then exit
uv run python -m orchestrator --log-level DEBUG
```

## License headers

Every source file (`*.py`, `*.sh`, `pyproject.toml`) starts with:

```
# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
```

## Commits

- Conventional Commits: `<type>: <subject>` with one of `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.
- Subject line only — no body, no `Co-Authored-By` trailer, no extended description. One `-m` flag.
- Imperative mood, short and specific. Match the style in `git log --oneline -20`.

## Pre-push checklist

Before committing, run each of these and fix what they report:

- `.venv/bin/python -m ruff check orchestrator tests` — recurring CI breakers:
  - **F401** (unused import): if the name is meant to be a re-export from a package facade that binds its
    surface with imports (`orchestrator/agents/`, `github/`, `scheduler/`, `observability/usage/`), alias it with
    `... as <name>` so ruff treats it as an explicit re-export instead of dead code. The `workflow` facade needs
    none of this: it resolves its whole inventory lazily off `_workflow_export_manifest` and binds nothing.
  - **F541** (f-string without placeholders): use a plain string.
  - **F841** (unused local).
  - **E402** (module-level import not at top of file).
- `uv run flake8 orchestrator tests --select=WPS` — all WPS naming, complexity, consistency, bug-prevention,
  refactoring, and OOP rules must pass.
- `git diff --check origin/main...HEAD` — catches trailing whitespace and stray blank lines at EOF.
- `.venv/bin/python -m pytest` — full suite must pass. Do not assume any "known" failure is
  acceptable; if a test fails on your branch, first reproduce it on `origin/main` at the same SHA
  you branched from, and only then call it out in the PR as a baseline failure with the reproduction
  steps. Otherwise fix it.

## Refactoring the `workflow` facade and the stage modules

`orchestrator/workflow/__init__.py` is a compatibility surface for callers outside the tree, not a
call path inside it. Get the boundary right:

- The facade resolves stage handlers and cross-module helpers under their original names off the
  owner that defines each one, so a historical `patch.object(workflow, "_foo", ...)` still resolves.
  It intercepts nothing in-tree, though: every in-repo caller names its owner, so a mock left on the
  facade lets the real helper run.
- Stage modules import the owner they borrow from at module scope —
  `from orchestrator.git.worktrees import paths as _worktree_paths`,
  `from orchestrator.workflow.engine import guards as _guards` — and call through that alias. Never
  reintroduce the `from orchestrator import workflow as _wf` call-time hop.
- Tests patch the owner. `tests/workflow_git_owners.py` records which git module defines each seam
  (`GIT_SEAM_OWNERS`, `seam_patch`), `tests/workflow_patch_runner.py` resolves every hermetic mock
  through that table plus the agent runner, and `tests/workflow_owner_boundaries.py` carries the
  `OwnerBoundaryMixin` a `test_owner_boundaries.py` module pins a borrowed owner with — the owner mock
  has to answer and the facade guard has to stay untouched.
- Stage-private helpers (only used inside one stage module — e.g. `_bump_in_review_watermarks`,
  `_seed_legacy_in_review_watermarks`, `_emit_conflict_round_incremented`) stay private to that stage
  module. Do **not** re-export them from the facade. Re-exports are an intentional surface, not a
  blanket.
- Preserve the public contract verbatim across a refactor: workflow labels, pinned-state JSON keys,
  comment marker text, watermark fields, event-emission shape. Live issues already carry these — a
  "harmless rename" is a migration, not a refactor.

## Tests

- When you move a helper to a new module, move the test's patch target to that owner with it. Patching
  through `orchestrator.workflow` is not an alternative: the facade resolves the name for callers outside the
  tree, so a mock left there intercepts nothing an in-repo caller runs.
- Tests mirror the runtime layout: a module under `orchestrator/<package>/` is covered by `tests/<package>/`, and
  stage-handler tests live beside their owners under `tests/workflow/stages/<stage>/`. Put a new test in the module
  that already covers the behavior's owner; add a new module only when none does, and name it after the behavior it
  protects rather than after the symbol it calls.
- Each stage package's tests are already split into focused modules — routing, the outcomes one tick can reach, the
  parks, drift, live pause, borrowed-owner boundaries — with the fixtures they share in a `*_test_support.py` beside
  them. Follow the split that is there instead of growing one omnibus module, and put a stage's shared fixtures in
  its own support module rather than in a sibling stage's.
- Each tests package carries its package-level guards (clean-process import, import-cycle / layering direction,
  public surface, and the forwarding checks a migrated owner owes) in its own `test_imports.py`.
- Facade-level helpers that belong to no single stage get their own focused module under `tests/workflow/`; fixtures
  shared across the flat workflow tests go in `tests/workflow_helpers.py`.
- Prefer extending the in-memory fakes in `tests/support/github/` (reached through the
  `tests/fakes.py` bridge) over mocking PyGithub directly. New behavior should land with tests in
  the matching stage file.
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

- `docs/architecture.md` — the module-by-module inventory lives here and nowhere else
- `docs/state-machine.md`
- `docs/workflow.md`
- the module docstrings at the top of the facade the symbol was reached through and of the owners it moved between

`AGENTS.md` (and its `CLAUDE.md` symlink) is deliberately not on that list. It carries no module, owner, or test
inventory, so a routine symbol or module move must leave it alone. Update it only when repository-wide agent
instructions, safety rules, or documentation routing change.

Be precise about what is and isn't re-exported — overstated claims like "every helper is re-exported" get flagged.

## `plans/` is working notes, not spec

Files under `plans/` (roadmap, design explorations, proposal write-ups) are human working notes, not
authoritative implementation requirements. Implement what the **issue** asks for; do not treat a
`plans/` document — or a numbered "Proposal N" inside one — as a spec to satisfy, and do not cite one
in code, comments, docstrings, or commit messages (that reference outlives the note and goes stale the
moment it is revised or deleted). Leave files under `plans/` untouched unless the current issue
explicitly asks you to edit or remove one.

## Dependencies

`pyproject.toml` pins `PyGithub` and `psycopg[binary]` as runtime deps; `pytest`, `ruff`, and
`wemake-python-styleguide` live in the `dev` group; the analytics dashboard's `streamlit` and `plotly` live in the
separate `dashboard` group so the default `uv sync --locked` stays minimal. `uv.lock` is the source of truth for
exact versions and is committed — regenerate it (`uv lock`) whenever `pyproject.toml` changes. Anything else needs
justification.

## Out of scope without explicit ask

- Adding dependencies that the current issue did not request.
- Reformatting unrelated files or churning whitespace.
- "Future-proofing" abstractions for hypothetical features. Implement what the issue asks for and stop.
