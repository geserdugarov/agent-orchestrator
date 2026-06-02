# Symphony Spec Review — Ideas Worth Borrowing

## Context

OpenAI's [Symphony Service Specification](https://github.com/openai/symphony/blob/main/SPEC.md)
(Draft v1) defines a language-agnostic, long-running orchestrator that drives
coding agents against Linear issues. It overlaps with `agent-orchestrator` in
spirit (issue tracker → per-issue workspace → coding-agent subprocess) but
diverges on most concrete choices: Linear instead of GitHub, a single
repo-owned `WORKFLOW.md` policy file instead of env vars and pinned-comment
state, an in-worker multi-turn loop instead of a stage machine, an OPTIONAL
HTTP dashboard, and an SSH worker-pool appendix.

The bulk of Symphony's surface is either already covered by our model
(stateless restart recovery, workspace persistence, sanitized workspace
paths, structured logging, retry queue with backoff caps, stall handling via
wall-clock timeouts) or is a different design choice for the same problem
(stage labels + pinned JSON comments instead of one in-memory orchestrator
state) where neither is obviously better. Two ideas, however, do address
real gaps in our model and survive critical review.

## What "gold" means here

The issue asks for ideas to borrow, with the explicit caveat that *more
features is bad*. The bar this review applies:

1. **Closes a gap we actually feel.** The pain has shown up in operating
   real target repos, not in hypothetical fleets we might build later.
2. **Small, additive, reversible.** Lands behind defaults so existing
   single-repo deployments are unchanged. Removable without ripping out the
   state machine.
3. **Doesn't fork the model.** Keeps GitHub-labels-plus-pinned-comment as
   the authoritative state and the stage handlers as the dispatcher.

Symphony's `WORKFLOW.md` and workspace-hook surface meet that bar. Most of
the rest doesn't (see [Rejected](#considered-but-rejected) below).

## Proposal 1 — Per-target-repo policy file (`.agent-orchestrator/policy.toml`)

### Gap

Today everything that varies between target repos goes through env vars on
the orchestrator process: `VERIFY_COMMANDS`, `VERIFY_TIMEOUT`,
`MAX_RETRIES_PER_DAY`, `MAX_REVIEW_ROUNDS`, `DECOMPOSE`, the
backend specs, and so on. The `REPOS` env line carries per-repo
`target_root`, `base_branch`, `remote_name`, and `parallel_limit` — but
nothing else. A polyglot orchestrator host that drives a Rust crate, a
Python service, and a Go CLI has to settle on one global `VERIFY_COMMANDS`
or run multiple orchestrator processes. The roadmap's
"Repo memory carried across issues" item already establishes
`<target_root>/.agent-orchestrator/` as the right home for target-repo-owned
context; this proposal extends the same directory to operator-tunable
policy.

### Symphony parallel

Symphony's `WORKFLOW.md` carries `tracker`, `polling`, `workspace`, `hooks`,
`agent`, and `codex` blocks as YAML front matter, with strict typed
validation and dynamic reload on file change. Symphony pushes *all* policy
into that file — for us that would mean tearing up env-driven config, which
is overkill. Borrow the file shape and the reload semantics for a narrow
allow-list of per-repo overrides instead. v1 ships TOML rather than YAML
so the orchestrator can stay on a single pinned dependency (`tomllib` is
stdlib in 3.12+); the schema is small enough that YAML's block ergonomics
don't pay off here.

### Sketch

File: `<target_root>/.agent-orchestrator/policy.toml`. Schema (initial,
intentionally small):

```toml
[verify]
commands = [                 # overrides VERIFY_COMMANDS for this repo only
  "uv run pytest",
  "uv run ruff check .",
]
timeout_seconds = 900        # overrides VERIFY_TIMEOUT

[budgets]
max_retries_per_day = 8      # overrides MAX_RETRIES_PER_DAY for this repo
max_review_rounds = 5        # overrides MAX_REVIEW_ROUNDS
```

Resolution rule: per-repo policy value wins over the env-default; missing
keys fall through. Unknown top-level keys log a warning and are ignored
(Symphony's "Unknown keys SHOULD be ignored" rule, which keeps schema
evolution backward-compatible).

Loader behavior:

- Read on each per-repo tick start, not at process boot, so edits land
  without restarting the orchestrator (Symphony's dynamic-reload contract).
- Parse failure → log operator-visible error, skip dispatch for that
  repo's tick, keep the last-known-good cached policy in memory
  (Symphony §6.2: "Invalid reloads MUST NOT crash the service; keep
  operating with the last known good effective configuration").
- The file is target-repo-owned and version-controlled; treat it the same
  way `.env.example` documents env vars — never write to it from the
  orchestrator.

Trust boundary: the file lives in the *target* repo, which an implementer
agent can edit. Restrict the schema deliberately to values that are
operator-visible and safe to flip from a PR (verify commands, budgets).
Anything that controls agent identity, tokens, or git remotes
stays env-only on the orchestrator host. This matches Symphony's stance in
§15.4 that hooks (and by extension repo-owned policy) "are fully trusted
configuration" — but we should be narrower because GitHub PRs are the
mutation channel, not a sysadmin commit.

### Cost / risk

- Zero new runtime dependencies: `tomllib` ships in the stdlib for the
  Python 3.12+ target. `pyproject.toml` continues to pin only `PyGithub`.
- Existing deployments are unaffected if the file is absent.
- A bug in policy resolution can flip budgets or verify behavior
  unexpectedly; unit-test the resolution rule against a matrix of
  present/missing keys and one corrupt-file case before shipping.

## Proposal 2 — Workspace lifecycle hooks

### Gap

Today, anything a target repo needs to do before the implementer runs
(prime a `cargo` build cache, install JS deps, pre-pull a Docker base
image, bootstrap a virtualenv) either has to live inside the agent's
prompt (slow, costs tokens every time) or get baked into `run.sh` (host-
wide, fights cross-repo). `VERIFY_COMMANDS` covers the post-implementation
test gate but nothing earlier in the lifecycle.

### Symphony parallel

Symphony §5.3.4 / §9.4 define four hooks — `after_create`, `before_run`,
`after_run`, `before_remove` — with `hooks.timeout_ms` defaulting to 60s
and well-defined failure semantics (`after_create` / `before_run` failures
abort, `after_run` / `before_remove` failures are logged and ignored).
That asymmetry is right: pre-work failures should park the run; post-work
failures shouldn't undo a good commit.

### Sketch

Adopt three of the four hooks. Skip `before_remove` — we don't
proactively remove worktrees per-tick; terminal cleanup is the only
removal path and an extra script hook there is not worth the surface
area.

Hook locations (in the target repo, version-controlled):

- `<target_root>/.agent-orchestrator/hooks/after_create.sh` — runs the
  first time a per-issue worktree is created. Good place to seed a
  `.envrc` or pre-warm a language-server cache for the worktree.
- `<target_root>/.agent-orchestrator/hooks/before_run.sh` — runs at the
  start of every implementer / reviewer / docs / fixing invocation,
  inside the worktree, before the agent subprocess starts. Good place
  for `uv sync`, `cargo fetch`, `npm ci`. Failure parks the run with a
  typed `park_reason=hook_before_run_failed`.
- `<target_root>/.agent-orchestrator/hooks/after_run.sh` — runs once the
  agent exits, regardless of success. Failure is logged but does not
  affect the dispatch outcome. Good place for `cargo clean` if disk
  pressure matters; most repos won't need it.

Timeout: a single `[hooks].timeout_seconds` key in `policy.toml` (default
60s), matching Symphony. Per-hook overrides can be a later extension if
demand emerges; v1 keeps one knob.

Execution contract:

- `bash -lc <path>` with `cwd=<worktree>`, identical environment scrub
  as `agents._agent_env` (no GitHub tokens leaked to the hook).
- Honour the same `_git_hardened` envelope so a hook can't poison git
  config in a way that survives into the agent run.
- Output captured and truncated in logs (Symphony §15.4: "Hook output
  SHOULD be truncated in logs"). Reuse `_format_stderr_diagnostics`.
- The hook is not advertised to the agent as a tool; it's an
  orchestrator-side ritual.

### Cost / risk

- Hooks are arbitrary shell scripts the implementer agent can write. The
  orchestrator already trusts the agent with worktree writes and a
  bypass-sandbox CLI flag, so this is not a fundamentally new trust
  expansion; but it deserves a one-line note in `docs/architecture.md`
  flagging that target-repo hooks run with the same OS user as the
  orchestrator process. Hosts that want stricter isolation should run
  the orchestrator under a dedicated UID (Symphony §15.2's RECOMMENDED
  hardening — worth echoing in our docs even without taking on
  containerization).
- Pre-run hooks add latency to every dispatch. Document the
  expectation: hooks should be idempotent and cheap on the steady-state
  path (cache hits), not from-scratch builds.

## Considered but rejected

Calling these out explicitly so future readers don't have to re-derive
why we passed.

- **`WORKFLOW.md` as the single source of truth for prompts and
  workflow.** Symphony pushes the entire per-issue prompt template
  (with a strict Liquid-like template engine) into the repo-owned file.
  For us the stage machine *is* the workflow — prompts are built across
  six stage modules with stage-specific logic, structured outputs
  (manifest blocks, verdict markers), and re-prompting flows. Letting
  target repos override the prompt template would either invalidate the
  parsers downstream of each stage or force us to expose them as a
  contract too. Not worth it. Proposal 1's narrow override list is the
  defensible slice.

- **HTTP server + JSON state API (`/api/v1/state`,
  `/api/v1/<issue>`, `/refresh`).** Symphony §13.7 ships an optional
  HTTP server for dashboards. We get most of this asynchronously: the
  pinned JSON state comment per issue is grep-able on github.com, and
  `ANALYTICS_LOG_PATH` exposes per-event timing / cost. Standing up an
  HTTP server adds bind/port/auth/loopback/scope questions for marginal
  operator benefit. The roadmap's "Dynamic workflow" item is already
  flagged as deferred until the static flow is fully dogfooded; an HTTP
  control plane is several rungs further out than that.

- **SSH worker pool (Appendix A).** Multi-host execution is genuinely
  heavy: workspace locality, host drift, failover semantics, per-host
  caps, and the "did this run actually start producing side effects on
  host A before we retried on host B" problem. Single-host with
  per-repo and global parallelism caps is sufficient for the project's
  budget. Revisit only if a real fleet need shows up.

- **In-worker continuation-turn loop.** Symphony's worker stays in one
  thread across multiple turns, re-checking tracker state between
  turns. Our model exits and re-ticks for visibility (every stage
  transition shows up as a label change on github.com). The token
  savings of an in-worker loop are real, but they cost us the property
  that the github.com view is the source of truth for "what is the
  orchestrator doing right now." Keep the current shape.

- **Per-state concurrency cap (`max_concurrent_agents_by_state`).**
  Niche: most fleets won't want to throttle decomposers separately from
  implementers, and our per-issue cap already provides the headline
  protection. Defer until a concrete pain shows up.

- **Event-stream stall detection on top of the wall-clock timeout.**
  Symphony §5.3.6 / §8.5 distinguish a "no events for N ms" stall from
  a total wall-clock turn cap. Our `AGENT_TIMEOUT` / `REVIEW_TIMEOUT`
  catches deadlocked agents at the same upper bound that catches
  legitimately long runs; a finer-grained stall signal would let us
  kill silently-stuck runs sooner. The plumbing cost (streaming + JSONL
  decode in `agents.py`) is real and the benefit is marginal — current
  parks on `agent_silent` / `agent_timeout` already cover the failure
  modes. Skip unless we see runs that should have been killed earlier.

- **`linear_graphql` client-side tool extension.** The Symphony
  equivalent for us would be a `gh` tool. Codex and Claude already get
  shell access via the bypass-sandbox flags, so they can call `gh`
  directly without us standing up a tool contract. Implicit in our
  model already.

- **Liquid-style strict template engine for prompts.** Adds a runtime
  dependency to fix a problem we don't have. Python f-strings and the
  stage modules' prompt builders are explicit and already strict (a
  missing field is a Python error, not a silent empty string).

## Open questions

- **Per-stage hook variants.** Symphony's hooks fire once per worker
  attempt; ours could plausibly want to differentiate
  `before_run.implementer.sh` vs `before_run.reviewer.sh`. Probably
  overkill for v1 — a single `before_run.sh` plus `$STAGE` in the
  environment is enough.
- **Hook idempotency vs caching.** If a `before_run.sh` runs `uv sync`
  on every dispatch, the cost compounds in tight feedback loops. Doc
  the contract; don't try to be clever in the orchestrator.

## Sequencing

If both proposals land, do them in this order:

1. **Proposal 1 first**, TOML-backed, single repo at a time. Covers the
   most-requested override (`verify.commands` per repo) and creates the
   `.agent-orchestrator/policy.toml` precedent.
2. **Proposal 2** depends on Proposal 1 for its timeout key
   (`hooks.timeout_seconds`) — and the trust framing in §15.4 reads
   more naturally once policy resolution exists.

Both proposals stay opt-in: an absent file is the documented "behave
exactly as today" state, so existing deployments don't have to migrate.
