---
name: develop
description: Project conventions and recurring gotchas for implementer agents working on agent-orchestrator, plus Rust-specific LLM gotchas. Use before opening a PR for any change in orchestrator/, tests/, docs/, or plans/, and for any change in a Rust codebase.
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
- Stage-private helpers (only used inside one stage module — e.g. `_bump_in_review_watermarks`, `_auto_merge_gates_pass`, `_seed_legacy_in_review_watermarks`, `_emit_conflict_round_incremented`) stay private to that stage module. Do **not** re-export them from `workflow.py`. Re-exports are an intentional surface, not a blanket.
- Preserve the public contract verbatim across a refactor: workflow labels, pinned-state JSON keys, comment marker text, watermark fields, event-emission shape. Live issues already carry these — a "harmless rename" is a migration, not a refactor.

## Tests

- When you move a helper to a new module, either update the test's patch target to the new module boundary, or keep the compatibility alias on `workflow.py` and patch through the facade. Pick one approach per PR and be consistent.
- Stage-handler tests live in `tests/test_workflow_<stage>.py` (`_decomposition`, `_implementing`, `_validating`, `_in_review`, `_conflicts`). Facade-level dispatcher / tick / pickup tests stay in `tests/test_workflow.py`. Shared fixtures go in `tests/workflow_helpers.py`.
- Prefer extending the in-memory fakes in `tests/fakes.py` over mocking PyGithub directly. New behavior should land with tests in the matching stage file.

## Documentation drift

When you move a handler, helper, or constant, grep for the symbol across these files and update them in the same commit:

- `AGENTS.md` (and its `CLAUDE.md` symlink)
- `docs/architecture.md`
- `docs/state-machine.md`
- `docs/workflow.md`
- `plans/roadmap.md`
- the module docstrings at the top of `orchestrator/workflow.py`, `workflow_drift.py`, `workflow_messages.py`, `worktrees.py`, and `orchestrator/stages/*.py`

Be precise about what is and isn't re-exported — overstated claims like "every helper is re-exported" get flagged.

## Rust gotchas

When the issue is for a Rust codebase, watch for these patterns LLMs reliably get wrong:

- **Lifetime laundering.** Don't return references whose lifetime is implicitly bound to a temporary or to an unrelated input — a cache keyed by `&'a str` collapses to an empty lifetime the moment any of its inputs goes out of scope. If two references have independent lifetimes, give them independent parameters (`<'a, 'b>`); if the borrow checker still won't let it pass, store owned data (`String`, `Vec<T>`) instead of `&str` / `&[T]`. When asking for help, show the calling code — lifetime errors usually live at the call site, not the signature.
- **Sync mutex inside async.** Use `tokio::sync::Mutex` whenever a guard may be held across an `.await`. `std::sync::Mutex` blocks the worker thread and deadlocks under tokio. Same rule for `RwLock`, channels, and `Notify` — pick the async-aware variant. State `tokio` / `axum` / `sqlx` major.minor up front in prompts so the LLM doesn't mix `std` and tokio primitives.
- **Drop order / RAII in async.** `Drop` runs in reverse declaration order and cannot be `async`. Transactions, file handles, and pooled connections must be closed explicitly (`tx.commit().await?`, `handle.shutdown().await?`) — never lean on implicit drop in async paths, which silently rolls back, blocks the executor, or panics.
- **Unsafe without invariants.** Every `unsafe { ... }` block needs a `// SAFETY:` comment naming the invariants the caller is upholding (alignment, aliasing, lifetime, init state). `ptr::read` on a network buffer without an alignment guarantee is UB even when the test passes on x86. Run `cargo miri test` against any change touching `unsafe`; bare `cargo test` won't catch UB that happens to compile.
- **Cancel-safety in async.** Any future passed to `tokio::select!`, `timeout`, or `JoinSet` can be dropped mid-`.await` — a cancellation between "write to DB" and "send ACK" duplicates work on retry. Annotate every async fn `// cancel-safe` or `// NOT cancel-safe` with one-line justification, and isolate non-cancel-safe critical sections behind `tokio::spawn(...).await` so the join, not the inner future, is what gets cancelled.
- **Blanket impls in public APIs.** `impl<T: Trait> Mine for T` is a semver landmine: a downstream `impl Mine for Foo` conflicts the moment your crate adds its own impl, and the breakage doesn't show up until someone else upgrades. Reserve blanket impls for sealed traits; otherwise write concrete impls per type.
- **Large values on the stack.** `fn f() -> [u8; 1 << 20]` and `let buf = [0u8; 1 << 20];` overflow in debug builds and aren't reliably elided by NRVO. Allocate on the heap instead: `vec![0u8; N].into_boxed_slice()` for a zeroed `Box<[u8]>`, or `Box::<[T]>::new_uninit_slice(N)` followed by per-element initialization and an `unsafe { slice.assume_init() }` (with a `// SAFETY:` note) when the element type doesn't have a meaningful zero. Don't reach for `Box::<[T; N]>::new_zeroed()` — it yields `Box<MaybeUninit<[T; N]>>`, needs unsafe `assume_init`, and is UB unless all-zero is a valid bit pattern for `T`.

Before committing Rust changes:

- `cargo fmt --all -- --check`
- `cargo clippy --all-targets --all-features -- -D warnings` (consider enabling `clippy::pedantic` / `clippy::nursery` for newly-added code)
- `cargo test --all-features`
- `cargo miri test` if the diff touches any `unsafe` block

## Out of scope without explicit ask

- Adding dependencies (`pyproject.toml` pins only PyGithub).
- Reformatting unrelated files or churning whitespace.
- "Future-proofing" abstractions for hypothetical features. Implement what the issue asks for and stop.
