# Symphony Spec Review — Ideas Worth Borrowing

## Context

OpenAI's [Symphony Service Specification](https://github.com/openai/symphony/blob/main/SPEC.md)
(Draft v1) is a language-agnostic, long-running orchestrator that drives
coding agents against Linear issues. It overlaps with `agent-orchestrator`
in spirit (issue tracker → per-issue workspace → coding-agent subprocess)
but diverges on most concrete choices: Linear instead of GitHub, a single
repo-owned `WORKFLOW.md` policy file instead of env vars and pinned-comment
state, an in-worker multi-turn loop instead of a stage machine, an optional
HTTP dashboard, and an SSH worker-pool appendix.

Most of Symphony's surface is already covered by our model (stateless
restart recovery, sanitized workspace paths, structured logging,
24h-window per-issue retry budget, wall-clock stall handling) or is a
different but not-obviously-better design choice for the same problem.
Two ideas survive critical review as real gaps.

## Filter

The issue asks for ideas to borrow, with the caveat that *more features
is bad*. Each candidate must:

1. **Close a gap we've actually felt** in operating real target repos.
2. **Stay small, additive, reversible.** Opt-in; absent file → today's
   behavior.
3. **Not fork the model.** Labels + pinned JSON comment remain the
   authoritative state; stage handlers remain the dispatcher.

## Proposal 1 — Per-target-repo policy file

Path: `<target_root>/.agent-orchestrator/policy.toml`.

### Gap

Every per-repo tunable today is an env var on the orchestrator process
(see [`docs/configuration.md`](../docs/configuration.md)). `REPOS`
carries `target_root`, `base_branch`, `remote_name`, and `parallel_limit`,
but nothing else. A polyglot host driving a Rust crate, a Python service,
and a Go CLI has to settle on one global `VERIFY_COMMANDS` or run multiple
orchestrator processes. `<target_root>/.agent-orchestrator/` is a
natural home for a version-controlled, target-repo-owned policy file
(distinct from the roadmap's orchestrator-owned `repo-memory.json`,
which lives under the same prefix but is explicitly *not* PR content).

### Symphony parallel

Symphony's `WORKFLOW.md` carries all policy as YAML front matter, with
strict typed validation and dynamic reload. We borrow the file shape and
reload semantics for a narrow allow-list, not the "everything in this
file" stance. TOML keeps us on stdlib (`tomllib`); YAML's block ergonomics
don't pay off at this schema size.

### Schema (initial, intentionally small)

```toml
[verify]
commands = ["uv run pytest", "uv run ruff check ."]
timeout_seconds = 900

[budgets]
max_retries_per_day = 8
max_review_rounds = 5
```

### Loader behavior

- Read on each per-repo tick start, so edits land without restarting.
- Per-repo value wins over env default; missing keys fall through.
- Unknown top-level keys log a warning and are ignored (backward-
  compatible schema evolution).
- Parse failure → log operator-visible error, skip that repo's tick,
  keep the last-known-good cached policy.
- Target-repo-owned and version-controlled; the orchestrator never
  writes to it.

### Trust

The file lives in the target repo, which an implementer agent can edit.
Restrict the schema to values safe to flip from a PR (verify commands,
budgets). Anything controlling agent identity, tokens, or git remotes
stays env-only on the orchestrator host. Narrower than Symphony §15.4
because our mutation channel is GitHub PRs, not a sysadmin commit.

### Cost

- Zero new runtime deps (`tomllib` is stdlib on 3.12+).
- Existing deployments unaffected when the file is absent.
- A bug in resolution can flip budgets unexpectedly — unit-test the
  resolution matrix and one corrupt-file case before shipping.

## Proposal 2 — Workspace lifecycle hooks

### Gap

Pre-implementation setup (`cargo fetch`, `uv sync`, `npm ci`, warming a
Docker base image) today either lives inside the agent prompt (slow,
token-costly every run) or in `run.sh` (host-wide, fights cross-repo).
`VERIFY_COMMANDS` covers the post-implementation gate, nothing earlier.

### Symphony parallel

Symphony §5.3.4 / §9.4 define four hooks with `hooks.timeout_ms` 60s
default. Pre-work failures abort; post-work failures are logged and
ignored. That asymmetry is right.

### Sketch

Adopt three of the four — skip `before_remove`. Worktree removal today
happens in several places (`_cleanup_decompose_worktree`,
`_cleanup_question_worktree`, terminal cleanup), and a hook firing on
every transient decomposer / question teardown is more surface than
the use case justifies.

- **`after_create`** (`<target_root>/.agent-orchestrator/hooks/after_create.sh`) — first time a
  per-issue worktree is created. Failure: park run.
- **`before_run`** (`…/hooks/before_run.sh`) — start of every agent invocation inside the
  worktree — implementer, reviewer, decomposer, question, docs, fixing, and the
  conflict-resolution dev run. Failure: park with `park_reason=hook_before_run_failed`.
- **`after_run`** (`…/hooks/after_run.sh`) — agent exits, regardless of success. Failure: log only.

Timeout: single `[hooks].timeout_seconds` key in `policy.toml`,
default 60s. Per-hook overrides can come later.

### Execution contract

- `bash -lc <path>` with `cwd=<worktree>`.
- Environment scrub via `agents._filter_agent_env(...,
  allow_provider_auth=False)` — the same verify-style filter used for
  `VERIFY_COMMANDS` (see [`docs/configuration.md`](../docs/configuration.md)).
  Hooks are arbitrary target-repo shell, so the stricter form is
  correct: GitHub tokens *and* model provider keys
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) stay out of the hook
  environment. The default `allow_provider_auth=True` path is reserved
  for the agent subprocess itself.
- `_git_hardened` today wraps `git` subprocesses only (no hooks /
  fsmonitor / credential helper / detached global config — see
  [`docs/architecture.md`](../docs/architecture.md) and
  [`docs/state-machine.md`](../docs/state-machine.md)). Shell hooks
  run outside that envelope, so before-run hooks need their own
  hardening: refuse to launch if the worktree's `.git/config` has
  been mutated since `after_create`, and run with a scrubbed env
  that omits `GIT_CONFIG*`, `GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_EXEC_PATH`, and `GIT_HOOKS_PATH` so a hook script can't redirect
  the agent's later `git` calls into attacker-controlled config.
- Output captured, truncated in logs via `_format_stderr_diagnostics`.
- Not advertised to the agent as a tool — orchestrator-side ritual.

### Cost

- Hooks are arbitrary shell the implementer can write. The orchestrator
  already trusts the agent with worktree writes and bypass-sandbox CLI
  flags, so this is not a new trust expansion. Add a note in
  `docs/architecture.md` that target-repo hooks run as the orchestrator
  UID; hosts wanting stricter isolation should run under a dedicated
  user (Symphony §15.2).
- Pre-run hooks add per-dispatch latency. Document that hooks should
  be idempotent and cheap on the steady-state path.

## Considered but rejected

- **Full `WORKFLOW.md` as single source of truth.** Our stage machine
  *is* the workflow ([`docs/state-machine.md`](../docs/state-machine.md));
  prompts are built across stage modules with structured outputs the
  downstream parsers depend on. Proposal 1's narrow override list is the
  defensible slice.
- **HTTP server + JSON state API.** Pinned JSON comments on
  github.com plus `ANALYTICS_LOG_PATH` cover the dashboard use case
  asynchronously. Bind/port/auth questions for marginal benefit.
- **SSH worker pool (Symphony Appendix A).** Workspace locality, host
  drift, failover, and "did this run start on host A before we retried
  on host B" are heavy. Single-host + per-repo / global caps suffice.
- **In-worker continuation-turn loop.** We exit and re-tick so each
  stage transition shows up as a label change on github.com. The token
  savings cost us that single-source-of-truth property.
- **Per-state concurrency cap.** The scheduler's per-repo and global
  parallelism caps plus the duplicate-active-issue gate already provide
  the headline protection. Defer until a concrete pain shows up.
- **Event-stream stall detection.** Current `AGENT_TIMEOUT` /
  `REVIEW_TIMEOUT` + `agent_silent` / `agent_timeout` parks cover the
  failure modes. JSONL streaming cost > benefit.
- **`gh` as a declared tool.** Codex and Claude already get shell via
  bypass-sandbox flags; `gh` is reachable today.
- **Liquid-style template engine.** Python f-strings + stage prompt
  builders are already strict (missing field = Python error).

## Open questions

- **Per-stage hook variants.** A single `before_run.sh` plus `$STAGE`
  in the environment is probably enough for v1.
- **Hook idempotency vs caching.** Document the contract; don't try
  to be clever in the orchestrator.

## Sequencing

Land Proposal 1 first — covers the most-requested override
(`verify.commands` per repo) and establishes the
`.agent-orchestrator/policy.toml` precedent that Proposal 2 reuses for
`hooks.timeout_seconds`. Both stay opt-in.
