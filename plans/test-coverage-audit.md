# Test coverage audit — post-split

Audit produced for #351 after the oversized-file split landed
(`tests/test_workflow_validating_*`, `tests/test_workflow_in_review_*`,
`tests/test_workflow_implementing_*`, `tests/test_workflow_decomposition_*`,
plus the facade-level routing / cleanup / drain modules).

The audit compares the reorganized test surface against the declared
functionality in `README.md`, `docs/architecture.md`, `docs/state-machine.md`,
`docs/workflow.md`, `docs/configuration.md`, `docs/observability.md`, and
`docs/security.md`. It only documents what is and is not covered today; no
new tests are added in this PR.

## How to read this

For every documented surface below, the column "Coverage" notes either the
test file(s) that meaningfully assert it, or `gap` when no test exercises the
contract. "Gap" never means "doesn't work" — it means a future regression
would not be caught by the suite. "Assertion-light" calls out tests that
deliberately verify a no-op / log-and-swallow behavior; they are listed
because issue #351 asks for them, not because they should be expanded.

## Covered surfaces

### State machine — per-stage handlers (`docs/state-machine.md`)

| Surface | Coverage |
| --- | --- |
| `_handle_pickup` (no-label → decomposing/implementing, `ALLOWED_ISSUE_AUTHORS`) | `tests/test_workflow_pickup.py` |
| User-content drift hook (every stage) | `tests/test_workflow_drift.py`, `tests/test_workflow_decomposition_drift.py`, `tests/test_workflow_implementing_drift.py`, `tests/test_workflow_validating_drift.py`, `tests/test_workflow_in_review_drift.py` |
| `_handle_decomposing` (single/split/umbrella, retries, half-finished recovery, DECOMPOSE-off mid-flight) | `tests/test_workflow_decomposition_decomposing.py`, `tests/test_workflow_decomposition_manifest.py` |
| `_handle_ready` | `tests/test_workflow_decomposition_ready.py` |
| `_handle_blocked` / `_handle_umbrella` | `tests/test_workflow_decomposition_blocked.py`, `tests/test_workflow_decomposition_umbrella.py`, `tests/test_workflow_decomposition_children.py`, `tests/test_workflow_decomposition_finalize.py` |
| `_handle_implementing` (fresh / retry / PR reuse / terminal / full-spec persistence / drift) | `tests/test_workflow_implementing_*.py` (six focused modules) |
| `_handle_documenting` (drift unwind, fetch_failed, diverged, `git reset --hard` + `git clean -fd`, `docs_drift_unwind_pending`, recovered-commit shortcut, ratchet of `pr_last_comment_id`, NO_CHANGE verdict) | `tests/test_workflow_documenting.py`, `tests/test_workflow_documenting_routing.py` |
| `_handle_validating` (review loops, cap escape hatch, squash, watermarks, verify gate, drift, terminal, handoff to `documenting`) | `tests/test_workflow_validating_*.py` (seven focused modules) |
| `_handle_in_review` (manual-merge-only, HITL ping de-duped by head SHA, fixing route, drift, checks, parked, migration, watermarks, fresh-feedback fixing route) | `tests/test_workflow_in_review_*.py` (eight focused modules) |
| `_handle_fixing` (quiet window, watermark-tight advance, pushed-fix → validating, no-feedback bounce) | `tests/test_workflow_fixing.py`, `tests/test_workflow_fixing_routing.py` |
| `_handle_resolving_conflict` (rebase, recovered push, conflict resume, awaiting-human, cap, dirty, diverged, drift, lease semantics) | `tests/test_workflow_conflicts.py` |
| `_handle_question` (answer, multi-turn, unsafe-relabel guard, closed terminal, base-sync skip) | `tests/test_workflow_question.py`, `tests/test_workflow_question_routing.py` |
| `_finalize_if_pr_merged` (cross-stage external-merge short-circuit) | `tests/test_workflow_finalize_pr_merged.py` |
| `_finalize_if_issue_closed` (open-PR vs closed-PR variants, no-`pr_number`, defer on fetch failure, defer on race-merged) | `tests/test_workflow_implementing_terminal.py` (`test_closed_implementing_defers_when_pr_fetch_fails`, `test_closed_implementing_defers_when_pr_merged`) |
| `_drain_review_pr_terminals` (shared terminal funnel for in_review / fixing / resolving_conflict) | `tests/test_workflow_drain_terminals.py` |
| Pinned-state JSON schema (every key documented in `state-machine.md` lines 90–117) | Covered by the stage-specific suites above. `dev_agent` / `review_agent` / `decomposer_agent` / `question_agent`, `pr_last_*_id` watermarks, `docs_*` fields, `pending_fix_*`, `ready_ping_sha`, `merged_at` / `closed_without_merge_at`, `user_content_hash`, `retry_*` are all asserted on real label transitions. |

### Scheduler and parallel dispatch (`docs/architecture.md`, `docs/configuration.md`)

| Surface | Coverage |
| --- | --- |
| Per-tick fan-out across repos, family vs fan-out partitioning | `tests/test_workflow_tick_parallel.py` |
| `IssueScheduler` caps (`MAX_PARALLEL_ISSUES_PER_REPO`, `MAX_PARALLEL_ISSUES_GLOBAL`), duplicate-active gate, family mutex, reap, shutdown sequencing | `tests/test_scheduler.py` |
| Label-to-bucket classification (every workflow / control label routed correctly) | `tests/test_workflow_scheduler_routing.py`, `tests/test_workflow_backlog_routing.py`, `tests/test_workflow_question_routing.py`, `tests/test_workflow_documenting_routing.py`, `tests/test_workflow_fixing_routing.py`, `tests/test_workflow_in_review_routing.py` |
| Pre-tick base refresh, `hold_base_sync` / `question` skip, PR-having vs pre-PR detour, scheduler-active skip | `tests/test_workflow_base_sync_unit.py`, `tests/test_workflow_base_sync_real_git.py` |
| Worktree serialization / per-target-root locks | `tests/test_workflow_worktree_serialization.py`, `tests/test_workflow_worktree_paths.py` |
| Worktree lifecycle (ensure/clean/decompose/question/PR worktrees, terminal branch cleanup) | `tests/test_workflow_cleanup.py`, `tests/test_workflow_decomposition_cleanup.py` |

### Agent / push subsystem (`docs/architecture.md`, `docs/workflow.md`)

| Surface | Coverage |
| --- | --- |
| `agents.run_agent` codex/claude dispatch, AgentResult assembly, session-id harvest | `tests/test_agents.py` |
| `_filter_agent_env` strip sets (`_FORBIDDEN_AGENT_ENV`, `_AGENT_SECRET_SUFFIXES`, `_AGENT_WRITE_CREDENTIAL_LOCATORS`), `_AGENT_PROVIDER_AUTH_ALLOWLIST` for agent vs `allow_provider_auth=False` for verify shell | `tests/test_agents.py`, `tests/test_workflow_validating_verify.py` |
| Push hardening (`GIT_ASKPASS` tempfile, `GIT_CONFIG_GLOBAL=/dev/null`, hooks/credential disabled, `--force-with-lease`, refuse `insteadOf` rewrites, explicit `HEAD:refs/heads/<branch>` refspec) | `tests/test_workflow_branch_publication.py`, `tests/test_workflow_conflicts.py` |
| Role pinning (`dev_agent` / `decomposer_agent` raw spec, locked spec on resume, legacy bare-backend round-trip, `codex_session_id` honored on read) | `tests/test_workflow_implementing_full_spec.py`, `tests/test_workflow_implementing_retry.py` |
| `_run_agent_tracked` analytics record (model, tokens, cost source, fallback model from `extra_args`) | `tests/test_workflow_agent_analytics.py`, `tests/test_workflow_model_extraction.py` |
| Question-stage spec (`question_agent` separate pin seeded from `DECOMPOSE_AGENT`) | `tests/test_workflow_question.py` |
| Prompt redaction (`stderr` secret scrub) | `tests/test_workflow_prompt_redaction.py` |

### Configuration (`docs/configuration.md`)

| Surface | Coverage |
| --- | --- |
| `REPOS` multi-repo syntax: every malformed-entry branch, fifth-field overrides, duplicate slug | `tests/test_config.py::Repos*` |
| `_parse_agent_spec` (first-token rule, args round-trip, SystemExit on bad spec) | `tests/test_config.py::AgentSpec*` |
| `_strip_dotenv_quotes`, `_load_dotenv` quoting bug fix (matched outer pair vs inner quotes) | `tests/test_config.py::DotenvQuoteStrippingTest` |
| Cadence / budget vars (`MAX_*` defaults, `MAX_RETRIES_PER_DAY=0 = unbounded`) | `tests/test_config.py` |
| `HITL_HANDLE` parsing / dedupe | `tests/test_config.py` |
| `ALLOWED_ISSUE_AUTHORS` pickup gate | `tests/test_config.py`, `tests/test_workflow_pickup.py` |
| Local verify gate — every `park_reason` token (`verify_failed`, `verify_timeout`, `verify_dirty`, `verify_head_changed`), redaction/truncation of published command output | `tests/test_workflow_validating_verify.py` |
| `IN_REVIEW_DEBOUNCE_SECONDS` quiet window | `tests/test_workflow_fixing.py` |
| `hold_base_sync` / `backlog` control labels | `tests/test_workflow_base_sync_unit.py`, `tests/test_workflow_backlog_routing.py`, `tests/test_workflow_conflicts.py`, `tests/test_workflow_in_review_routing.py` |

### Observability (`docs/observability.md`)

| Surface | Coverage |
| --- | --- |
| `stage_enter` audit event (every label flip via `set_workflow_label`) | `tests/test_workflow_event_emission.py` |
| `agent_spawn` / `agent_exit` audit events, `session_id` omitted on fresh spawn, `duration_s` / `exit_code` / `timed_out` on exit | `tests/test_workflow_event_emission.py` |
| `review_verdict`, `park_awaiting_human` (with stage from current label, reasons) | `tests/test_workflow_pr_lifecycle.py` |
| `pr_opened`, `pr_merged` (per-stage `stage=`, always `merge_method="external"`), `pr_closed_without_merge` | `tests/test_workflow_pr_lifecycle.py`, `tests/test_workflow_conflicts.py` |
| `merge_attempt` only emitted by `_handle_resolving_conflict`; absent from `_handle_in_review` | `tests/test_workflow_pr_lifecycle.py`, `tests/test_workflow_conflicts.py` |
| `conflict_round` (`entered` / `incremented` with `outcome`) | `tests/test_workflow_conflicts.py`, `tests/test_workflow_pr_lifecycle.py` |
| Analytics append, prune, retention-aware no-op when sink disabled / retention non-positive, concurrent append-vs-prune lock | `tests/test_analytics.py` |
| Analytics schema (`analytics_events`, `analytics_agent_runs` view, derived columns) | `tests/test_analytics_schema.py` |
| Sync (batched `cur.executemany("INSERT … ON CONFLICT (content_hash) DO NOTHING", batch)` flush sized by `_BATCH_SIZE` with final partial-batch flush at EOF and per-batch `rowcount` driving `inserted` / `skipped_duplicate`; startup `SELECT content_hash FROM analytics_events WHERE content_hash IS NOT NULL` pre-check that filters already-present rows in Python before they reach the batch and absorbs intra-file duplicates against the same set so pre-skipped rows never reach `executemany`; malformed-line tolerance, driver-error rollback, no-op when disabled / file missing, connection-success log + redacted URL, periodic progress log per `_PROGRESS_INTERVAL`, `duration_s` + UTC-stamped stdout summary + log/stdout share one UTC clock, libpq query-string credential redaction including case-insensitive `password` / `sslpassword` / `user` / `passfile`) | `tests/test_analytics_sync.py` |
| Read model (`get_filter_options`, `get_data_extent`, `get_summary`, `get_kpi_prev`, `get_time_series`, `get_stage_breakdown`, `get_event_breakdown`, `get_recent_agent_exits`, `get_issues`, `get_issue_events`, `get_repo_breakdown`, `get_hourly_heatmap`, `get_throughput_breakdown`, `get_review_round_breakdown`, `get_backend_daily_tokens`, `get_backend_efficiency`, `get_cost_coverage`) | `tests/test_analytics_read.py` |
| Dashboard rendering primitives, lazy-import guard, multiselect / stage-filter contract (`None` vs `[]`), insight banners | `tests/test_dashboard.py`, `tests/test_dashboard_charts.py`, `tests/test_dashboard_theme.py` |
| Usage parser cost precedence (`reported` > `estimated` > `unknown-price` > `no-usage`), malformed-JSONL tolerance, fallback model from spec args | `tests/test_usage.py`, `tests/test_workflow_agent_analytics.py` |

### Main loop / run.sh

| Surface | Coverage |
| --- | --- |
| Reap + prune ordering: one reap, one prune per polling pass | `tests/test_main.py` |
| Multi-repo `_run_tick` fan-out, per-repo exception isolation | `tests/test_main.py` |
| Global scheduler cap binding across repos | `tests/test_main.py::test_scheduler_global_cap_bounds_concurrent_workers_across_repos` |
| SIGINT/SIGTERM exit code (`128 + signum`), `shutdown(wait=False)` on signal mid-tick (`reason=closed` on every remaining submit), `shutdown(wait=True)` on normal exit | `tests/test_main.py` |
| `run.sh` self-update: startup `git pull --ff-only` failure stops without launching; restart-time pull failure exits instead of relaunching stale code | `tests/test_run_sh.py` |

## Gaps (clear, prioritized)

These are documented behaviors with no behavioural coverage. They are
ordered by likely blast radius if they regressed silently.

1. **`GITHUB_TOKEN` rejection from `.env`** — `docs/configuration.md#github-pat`
   and `docs/security.md` both call this out as the load-bearing reason the
   PAT lives outside the worktree. `orchestrator/config.py:73` logs and skips
   any `_SECRET_KEYS` entry it finds in `.env`. No test verifies the warning
   is emitted or that `os.environ` is left untouched, so a refactor that
   accidentally removes the guard would not fail CI. Smallest useful test:
   point `_load_dotenv` at a temp dir containing
   `GITHUB_TOKEN=leaked` and assert (a) the warning lands on stderr,
   (b) `GITHUB_TOKEN` is not subsequently set in `os.environ`.
2. **Self-restart guard (`main._self_modifying_merge_happened`)** — defined
   at `orchestrator/main.py:119` and called at `orchestrator/main.py:196`.
   `docs/architecture.md` describes the contract as "exit 0 so the wrapper
   re-execs the new code". `tests/test_run_sh.py` covers the wrapper side
   (a successful self-update with a clean exit relaunches), but no test
   exercises the Python side — e.g., a faked `git rev-list` that returns a
   diff which touches `orchestrator/*` should trigger the early `return`.
   The closest existing coverage is the wrapper test that hard-codes
   `pull --ff-only origin main`; it neither pins `ORCHESTRATOR_BASE_BRANCH`
   to a non-default value nor checks the Python guard.
3. **`build_event_record` schema invariants** — `docs/observability.md` lists
   the `ts` / `repo` / `issue` / `event` floor, the optional `stage`, the
   None-drop on extras, and `sort_keys=True` on disk. These invariants are
   exercised transitively from every `emit_event` test, but no direct unit
   test asserts (a) a `None` extra disappears from the resulting JSON,
   (b) two records with the same content hash to different field orders,
   (c) the dropped-key contract for `session_id` on `agent_spawn` for fresh
   spawns. A drift here would silently break content-hash dedupe in
   `analytics/sync.py` because the hash is taken over the canonical JSON.
4. **License-header convention** — `CLAUDE.md` requires every `.py` / `.sh`
   / `pyproject.toml` source file to start with the two-line copyright +
   SPDX header. No test enforces it. A file added by an agent without the
   header would only be caught at code review. A simple test could glob
   `orchestrator/**/*.py`, `tests/**/*.py`, `*.sh`, `pyproject.toml` and
   assert the first two lines match the documented prefix.
5. **CI workflow `permissions:` block** — `docs/security.md` documents that
   `.github/workflows/ci.yml` and `dependency-review.yml` must declare
   `permissions: contents: read` at the top level so the fork-PR
   `GITHUB_TOKEN` is read-only. No test parses the workflow files to verify
   the assertion. A small YAML-parser test would catch an accidental
   permission widening.
6. **`run.sh` signal traps** — the wrapper documents `trap 'exit 130' INT` /
   `trap 'exit 143' TERM` and a "second Ctrl+C terminates immediately"
   contract. `tests/test_run_sh.py` covers the `git pull` failure paths
   only; the signal traps are exercised transitively by the systemd
   guidance but not by the suite. A test that signals the wrapper while
   it is mid-poll would verify both exit codes.
7. **`ORCHESTRATOR_BASE_BRANCH` non-default in `run.sh`** — the wrapper reads
   it from `.env` (default `main`). Both `test_run_sh.py` cases hard-code
   `pull --ff-only origin main`; no test asserts that an alternate base
   branch is honored. A regression that ignored the env variable would be
   silent in CI.

## Assertion-light / smoke-only tests

The suite is broadly behavioural — most tests assert label transitions,
exact pinned-state mutations, comment markers, or event-record contents.
Two patterns stand out as "thin but intentional"; both are listed for the
reviewer rather than flagged for expansion.

- **Routing invariant tests.** `tests/test_workflow_question_routing.py`,
  `tests/test_workflow_documenting_routing.py`,
  `tests/test_workflow_fixing_routing.py`,
  `tests/test_workflow_backlog_routing.py`,
  `tests/test_workflow_decomposition_children.py`. Each function has 1–2
  assertions verifying that the label is in the right `_FAMILY_AWARE_LABELS`
  set, dispatches to the right handler, and (for `backlog`) short-circuits
  before timing. They are intentionally narrow — they pin the dispatcher
  contract so a future refactor that moves a handler out of the facade
  cannot silently break routing. Worth keeping as-is.
- **Swallow-and-continue tests.** `tests/test_workflow_cleanup.py::test_swallows_all_failures`
  (line 113) and the `_refresh_base_and_worktrees` per-worktree-exception
  cases in `tests/test_workflow_base_sync_unit.py` (around line 104) verify
  log-and-swallow paths — they assert no exception escapes and that
  surrounding state stayed sensible, which is the correct contract for a
  best-effort cleanup. Borderline by mechanical-assertion count, but
  semantically correct.

No tests were found that merely instantiate a value and assert
`is not None` without checking semantically important state, and no
"the function returned without raising" smoke patterns were observed
outside the categories above.

## Out of scope for this audit

Operator-owned controls listed in `docs/security.md` (branch protection,
2FA, secret scanning, required reviewers) sit on GitHub / org settings and
cannot be exercised from inside the repo. The audit is silent on these by
design — the doc itself flags them as N/A for code.

Worktree placement, restart-recovery, and worktree-cache semantics depend
on a real on-disk git layout; `tests/test_workflow_base_sync_real_git.py`
already drives an actual `git` binary, but exhaustive crash-recovery
matrices would have to land as integration tests rather than expansions
of the unit suite.
