# Existing Findings Remediation Plan

## Purpose

This working note tracks incremental cleanup of the existing style, structure, and complexity findings captured from
the latest `main` branch. The primary target is to eliminate all findings. If a small remainder cannot be removed
without harming readability or a public contract, the fallback target is to eliminate the main body of findings and
document every remainder.

The work is intentionally divided into bounded packages so it can be resumed in separate implementation sessions.
This note is progress tracking, not an authoritative behavior specification.

## Constraints

- Do not add a new linter, dependency, configuration file, or CI job during this cleanup.
- Do not add `# noqa` comments or blanket ignores merely to reduce the count.
- Keep the existing Ruff gate green throughout the work.
- Preserve workflow labels, pinned-state keys, comment markers, watermarks, event shapes, and public compatibility
  re-exports.
- Avoid unrelated formatting, speculative abstractions, and cross-subsystem refactors.
- Prefer a clear, tested implementation over satisfying a subjective rule mechanically.

## How to use this plan

For each implementation session:

1. Select the first useful unchecked work package that fits the available budget.
2. Add a row to the session log with the selected package.
3. Inspect the current code and tests; line numbers and surrounding code may have changed since the initial snapshot.
4. Implement only that package and run focused tests while iterating.
5. Run the complete validation gate before marking the package complete.
6. Change completed checkboxes from `[ ]` to `[x]`, update the stage progress, and finish the session-log row.
7. If a package is only partially completed, leave its main checkbox unchecked and record the exact next action in the
   session log.

Do not mark a stage complete until its completion gate is satisfied.

## Progress summary

| Stage | Goal | Packages complete | Status |
|---|---|---:|---:|
| 1 | Concrete formatting and correctness cleanup | 9/9 | [x] |
| 2 | Extreme production complexity hotspots | 8/8 | [x] |
| 3 | Remaining production complexity | 5/6 | [ ] |
| 4 | Remaining production style and structure | 0/5 | [ ] |
| 5 | Test structure and complexity | 0/7 | [ ] |
| 6 | Test literals and naming | 0/7 | [ ] |
| 7 | Long-tail cleanup and final verification | 0/5 | [ ] |

## Finding-count progress

Update the current column whenever a refreshed full scan is available. Package checkboxes remain the primary progress
signal between scans.

| Metric | Initial | Current | Target |
|---|---:|---:|---:|
| Parsed findings | 7,683 | 7,683 | 0 |
| Unique findings | 7,660 | 7,660 | 0 |
| Production findings | 1,468 | 1,468 | 0 |
| Test findings | 6,215 | 6,215 | 0 |
| Affected files | 172 | 172 | 0 |
| Standard `E...` findings | 29 | 29 | 0 |

Minimum acceptable fallback: reduce the total by at least 90%, leave no production correctness or formatting
findings, and explain every retained finding in the accepted-remainder register.

## Initial rule inventory

These rules account for most of the work and provide stable progress categories even when individual line numbers
move.

| Rule | Initial count | Main implementation approach |
|---|---:|---|
| `WPS432` magic numbers | 2,542 | Name domain values; consolidate fixture data |
| `WPS226` repeated strings | 983 | Reuse semantic constants and fixture builders |
| `WPS210` local variables | 505 | Extract cohesive helpers and intermediate models |
| `WPS111` short names | 439 | Rename unclear variables |
| `WPS204` repeated expressions | 392 | Store meaningful intermediate values |
| `WPS110` generic names | 325 | Use domain-specific names where they improve clarity |
| `WPS430` nested functions | 218 | Reuse module helpers, fakes, or small callable objects |
| `WPS213` expressions | 203 | Split setup and branch-heavy functions |
| `WPS358` float zero | 162 | Use an existing semantic constant where appropriate |
| `WPS338` method order | 161 | Reorder methods without changing behavior |
| `WPS441` control-variable reuse | 157 | Rename reused or shadowed variables |
| Other WPS rules | 1,567 | Resolve in the subsystem and long-tail stages |
| Standard `E...` rules | 29 | Correct whitespace and indentation directly |

## Validation gate for every completed package

- [ ] Focused tests for the changed module pass during implementation.
- [ ] `.venv/bin/python -m ruff check orchestrator tests` passes.
- [ ] `git diff --check origin/main...HEAD` passes.
- [ ] `.venv/bin/python -m pytest` passes.
- [ ] Added or modified tests have distinct coverage and no avoidable duplicated setup.
- [ ] Comments describe current invariants and contain no diff-relative wording.
- [ ] The package is recorded in the session log.

## Stage 1 — Concrete formatting and correctness cleanup

Goal: remove objective, low-risk findings before structural refactoring.

### Package 1.1 — Production blank-line findings

- [x] Correct the three production `E305` findings in `orchestrator/agents.py` and `orchestrator/config.py`.
- [x] Verify that only blank-line layout changes.

### Package 1.2 — Test blank-line findings

- [x] Correct `E303`, `E305`, and `E306` in:
  - `tests/test_analytics_read_conn_reuse.py`
  - `tests/test_workflow_decomposition_children.py`
  - `tests/test_workflow_drift.py`
  - `tests/test_workflow_fixing_routing.py`
  - `tests/test_workflow_in_review_checks.py`
  - `tests/test_workflow_scheduler_routing.py`
  - `tests/test_workflow_tick_parallel.py`

### Package 1.3 — Test continuation indentation

- [x] Correct `E127` and `E128` in:
  - `tests/test_dashboard.py`
  - `tests/test_main.py`
  - `tests/test_workflow_implementing_paused.py`
  - `tests/test_workflow_question.py`
  - `tests/test_workflow_scheduler_routing.py`
  - `tests/test_workflow_tick_parallel.py`
  - `tests/test_workflow_worktree_serialization.py`

### Package 1.4 — Inline-comment spacing

- [x] Correct `E261` in `tests/test_workflow_implementing_pr_reuse.py`.

### Package 1.5 — File resource handling

- [x] Replace the unscoped `open()` in `tests/test_reexport_surface.py` with `Path.read_text()` or a context
  manager.
- [x] Preserve the parsed source and encoding behavior.

### Package 1.6 — Small redundant constructs

- [x] Simplify the redundant subscript slice in `orchestrator/dashboard_charts.py`.
- [x] Remove the useless terminal `continue` in `orchestrator/skill_catalog.py`.
- [x] Confirm that sequence boundaries and loop behavior remain unchanged.

### Package 1.7 — Incorrect unused-name marker

- [x] Rename or clarify `_scheduler` in `orchestrator/main.py` so a used value is not marked as unused.
- [x] Preserve shutdown ordering and the watchdog-release behavior.

### Package 1.8 — Unpacking targets

- [x] Resolve the two unique production unpacking-target findings in `orchestrator/usage.py`.
- [x] Update the related parser tests and preserve all accepted provider payload shapes.

### Package 1.9 — Production control-variable reuse

- [x] Rename the reused variables in:
  - `orchestrator/dashboard_charts.py`
  - `orchestrator/github.py`
  - `orchestrator/state_machine.py`
- [x] Confirm that empty iterables cannot expose an unbound-variable path.

Completion gate: all 29 standard `E...` findings and the concrete findings above are gone.

## Stage 2 — Extreme production complexity hotspots

Goal: simplify the eight highest-complexity functions. Each checkbox is an independent work package and should
normally be implemented in its own PR or commit.

### Package 2.1 — Repository configuration parsing

- [x] Refactor [`_parse_repos_env()`](../orchestrator/config.py), initial score 78.
- [x] Extract repository-entry parsing, option validation, and duplicate detection.
- [x] Preserve exact validation errors, defaults, ordering, and environment semantics.

### Package 2.2 — Claude result parsing

- [x] Refactor [`_claude_last_message()`](../orchestrator/agents.py), initial score 66.
- [x] Separate event decoding, content-block collection, diagnostics, and final-result selection.
- [x] Preserve malformed-stream, error-result, and fallback behavior.

### Package 2.3 — Analytics summary query

- [x] Refactor [`get_summary()`](../orchestrator/analytics/read_rollup.py), initial score 49.
- [x] Separate filter construction, query execution, and row-to-model conversion.
- [x] Preserve connection lifecycle and empty-result behavior.

### Package 2.4 — Trajectory filtering

- [x] Refactor [`filter_runs()`](../orchestrator/trajectory_reader.py), initial score 48.
- [x] Extract filter normalization and independent predicates while retaining input order.
- [x] Preserve stable ordering and every existing filter combination.

### Package 2.5 — Agent-exit analytics

- [x] Refactor [`record_agent_exit()`](../orchestrator/analytics/__init__.py), initial score 43.
- [x] Separate payload normalization, redaction, event construction, and persistence.
- [x] Preserve secret handling and analytics schema.

### Package 2.6 — Pull-request base synchronization

- [x] Refactor [`_sync_pr_worktree_to_base()`](../orchestrator/base_sync.py), initial score 42.
- [x] Separate state probes, routing decisions, git operations, and event emission.
- [x] Preserve command ordering, recovery behavior, locks, and emitted events.

### Package 2.7 — Question-stage handler

- [x] Refactor [`_handle_question()`](../orchestrator/stages/question.py), initial score 41.
- [x] Extract precondition checks, session selection, prompt execution, and outcome routing.
- [x] Preserve labels, pinned-state fields, comments, retries, and resume behavior.

### Package 2.8 — Combined check state

- [x] Refactor [`pr_combined_check_state()`](../orchestrator/github.py), initial score 40.
- [x] Extract check normalization and status-priority folding.
- [x] Preserve GitHub status/check-run precedence and missing-data behavior.

Before Packages 2.2, 2.6, or 2.7, read [`docs/workflow.md`](../docs/workflow.md) and
[`docs/state-machine.md`](../docs/state-machine.md). Keep the `workflow.py` compatibility facade and late-bound
`_wf` calls intact.

Completion gate: all eight functions are decomposed into cohesive units with focused regression coverage and no
public-contract change.

## Stage 3 — Remaining production complexity

Goal: resolve the remaining production `WPS210`, `WPS211`, `WPS213`, `WPS220`, `WPS221`, `WPS222`,
`WPS231`, and `WPS232` findings.

Apply this sequence in every package:

1. Identify branch-heavy functions and write or locate characterization tests.
2. Extract decision-free parsing, normalization, formatting, and persistence helpers first.
3. Reduce locals by grouping already-cohesive data, not by hiding unrelated values in generic dictionaries.
4. Reduce arguments with existing domain objects or a narrowly scoped dataclass only when the values travel together.
5. Preserve public signatures when callers or compatibility modules depend on them.
6. Run focused tests after each extraction and the full validation gate before marking the package complete.

### Package 3.1 — Analytics reads and synchronization

- [x] Simplify `orchestrator/analytics/__init__.py`, `connection.py`, `predicates.py`, `query.py`,
  `read_dashboard.py`, `read_raw.py`, `read_rollup.py`, and `sync.py`.

### Package 3.2 — Dashboard rendering

- [x] Simplify `dashboard.py`, `dashboard_charts.py`, `dashboard_html.py`, `dashboard_kpis.py`,
  `dashboard_state.py`, `dashboard_theme.py`, and `trajectory_dashboard.py`.
- [x] Keep the optional dashboard dependency boundary intact.

### Package 3.3 — Agent, usage, configuration, and trajectory code

- [x] Simplify `agents.py`, `usage.py`, `main.py`, `config.py`, and `trajectory_reader.py`.
- [x] Preserve provider payload compatibility and subprocess-cleanup behavior.

### Package 3.4 — Git and worktree infrastructure

- [x] Simplify `base_sync.py`, `branch_publication.py`, `git_plumbing.py`, `verify.py`, and
  `worktree_lifecycle.py`.
- [x] Preserve authentication envelopes, lock boundaries, command order, and recovery routes.

### Package 3.5 — GitHub, scheduling, state, and shared workflow helpers

- [x] Simplify `github.py`, `scheduler.py`, `skill_catalog.py`, `state_machine.py`, `workflow_drift.py`,
  `workflow_messages.py`, and non-facade logic still present in `workflow.py`.

### Package 3.6 — Stage handlers

- [x] Simplify `orchestrator/stages/decomposition.py`.
- [ ] Simplify the remaining handlers under `orchestrator/stages/` one stage at a time.
- [ ] Keep cross-module calls late-bound through `from .. import workflow as _wf`.
- [ ] Keep stage-private helpers private and explicitly alias every facade re-export.
- [ ] Update the matching focused stage tests and documentation pointers after helper moves.

Completion gate: no production function remains above the selected complexity limits unless recorded in the
accepted-remainder register with a concrete contract or readability reason.

## Stage 4 — Remaining production style and structure

Goal: clear the production findings that are not covered by the complexity stages.

### Package 4.1 — Names

- [ ] Resolve production `WPS110`, `WPS111`, `WPS114`, `WPS115`, `WPS117`, and `WPS122` findings.
- [ ] Use domain names rather than mechanically lengthening identifiers.
- [ ] Update keyword callers and patch targets when a parameter or helper is renamed.

### Package 4.2 — Literals and repeated expressions

- [ ] Resolve production `WPS204`, `WPS226`, `WPS358`, and `WPS432` findings.
- [ ] Create constants only for real concepts such as statuses, limits, timeouts, field names, and protocol values.
- [ ] Keep constants close to their consumers unless they form a shared public contract.

### Package 4.3 — Strings and formatting

- [ ] Resolve production `WPS237`, `WPS336`, and related string-construction findings.
- [ ] Extract message or SQL fragments only when the resulting construction is easier to read and test.
- [ ] Preserve exact operator-visible messages and persisted content.

### Package 4.4 — Control flow and expression shape

- [ ] Resolve production negated-condition, nested-try, implicit-`.get()`, tuple-shape, deep-nesting, and long-try
  findings.
- [ ] Do not alter cleanup guarantees or exception boundaries solely to flatten code.
- [ ] Add branch-level tests when a rewrite changes evaluation order.

### Package 4.5 — Module and import structure

- [ ] Resolve production import-count, member-count, metadata, collision, and module-complexity findings where a
  cohesive split is possible.
- [ ] Do not break `workflow.py`, `worktrees.py`, analytics re-exports, or package compatibility surfaces merely
  to lower a count.
- [ ] Search AGENTS.md and the architecture/workflow documentation after moving a helper or module.

Completion gate: all feasible production findings are removed and every unavoidable compatibility-related remainder
is documented.

## Stage 5 — Test structure and complexity

Goal: reduce duplicated setup and structural complexity before addressing the high-volume literal and naming rules.

For every package:

1. Group findings by test class or behavior.
2. Merge cases that differ only by data using `pytest.mark.parametrize` or a small named loop.
3. Move reusable GitHub behavior into `tests/fakes.py` rather than adding more direct PyGithub mocks.
4. Replace repeated nested callbacks with module helpers or narrowly scoped callable recorders when that improves
   clarity.
5. Keep separate tests when their setup or protected contract is materially different.

### Package 5.1 — Analytics and dashboard tests

- [ ] Refactor analytics, analytics-read, dashboard, dashboard-chart, trajectory, and observability test modules.

### Package 5.2 — Agent, usage, main, and configuration tests

- [ ] Refactor `test_agents.py`, `test_usage.py`, `test_main.py`, `test_config.py`, and related helper tests.

### Package 5.3 — Scheduler, base-sync, git, and worktree tests

- [ ] Refactor scheduler-routing, base-sync, branch-publication, cleanup, serialization, and real-git test modules.

### Package 5.4 — Decomposition, question, and documenting tests

- [ ] Refactor decomposition, question, documenting, and their routing/paused/drift test modules.

### Package 5.5 — Implementing and fixing tests

- [ ] Refactor implementing, fixing, PR-reuse, retry, timeout, drift, and terminal test modules.

### Package 5.6 — Validating, in-review, and conflict tests

- [ ] Refactor validating, in-review, conflict-resolution, review-filtering, watermark, and handoff test modules.

### Package 5.7 — Shared and remaining tests

- [ ] Refactor `tests/fakes.py`, `tests/workflow_helpers.py`, and remaining small modules not covered above.
- [ ] Re-run the redundancy pass across helpers introduced by Packages 5.1–5.6.

Completion gate: test structure and complexity findings are removed without reducing behavior coverage or hiding
assertions behind overly generic helpers.

## Stage 6 — Test literals and naming

Goal: address the largest remaining volume: magic values, repeated strings, repeated expressions, and identifier
rules.

Apply these rules consistently:

- Name recurring issue numbers, PR numbers, timestamps, retry limits, timeouts, exit codes, costs, and status values.
- Prefer module-local constants or existing fixture builders over a repository-wide test-constant module.
- Parameterize genuinely equivalent cases instead of creating many one-use constants.
- Store repeated expressions in a named local only when the name explains their role.
- Rename short variables when their meaning is not immediately established by a tiny loop or comprehension.
- Move uppercase test-class attributes to module scope when they are true constants; otherwise use descriptive
  lowercase fixture attributes.
- Do not introduce shared abstractions between unrelated tests solely to remove duplicated literals.

### Package 6.1 — Analytics and dashboard test literals

- [ ] Resolve literal, repeated-expression, and naming findings in analytics, dashboard, and trajectory tests.

### Package 6.2 — Agent, usage, main, and configuration test literals

- [ ] Resolve the same rule groups in agent, usage, main, and configuration tests.

### Package 6.3 — Scheduler, base-sync, git, and worktree test literals

- [ ] Resolve the same rule groups in scheduler, synchronization, publication, and worktree tests.

### Package 6.4 — Decomposition, question, and documenting test literals

- [ ] Resolve the same rule groups in decomposition, question, and documenting tests.

### Package 6.5 — Implementing and fixing test literals

- [ ] Resolve the same rule groups in implementing and fixing tests.

### Package 6.6 — Validating, in-review, and conflict test literals

- [ ] Resolve the same rule groups in validating, in-review, and conflict tests.

### Package 6.7 — Shared and remaining test literals

- [ ] Resolve the same rule groups in shared fakes/helpers and every remaining test module.

Completion gate: high-volume test rules are cleared while individual test scenarios remain understandable.

## Stage 7 — Long-tail cleanup and final verification

### Package 7.1 — Refresh the inventory

- [ ] Run a fresh full scan against the completed branch when the same scanning environment is available.
- [ ] Update the finding-count progress table and add newly exposed findings to the appropriate package.

### Package 7.2 — Clear production remainder

- [ ] Resolve every remaining production finding that can be fixed without changing a public contract.
- [ ] Add focused tests for any late behavioral refactor.

### Package 7.3 — Clear test remainder

- [ ] Resolve remaining test findings file by file.
- [ ] Recheck that cleanup did not merge tests with materially different behavior.

### Package 7.4 — Review accepted remainder

- [ ] Challenge every entry in the accepted-remainder register and remove it if a clear implementation is now
  available.
- [ ] Confirm each retained entry protects readability or a documented compatibility contract rather than convenience.

### Package 7.5 — Final repository validation

- [ ] Run the full validation gate from a clean worktree.
- [ ] Update all progress tables and close every completed package.
- [ ] Confirm the final result reaches zero findings or the minimum acceptable fallback.

Completion gate: the primary zero-finding target is achieved, or at least 90% of findings are removed with no
unexplained remainder and no production correctness or formatting findings.

## Accepted remainder

Use this register only when a finding cannot be removed safely. Do not add an entry until a concrete refactor has been
considered.

### Issue scheduler submission API

- File and symbol: `orchestrator/scheduler.py: IssueScheduler.submit`
- Rule: `WPS211`
- Reason: The documented submission API accepts the issue identity and callback plus three independent keyword-only
  scheduling controls. Replacing those controls with an options object would break workflow dispatch callers and the
  direct scheduler API used throughout its regression suite while making the common call sites less explicit. The
  implementation immediately groups the values in `_Submission` and delegates reservation, logging, and dispatch.
- Protected by: `orchestrator/workflow.py`, `tests/test_scheduler.py`, and `tests/test_workflow_scheduler_routing.py`.
- Reviewed: [x]

### Agent-exit analytics context

- File and symbol: `orchestrator/analytics/__init__.py: record_agent_exit`
- Rule: `WPS211`
- Reason: The explicit keyword-only run context is called by workflow code and tests. A request object would break the
  established call contract, while `**kwargs` would discard useful typing and validation. Cohesive context helpers own
  the implementation.
- Protected by: `tests/test_analytics.py` and tracked-agent workflow callers.
- Reviewed: [x]

### Raw analytics facade readers

- File and symbols: `orchestrator/analytics/read_raw.py`: `get_event_breakdown`, `get_recent_agent_exits`, `get_issues`,
  and `get_issue_events`.
- Rule: `WPS211`
- Reason: These public readers expose one consistent keyword filter contract. A request object would break callers and
  `**kwargs` would weaken the API; each function delegates to a small filter/query helper.
- Protected by: `orchestrator/analytics/read.py` and `tests/test_analytics_read_*.py`.
- Reviewed: [x]

### Rollup analytics facade readers

- File and symbols: `orchestrator/analytics/read_rollup.py`: `get_summary`, `get_kpi_prev`, `get_time_series`,
  `get_stage_breakdown`, `get_backend_efficiency`, `get_repo_breakdown`, and `get_throughput_breakdown`.
- Rule: `WPS211`
- Reason: These public readers expose one consistent keyword filter contract. A request object would break callers and
  `**kwargs` would weaken the API; each function delegates to a small filter/query helper.
- Protected by: `orchestrator/analytics/read.py` and `tests/test_analytics_read_*.py`.
- Reviewed: [x]

### Dashboard analytics facade readers

- File and symbols: `orchestrator/analytics/read_dashboard.py`: `get_review_round_breakdown`,
  `get_skill_trigger_rates`, `get_skill_trigger_matrix`, `get_cost_coverage`, `get_backend_daily_tokens`, and
  `get_hourly_heatmap`.
- Rule: `WPS211`
- Reason: These public readers expose one consistent keyword filter contract. A request object would break callers and
  `**kwargs` would weaken the API; each function delegates to a small filter/query helper.
- Protected by: `orchestrator/analytics/read.py` and `tests/test_analytics_read_*.py`.
- Reviewed: [x]

### Dashboard topbar compatibility helper

- File and symbol: `orchestrator/dashboard_html.py: _topbar_html`
- Rule: `WPS211`
- Reason: The six explicit keyword arguments are part of the historical `orchestrator.dashboard` export surface. A
  request object would break callers and `**kwargs` would weaken the formatter contract.
- Protected by: `orchestrator.dashboard.__all__` and `tests/test_dashboard.py`.
- Reviewed: [x]

### Dashboard drill-down compatibility helper

- File and symbol: `orchestrator/dashboard.py: _render_drilldown`
- Rule: `WPS211`
- Reason: The seven explicit keyword arguments are part of the historical `orchestrator.dashboard` export surface. The
  adapter preserves that call contract while delegating implementation to typed page/filter state.
- Protected by: `orchestrator.dashboard.__all__` and `tests/test_dashboard.py`.
- Reviewed: [x]

### Auto-rebase recovery compatibility helper

- File and symbol: `orchestrator/base_sync.py: _recover_pending_auto_base_rebase`
- Rule: `WPS211`
- Reason: The explicit positional and keyword-only arguments are re-exported from `orchestrator.worktrees`. Replacing
  them with the typed recovery context would break direct callers and patch targets; the compatibility helper builds
  that context immediately and delegates the recovery flow.
- Protected by: `orchestrator.worktrees.__all__` and `tests/test_workflow_base_sync_unit.py`.
- Reviewed: [x]

### PR worktree synchronization compatibility helper

- File and symbol: `orchestrator/base_sync.py: _sync_pr_worktree_to_base`
- Rule: `WPS211`
- Reason: The explicit synchronization contract is re-exported through both `orchestrator.worktrees` and
  `orchestrator.workflow`. A request object would break existing callers and patch targets; the helper already groups
  its implementation state in `_AutoRebaseContext`.
- Protected by: the two compatibility facades and `tests/test_workflow_base_sync_unit.py`.
- Reviewed: [x]

### PR conflict-routing compatibility helper

- File and symbol: `orchestrator/base_sync.py: _route_pr_worktree_to_resolving_conflict`
- Rule: `WPS211`
- Reason: The explicit routing inputs are part of the `orchestrator.worktrees` compatibility surface and map directly
  to the persisted state and event fields. Replacing them with a request object would break callers and patch targets.
- Protected by: `orchestrator.worktrees.__all__`, the base-sync tests, and conflict event-emission tests.
- Reviewed: [x]

## Session log

Add one row for every implementation session, including partial sessions.

| Date | Package | Result | Validation | PR or commit | Exact next action |
|---|---|---|---|---|---|
| 2026-07-11 | 1.1 | Complete | E305, Ruff, diff, 2093 passed | Not committed | Start Package 1.2 |
| 2026-07-11 | 1.2 | Complete | E303/E305/E306, Ruff, diff, 2093 passed, 3 skipped | Not committed | Start Package 1.3 |
| 2026-07-11 | 1.3 | Complete | E127/E128, Ruff, diff, 2093 passed, 3 skipped | Not committed | Start Package 1.4 |
| 2026-07-11 | 1.4 | Complete | E261, 41 tests, Ruff, diff, 2093 passed, 3 skipped | Not committed | Start Package 1.5 |
| 2026-07-11 | 1.5 | Complete | 3 focused, Ruff, diff, 2093 passed, 3 skipped | Not committed | Start Package 1.6 |
| 2026-07-11 | 1.6 | Complete | WPS327/349, Ruff, diff, 2093 passed, 3 skipped | None | Start Package 1.7 |
| 2026-07-11 | 1.7 | Complete | WPS121, 31 focused, Ruff, diff, 2093 passed, 3 skipped | e2cae6d | Start Package 1.8 |
| 2026-07-11 | 1.8 | Complete | WPS414, 90 focused, Ruff, diff, 2093 passed, 3 skipped | Not committed | Package 1.9 |
| 2026-07-11 | 1.9 | Complete | WPS441, 77 focused, Ruff, diff, 2093 passed, 3 skipped | 403c4d6 | Start Package 2.1 |
| 2026-07-11 | 2.1 | Complete | Target WPS, 81 focused, Ruff, diff, 2094 passed, 3 skipped | d6216d1 | Package 2.2 |
| 2026-07-11 | 2.2 | Complete | WPS210/220/231, 55 focused, Ruff, diff, 2096 passed, 3 skipped | None | Package 2.3 |
| 2026-07-11 | 2.3 | Complete | WPS210/WPS231, 50, Ruff, diff, 2096 passed, 3 skipped | Not committed | Package 2.4 |
| 2026-07-11 | 2.4 | Complete | 90 focused; WPS/Ruff/diff; 2096 passed, 3 skipped | Not committed | 2.5 |
| 2026-07-11 | 2.5 | Complete | WPS210/WPS231, 112 focused; Ruff/diff; 2096 passed, 3 skipped | Not committed | 2.6 |
| 2026-07-11 | 2.6 | Complete | Target WPS, 113 focused; Ruff/diff; 2096 passed, 3 skipped | Not committed | 2.7 |
| 2026-07-12 | 2.7 | Complete | Target WPS, 90 focused; Ruff/diff; 2096 passed, 3 skipped | Not committed | 2.8 |
| 2026-07-12 | 2.8 | Complete | WPS210/WPS231, 10 focused; Ruff/diff; 2099 passed, 3 skipped | None | Package 3.1 |
| 2026-07-12 | 3.1 | Complete | Target WPS; focused; Ruff/diff; full suite | Not committed | Package 3.2 |
| 2026-07-12 | 3.2 | Complete | Target WPS; Ruff/diff; full suite | Not committed | Package 3.3 |
| 2026-07-13 | 3.3 | Complete | Target WPS; 360 focused; full gate | Not committed | Package 3.4 |
| 2026-07-13 | 3.4 | Complete | WPS (3 retained); 221 focused; full gate | Not committed | Package 3.5 |
| 2026-07-13 | 3.5 | Complete | WPS (1 retained); 217 focused; full gate | Not committed | Package 3.6 |
| 2026-07-13 | 3.6/decomposition | Complete | Target WPS; 104 focused; full gate | Not committed | `implementing.py` |
| 2026-07-15 | 3.6/implementing | Complete | Target WPS; 148 focused; full gate | Not committed | `validating.py` |

Package 3.1 retained 18 reviewed API findings and passed 2,099 tests, 3 skips, and 627 subtests.

Package 3.2 retained two reviewed `WPS211` compatibility findings. All 246 focused tests and 2,100 full tests passed;
3 live-Postgres tests were skipped because `ANALYTICS_TEST_DB_URL` was unset.

Package 3.4 retained three reviewed `WPS211` compatibility findings. All 221 focused tests and 2,100 full tests
passed; 3 live-Postgres tests were skipped because `ANALYTICS_TEST_DB_URL` was unset.

Package 3.5 retained one reviewed `WPS211` compatibility finding. All 217 focused tests and 2,100 full tests passed;
3 live-Postgres tests were skipped because `ANALYTICS_TEST_DB_URL` was unset.

Packages 2.8, 3.2, 3.3, 3.4, and 3.5 ran `tests/` because root collection was blocked by the unreadable ignored
database volume.

Package 3.6 handler progress: `decomposition.py` and `implementing.py` are clear of the Stage 3 complexity rules;
`validating.py`, `in_review.py`, `documenting.py`, `fixing.py`, and `conflicts.py` remain. The `implementing.py` pass
cleared its two remaining `WPS221` findings — the shared `silent_park_count` increment in `_park_session_limit` and
`_park_silent_failure` — by routing both through the new `_mark_agent_silent_park` persistence helper; no Stage 3
finding was retained. All 148 focused implementing tests and 2,082 full tests passed (32 skipped for the optional
dashboard and live-Postgres dependencies).
