---
name: review
description: Review checklist for reviewer agents on agent-orchestrator PRs, plus Rust-specific LLM defect patterns. Use when evaluating a developer-produced branch before approval or change-requests, including Rust PRs.
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
- Squash-on-approval, auto-merge gates, retry budgets, and stale-session detection are easy to break by accident during a move; verify their call paths survive intact.

## Facade and module boundaries

The compatibility surface on `orchestrator/workflow.py` is load-bearing. Confirm:

- New stage helpers that another stage module reaches for are re-exported from `workflow.py`, each aliased `... as <name>`. Stage-private helpers (only used inside one stage module — `_bump_in_review_watermarks`, `_auto_merge_gates_pass`, `_seed_legacy_in_review_watermarks`, `_emit_conflict_round_incremented`, etc.) should **not** be re-exported.
- Stage modules access cross-module helpers via `from .. import workflow as _wf` **at call time**, not via top-level `from ..workflow import _foo` and not via direct imports from `workflow_drift` / `workflow_messages` / `worktrees`. The late-binding pattern preserves `patch.object(workflow, ...)` semantics in tests.
- Test patches target the new module boundary after a move (or the facade alias, consistently). Flag tests that still patch the old location.

## Documentation drift

After any handler or helper move, grep the PR for stale pointers and request fixes in:

- `AGENTS.md` / `CLAUDE.md`
- `docs/architecture.md`
- `docs/state-machine.md`
- `docs/workflow.md`
- `plans/roadmap.md`
- module docstrings at the top of `workflow.py`, `workflow_drift.py`, `workflow_messages.py`, `worktrees.py`, `orchestrator/stages/*.py`

Treat blanket statements like "every helper is re-exported" with suspicion — verify literally against the code.

## Commit hygiene

- Conventional Commits: `<type>: <subject>` only. Reject any commit with a body, a `Co-Authored-By` trailer, or a non-imperative subject. Type must be one of `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.

## Rust gotchas

For PRs against a Rust codebase, the patterns below are LLM-prone defects to call out explicitly:

- **Lifetime laundering.** Reject functions whose returned reference is implicitly tied to a temporary or to an unrelated input — the borrow checker collapses both lifetimes to their intersection and the caller will see a `does not live long enough` error far from the signature. Ask for split lifetimes (`<'a, 'b>`) or owned data (`String`, `Vec<T>`).
- **Sync mutex in async paths.** Flag any `std::sync::Mutex` / `std::sync::RwLock` / `std::mpsc` whose guard or receiver can span an `.await`. Require `tokio::sync::*` (or `parking_lot` only where the critical section is provably sync). Verify dependency major.minor versions are stated, not guessed.
- **Drop in async paths.** A `Drop` impl that performs blocking or async-unsafe work inside an async path is a defect — silent rollback on a failed `commit().await?`, blocking I/O on an executor thread, panics from re-entry. Require an explicit `commit().await?` / `close().await?` / `shutdown().await?`, not implicit drop.
- **Unsafe without `// SAFETY:`.** Every `unsafe` block must carry a `// SAFETY:` comment naming the invariants the caller upholds (alignment, aliasing, lifetime, init state). PRs touching `unsafe` must run `cargo miri test` and the result must be referenced in the description. `ptr::read` / `from_raw_parts` / `transmute` over external bytes are the usual suspects.
- **Cancel-safety unanalyzed.** Reject newly-introduced or modified async fns that don't declare `// cancel-safe` or `// NOT cancel-safe` with justification. Futures used inside `tokio::select!`, `timeout`, or `JoinSet` whose cancellation can leave state half-written (e.g. DB write committed but ACK not sent) need an isolating `tokio::spawn(...).await`.
- **Blanket impls in public APIs.** `impl<T: Trait> MyTrait for T` in a non-sealed public trait is a semver landmine — a downstream `impl MyTrait for Foo` conflicts the moment the upstream adds its own impl. Ask for concrete impls per type or a sealed trait.
- **Large stack values.** Returning `[T; N]` or binding `let x = [T; N];` with large `N` (rule of thumb: > ~16 KiB) belongs on the heap. NRVO is not guaranteed and debug builds will overflow. Expect `Vec<T>`, `vec![..; N].into_boxed_slice()`, or `Box::<[T]>::new_uninit_slice(N)` with explicit initialization and a `// SAFETY:` note around `assume_init()`. Reject `Box::<[T; N]>::new_zeroed()` patterns — they return `Box<MaybeUninit<_>>`, require unsafe `assume_init`, and are only sound when zero is a valid bit pattern for `T`.

CI / lint gates to require for Rust PRs:

- `cargo fmt --all -- --check`
- `cargo clippy --all-targets --all-features -- -D warnings` (prefer `clippy::pedantic` / `clippy::nursery` enabled for new code)
- `cargo test --all-features`
- `cargo miri test` for any PR touching `unsafe`

## Out of scope — push back

- New dependencies (`pyproject.toml` should still pin only PyGithub).
- Reformatting of files outside the change's blast radius.
- Abstractions or generality added for hypothetical future features. The roadmap is the source of truth.
