---
name: review
description: Review checklist for reviewer agents on agent-orchestrator PRs. Use when evaluating a developer-produced branch before approval or change-requests.
---

# Reviewer skill — agent-orchestrator

## CI / lint

Reject (or request fixes) if any of these are red:

- `ruff check orchestrator tests`. Common offenders to look for explicitly:
  - **F401** — unused import on the facade. If the import is intended as a re-export from `workflow.py`, it must be aliased `from X import Y as Y`. A bare import will not survive ruff.
  - **F541** — f-strings without placeholders, typically in newly-added test files.
  - **F841** — unused local in tests.
  - **E402** — import after non-import code.
- `git diff --check origin/main...HEAD` — trailing whitespace and blank lines at EOF. Check it even if everything else looks clean.
- Full `pytest` run is referenced in the PR description and passes end-to-end. Reject "known failure" hand-waves; if the PR claims a baseline failure, the description must include a reproduction on `origin/main` at the branch point. Otherwise the developer must fix it.

## Behavior preservation

For any refactor:

- Workflow labels, pinned-state JSON keys, comment marker text, watermark fields, and event-emission shape must match `main` exactly. Issues already in flight depend on these — a rename is a migration, not a refactor.
- Spot-check that moved code still routes through the same auth / fetch / push / retry helpers. A refactor is not allowed to silently change side effects.
- Squash-on-approval, the in_review HITL ready-ping gates (mergeable + approved + no standing CHANGES_REQUESTED), retry budgets, and stale-session detection are easy to break by accident during a move; verify their call paths survive intact.

## Facade and module boundaries

The compatibility surface on `orchestrator/workflow.py` is load-bearing. Confirm:

- New stage helpers that another stage module reaches for are re-exported from `workflow.py`, each aliased `... as <name>`. Stage-private helpers (only used inside one stage module — `_bump_in_review_watermarks`, `_seed_legacy_in_review_watermarks`, `_emit_conflict_round_incremented`, etc.) should **not** be re-exported.
- Stage modules access cross-module helpers via `from .. import workflow as _wf` **at call time**, not via top-level `from ..workflow import _foo` and not via direct imports from `workflow_drift` / `workflow_messages` / `worktrees`. The late-binding pattern preserves `patch.object(workflow, ...)` semantics in tests.
- Test patches target the new module boundary after a move (or the facade alias, consistently). Flag tests that still patch the old location.

## Documentation drift

After any handler or helper move, grep the PR for stale pointers and request fixes in:

- `AGENTS.md` / `CLAUDE.md`
- `docs/architecture.md`
- `docs/state-machine.md`
- `docs/workflow.md`
- module docstrings at the top of `workflow.py`, `workflow_drift.py`, `workflow_messages.py`, `worktrees.py`, `orchestrator/stages/*.py`

Treat blanket statements like "every helper is re-exported" with suspicion — verify literally against the code.

## Commit hygiene

- Conventional Commits: `<type>: <subject>` only. Reject any commit with a body, a `Co-Authored-By` trailer, or a non-imperative subject. Type must be one of `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.

## Out of scope — push back

- New dependencies (`pyproject.toml` should still pin only PyGithub).
- Reformatting of files outside the change's blast radius.
- Abstractions or generality added for hypothetical future features. The issue's stated scope is the source of truth.
