# Agent Orchestrator — Roadmap

## Status as of 2026-06-05

The full label lifecycle is wired end-to-end: pickup → `decomposing` →
`ready` / `blocked` / `umbrella` → `implementing` → `validating` →
`documenting` (final-docs handoff) → `in_review` → terminal
`done` / `rejected`, with `fixing` and `resolving_conflict` as the
review-side loops back to `validating`, and `question` as an
operator-applied read-only Q&A side branch.

The orchestrator runs as a single long-lived Python process
(`python -m orchestrator.main`, wrapped by `run.sh` for self-restart),
polls one or more configured repos, and delegates coding to `codex` /
`claude` CLI subprocesses in per-issue git worktrees. State lives in
GitHub Issues themselves (one workflow label plus one pinned JSON
comment), so the loop stays stateless and progress is observable on
github.com. Per-repo ticks fan out concurrently; per-issue handlers
within each repo run in parallel up to configurable caps.

For the authoritative behavior, see:

- [`docs/architecture.md`](../docs/architecture.md) — design, module
  map, process / agent / push model.
- [`docs/state-machine.md`](../docs/state-machine.md) — label set,
  per-tick flow, stage-handler semantics, pinned-state schema, label
  lifecycle diagram.
- [`docs/workflow.md`](../docs/workflow.md) — agent roles, command
  specs, session lifecycles.
- [`docs/observability.md`](../docs/observability.md) — audit event
  log, analytics sink, database, dashboard, usage parser.
- [`docs/configuration.md`](../docs/configuration.md) — env vars and
  knobs.
- [`docs/security.md`](../docs/security.md) — operator-owned controls.

This file tracks what shipped and what is still open.

## Shipped

The orchestrator is feature-complete against its original scope. Each
shipped area below is a one-line pointer; behavior details live in the
linked docs.

- **Bootstrap and process model.** Polling loop with `--once` and
  signal-clean shutdown, ancestry-aware self-update detection, `run.sh`
  self-restart wrapper. See
  [`docs/architecture.md#process-model`](../docs/architecture.md#process-model).
- **Agent invocation.** `agents.run_agent` dispatches to `codex` /
  `claude`; `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` specs are
  pinned per issue and re-parsed on every resume. See
  [`docs/workflow.md`](../docs/workflow.md).
- **Security hardening.** Agent and verify-command env strip GitHub
  tokens and secret-shaped vars; provider keys are exact-name
  allowlisted for agent subprocesses only; `git push` runs under a
  neutered git-config envelope with a stamped commit identity. See
  [`docs/security.md`](../docs/security.md).
- **Stage handlers.** Per-stage flow, drift detection, the final-docs
  handoff, manual-merge-only HITL ping, the two `fixing` routes
  (in_review→fixing PR-feedback and validating→fixing
  CHANGES_REQUESTED), the conflict-only `resolving_conflict` route,
  and the read-only `question` side branch all live under
  `orchestrator/stages/`. See
  [`docs/state-machine.md#stage-handlers`](../docs/state-machine.md#stage-handlers).
- **Typed state machine.** `WorkflowLabel` / `ControlLabel` enums in
  `orchestrator/state_machine.py`, with a typo guard and a configurable
  transition guard at the single label-write chokepoint. See
  [`docs/state-machine.md#typed-states-and-the-transition-guard`](../docs/state-machine.md#typed-states-and-the-transition-guard).
- **Multi-repo support.** `REPOS` drives per-repo fan-out across a
  `ThreadPoolExecutor` with per-repo exception isolation; worktrees are
  slug-namespaced. See
  [`docs/architecture.md#per-tick-flow-workflowtick`](../docs/architecture.md#per-tick-flow-workflowtick).
- **Parallel issue processing.** `MAX_PARALLEL_ISSUES_PER_REPO` and
  `MAX_PARALLEL_ISSUES_GLOBAL` bound concurrency; a long-lived
  `IssueScheduler` enforces the in-flight set, per-repo counter,
  family mutex, and duplicate-active gate. Family-aware buckets drain
  on a single worker; no-agent buckets (`blocked` / `umbrella`) run
  cap-exempt on a dedicated pool. See
  [`docs/architecture.md#per-tick-flow-workflowtick`](../docs/architecture.md#per-tick-flow-workflowtick)
  and [`orchestrator/scheduler.py`](../orchestrator/scheduler.py).
- **Workflow module split.** `workflow.py` is a slim facade; stage
  bodies live under `orchestrator/stages/`; shared helpers live in
  `workflow_drift.py`, `workflow_messages.py`, `worktree_lifecycle.py`,
  `git_plumbing.py`, `verify.py`, `branch_publication.py`,
  `base_sync.py`, with `worktrees.py` as a compatibility re-export
  hub. See [`docs/architecture.md#top-level-layout`](../docs/architecture.md#top-level-layout).
- **Tests.** Per-stage and per-routing suites under `tests/`, shared
  helpers in `tests/workflow_helpers.py`, in-memory fakes in
  `tests/fakes.py`. See [`CLAUDE.md`](../CLAUDE.md).
- **Project CI.** GitHub Actions runs `ruff` and `pytest` on PRs under
  read-only token scope; Dependabot opens weekly updates with a
  30-day cooldown; `dependency-review` blocks vulnerable PRs.
- **Audit event log.** Optional opt-in JSONL sink at `EVENT_LOG_PATH`,
  one record per workflow event. See
  [`docs/observability.md#audit-event-log-event_log_path`](../docs/observability.md#audit-event-log-event_log_path).
- **Analytics sink, database, and dashboard.** JSONL sink at
  `ANALYTICS_LOG_PATH` plus an operator-deployed Postgres aggregation
  target (`analytics-db/`), an operator-driven sync CLI
  (`python -m orchestrator.analytics.sync`), a read model
  (`orchestrator/analytics/read.py`), and a Streamlit dashboard
  (`orchestrator/dashboard.py`) over the redesigned standalone analytics
  view. See [`docs/observability.md`](../docs/observability.md).
- **Agent usage / cost parser.** `orchestrator/usage.py` decodes JSONL
  agent stdout into a `UsageMetrics` dataclass; CLI-reported cost wins,
  otherwise a baked-in price table estimates and unknown SKUs yield
  `unknown-price`. See
  [`docs/observability.md#usage-parser-orchestratorusagepy`](../docs/observability.md#usage-parser-orchestratorusagepy).

## Future work

Short actionable entries; expand into design docs only when picked up.

- **Spec-first split.** Insert a `specifying` stage between `ready` and
  `implementing` so a separate spec agent writes failing tests first
  (scoped to test paths) and the orchestrator verifies they fail
  against `origin/<base>` before the implementer runs. Add a
  `spec_skip: true` opt-out to the decomposer manifest for docs /
  refactor work that cannot be expressed as failing tests.
- **Repo memory across issues.** Add a per-target-repo
  `<target_root>/.agent-orchestrator/repo-memory.json` (schema_version,
  verify_commands, top touched files, capped recent failures) updated
  best-effort on merge and folded into decomposer / implementer
  prompts with strict caps. Treat as orchestrator-owned context, not
  PR content.
- **Container / VM isolation + GitHub App migration.** Container or VM
  isolation around the orchestrator host remains an open deployment
  question (the host is currently the real sandbox boundary). Migrate
  from per-repo PATs to a GitHub App installation token.
- **Architectural review at `validating`.** Optional reviewer pass that
  flags structural issues (oversized files, layering violations) that
  the correctness reviewer ignores.
- **Dynamic workflow.** Planner agent that picks stages per issue
  (extra architectural exploration; skip acceptance for trivial fixes).
  Revisit once the static flow is fully dogfooded.
- **Symphony-inspired hooks and policy overrides.** Narrow
  `<target_root>/.agent-orchestrator/policy.toml` overrides (verify
  commands, retry / review-round budgets) with hot-reload, plus three
  workspace lifecycle hooks (`after_create`, `before_run`,
  `after_run`) under `<target_root>/.agent-orchestrator/hooks/`. Both
  opt-in; absent = identical behavior. Full review in
  [`plans/symphony-spec-review.md`](symphony-spec-review.md).

## Risks

- **R1 — Codex / Claude CLI output format drift.** Isolated in
  `agents.parse_session_id` and the per-backend last-message capture;
  failures surface as `session_id=None` (logged) or empty
  `last_message` (park with stderr quoted via
  `_format_stderr_diagnostics`).
- **R2 — Self-mutation while running.** Per-issue worktrees +
  ancestry-aware self-update detection in
  `main._self_modifying_merge_happened` + the `run.sh` self-restart
  wrapper.
- **R3 — Runaway agent loops / token cost.** Wall-clock timeouts
  (`AGENT_TIMEOUT`, `REVIEW_TIMEOUT`), per-issue retry budget
  (`MAX_RETRIES_PER_DAY`), review / fix cap (`MAX_REVIEW_ROUNDS`),
  conflict-resolution cap (`MAX_CONFLICT_ROUNDS`).
- **R4 — GitHub rate limits.** PyGithub handles backoff; 60s ticks are
  well under the 5000 req/hr limit.
- **R5 — Race between human comments and orchestrator action.** Each
  handler re-fetches the issue + pinned-state immediately before any
  transition; any comment newer than the recorded watermark drives the
  awaiting-human resume branch.
