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
| 4 | Remaining production style and structure | 5/5 | [x] |
| 5 | Test structure and complexity | 7/7 | [x] |
| 6 | Test literals and naming | 4/7 | [ ] |
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
- [x] Simplify the remaining handlers under `orchestrator/stages/` one stage at a time.
- [x] Keep cross-module calls late-bound through `from .. import workflow as _wf`.
- [x] Keep stage-private helpers private and explicitly alias every facade re-export.
- [x] Update the matching focused stage tests and documentation pointers after helper moves.

Completion gate: no production function remains above the selected complexity limits unless recorded in the
accepted-remainder register with a concrete contract or readability reason.

## Stage 4 — Remaining production style and structure

Goal: clear the production findings that are not covered by the complexity stages.

### Package 4.1 — Names

- [x] Resolve production `WPS110`, `WPS111`, `WPS114`, `WPS115`, `WPS117`, and `WPS122` findings.
- [x] Use domain names rather than mechanically lengthening identifiers.
- [x] Update keyword callers and patch targets when a parameter or helper is renamed.

### Package 4.2 — Literals and repeated expressions

- [x] Resolve production `WPS204`, `WPS226`, `WPS358`, and `WPS432` findings.
- [x] Create constants only for real concepts such as statuses, limits, timeouts, field names, and protocol values.
- [x] Keep constants close to their consumers unless they form a shared public contract.

### Package 4.3 — Strings and formatting

- [x] Resolve production `WPS237`, `WPS336`, and related string-construction findings.
- [x] Extract message or SQL fragments only when the resulting construction is easier to read and test.
- [x] Preserve exact operator-visible messages and persisted content.

### Package 4.4 — Control flow and expression shape

- [x] Resolve production negated-condition, nested-try, implicit-`.get()`, tuple-shape, deep-nesting, and long-try
  findings.
- [x] Do not alter cleanup guarantees or exception boundaries solely to flatten code.
- [x] Add branch-level tests when a rewrite changes evaluation order.

### Package 4.5 — Module and import structure

- [x] Resolve production import-count, member-count, metadata, collision, and module-complexity findings where a
  cohesive split is possible.
- [x] Do not break `workflow.py`, `worktrees.py`, analytics re-exports, or package compatibility surfaces merely
  to lower a count.
- [x] Search AGENTS.md and the architecture/workflow documentation after moving a helper or module.

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

- [x] Refactor analytics, analytics-read, dashboard, dashboard-chart, trajectory, and observability test modules.

### Package 5.2 — Agent, usage, main, and configuration tests

- [x] Refactor `test_agents.py`, `test_usage.py`, `test_main.py`, `test_config.py`, and related helper tests.

### Package 5.3 — Scheduler, base-sync, git, and worktree tests

- [x] Refactor scheduler-routing, base-sync, branch-publication, cleanup, serialization, and real-git test modules.

### Package 5.4 — Decomposition, question, and documenting tests

- [x] Refactor decomposition, question, documenting, and their routing/paused/drift test modules.

### Package 5.5 — Implementing and fixing tests

- [x] Refactor implementing, fixing, PR-reuse, retry, timeout, drift, and terminal test modules.

### Package 5.6 — Validating, in-review, and conflict tests

- [x] Refactor validating, in-review, conflict-resolution, review-filtering, watermark, and handoff test modules.

### Package 5.7 — Shared and remaining tests

- [x] Refactor `tests/fakes.py`, `tests/workflow_helpers.py`, and remaining small modules not covered above.
- [x] Re-run the redundancy pass across helpers introduced by Packages 5.1–5.6.

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

- [x] Resolve literal, repeated-expression, and naming findings in analytics, dashboard, and trajectory tests.

### Package 6.2 — Agent, usage, main, and configuration test literals

- [x] Resolve the same rule groups in agent, usage, main, and configuration tests.

### Package 6.3 — Scheduler, base-sync, git, and worktree test literals

- [x] Resolve the same rule groups in scheduler, synchronization, publication, and worktree tests.

### Package 6.4 — Decomposition, question, and documenting test literals

- [x] Resolve the same rule groups in decomposition, question, and documenting tests.

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

### Pinned-state payload attribute

- File and symbol: `orchestrator/github.py: PinnedState.data`
- Rule: `WPS110`
- Reason: `data` is the parsed pinned-state dict on the `PinnedState` model. It is constructed by keyword
  (`PinnedState(data=...)`), serialized directly (`json.dumps(state.data, sort_keys=True)`), and read as `.data`
  across the workflow, stage handlers, and tests. Renaming it is a migration of a live pinned-state contract, not a
  local cleanup; the `.get` / `.set` accessors already keep most callers off the attribute.
- Protected by: `orchestrator/github.py`, `orchestrator/workflow.py`, `orchestrator/stages/`, and `tests/`.
- Reviewed: [x]

### Trajectory step content field

- File and symbol: `orchestrator/_usage_trajectory.py: TrajectoryStep.content`
- Rule: `WPS110`
- Reason: `content` is the serialized field name emitted by `AgentTrajectory.to_dict()` (the `"content"` key) and read
  back as `.content` by the trajectory reader, dashboard, and analytics. The field mirrors its persisted key, so a
  rename would either desync the two or migrate the serialized trajectory schema.
- Protected by: `orchestrator/trajectory_reader.py`, `orchestrator/trajectory_dashboard.py`,
  `orchestrator/analytics/__init__.py`, and `tests/test_trajectory_reader.py`.
- Reviewed: [x]

### Analytics event-recording result inputs

- File and symbols: `orchestrator/analytics/__init__.py`: `record_stage_evaluation` and `record_agent_exit` (the
  `result` parameter).
- Rule: `WPS110`
- Reason: `result` is part of the public keyword contract of these `analytics.__all__` recorders; workflow code calls
  them as `record_stage_evaluation(result=...)` / `record_agent_exit(result=...)`. Renaming the parameter would break
  the established keyword call sites the same way a request object would (see the sibling `WPS211` entries).
- Protected by: `orchestrator.analytics.__all__`, `orchestrator/workflow.py`, and `tests/test_analytics.py`.
- Reviewed: [x]

### Issue-event row result field

- File and symbol: `orchestrator/analytics/read_models.py: IssueEventRow.result`
- Rule: `WPS110`
- Reason: `result` is a field of the frozen public `IssueEventRow` read model exported through
  `orchestrator.analytics.read.__all__` and consumed as `ev.result` by the dashboard. Renaming it changes the public
  read-model shape.
- Protected by: `orchestrator.analytics.read.__all__`, `orchestrator/dashboard.py`, and the analytics read tests.
- Reviewed: [x]

### Dashboard preset identifiers

- File and symbols: `orchestrator/dashboard_state.py` and `orchestrator/dashboard.py`: `PRESET_3D` and `PRESET_7D`.
- Rule: `WPS114`
- Reason: The `3D` / `7D` suffixes name the 3-day and 7-day dashboard windows and are part of the historical
  `orchestrator.dashboard` export surface (`__all__`), reached by tests as `dashboard.PRESET_3D` /
  `dashboard.PRESET_7D`. Any spelling that satisfies `WPS114` renames a public export.
- Protected by: `orchestrator.dashboard.__all__` and `tests/test_dashboard.py`.
- Reviewed: [x]

### Public formatter and query-parser inputs

- File and symbols: `orchestrator/dashboard_theme.py` (`fmt_money`, `fmt_money_exact`, `fmt_tokens`, `fmt_num`:
  `value`), `orchestrator/dashboard_charts_cost.py` (`cost_horizontal_bars`: `items`), and
  `orchestrator/dashboard_skill_matrix.py` (`parse_skill_matrix_sort`: `params`).
- Rule: `WPS110`
- Reason: These are public dashboard functions whose parameter name is a keyword-call contract -- a caller may pass
  `fmt_money(value=...)`, `cost_horizontal_bars(items=...)`, or `parse_skill_matrix_sort(params=...)`. Renaming the
  parameter turns a previously valid keyword call into a `TypeError`; only internal locals were renamed.
- Protected by: `tests/test_dashboard_theme.py`, `tests/test_dashboard_charts.py`, and `tests/test_dashboard.py`,
  which each assert the keyword call.
- Reviewed: [x]

### Dashboard HTML export helpers

- File and symbols: `orchestrator/dashboard_html.py`: `_delta_pill` (the `value` parameter) and `_sparkline_svg` (the
  `values`, `w`, and `h` parameters).
- Rule: `WPS110` (`value`, `values`) and `WPS111` (`w`, `h`)
- Reason: Both helpers are re-exported through `orchestrator.dashboard.__all__`, so despite the leading underscore
  their parameter names are a historical keyword contract -- `dashboard._delta_pill(value=...)` and
  `dashboard._sparkline_svg(values=..., w=..., h=...)` must keep working. Renaming a parameter turns a previously
  valid facade keyword call into a `TypeError`; only the `cls` -> `css_class` body local in `_delta_pill` was renamed.
- Protected by: `orchestrator.dashboard.__all__` and `tests/test_dashboard.py`, which assert the facade keyword calls.
- Reviewed: [x]

### Workflow-label coercion inputs

- File and symbols: `orchestrator/state_machine.py`: `coerce_workflow_label` and `coerce_child_issue_label` (the
  `value` parameter).
- Rule: `WPS110`
- Reason: Both are public typo guards called across the workflow, the GitHub client, and tests; `value` is their
  keyword-call contract, so renaming it would break `coerce_workflow_label(value=...)` callers.
- Protected by: `orchestrator/github.py`, `orchestrator/state_machine.py`, and `tests/test_state_machine.py`, which
  asserts the keyword call.
- Reviewed: [x]

### Trajectory view content fields and record parser

- File and symbols: `orchestrator/_trajectory_records.py`: `TrajectoryStepView.content` and `TimelineEntry.content`
  (fields) plus `parse_record` (the `obj` parameter). Re-exported through `orchestrator/trajectory_reader.py`.
- Rule: `WPS110`
- Reason: `content` is the public view-model field constructed and read as `.content` by the trajectory dashboard and
  tests (mirroring the serialized `"content"` key); `obj` is `parse_record`'s keyword-call contract. Renaming either
  breaks construction, attribute access, or a keyword call. Only internal locals (e.g. the step loop var) were renamed.
- Protected by: `orchestrator/trajectory_dashboard.py`, `tests/test_trajectory_reader.py`, and
  `tests/test_trajectory_dashboard.py`.
- Reviewed: [x]

### Float-zero numeric defaults and rate floors

- File and symbols: `orchestrator/analytics/read_models.py` (the `*_cost_usd` frozen-dataclass field
  defaults and the two `skill_trigger_rate` `else 0.0` returns), `orchestrator/analytics/sync.py`
  (`duration_s` field default), `orchestrator/dashboard_charts_usage.py` (`_empty_token_bucket` band
  seeds), `orchestrator/dashboard_kpi_strip.py` (the `[0.0, 0.0]` daily cost/token accumulator and the
  rework-share `else 0.0`), `orchestrator/dashboard_widgets.py` (the `_topbar_html(spend_in_range=0.0)`
  empty-window call), `orchestrator/dashboard_html.py` (`_relative_width_pct` zero return),
  `orchestrator/dashboard_cards.py` (`_safe_ratio` zero return), `orchestrator/dashboard_kpis.py` (the
  success-rate `else 0.0`), and `orchestrator/trajectory_reader.py` (`total_cost_usd` default).
- Rule: `WPS358`
- Reason: Each is a genuine float zero -- a typed `float` field/keyword default, an accumulator seed,
  or a rate/ratio floor. The literal cannot be dropped without either `float(0)` / `float()` (a
  linter dodge that reads worse than `0.0`) or a `_ZERO = 0.0` constant whose own definition
  re-triggers `WPS358`. Every place a value is coerced was rewritten to `float(x or 0)`, so only the
  irreducible zeros remain.
- Protected by: `tests/test_analytics_read_*.py`, `tests/test_analytics_read_cost_cell.py`,
  `tests/test_dashboard*.py`, and `tests/test_trajectory_reader.py`.
- Reviewed: [ ]

### Terminal state writes and inherent per-branch repetition

- File and symbols: `gh.write_pinned_state(issue, state)` in `orchestrator/stages/decomposition.py`,
  `orchestrator/stages/implementing.py`, and `orchestrator/stages/validating.py`;
  `context.gh.write_pinned_state(context.issue, context.state)` and
  `context.state.set(_PENDING_PUSH_SHA, None)` in `orchestrator/base_sync.py`;
  `_wf._resolve_branch_name(state, spec, issue.number)` in `orchestrator/stages/implementing.py`;
  `str(wt)` in `orchestrator/worktree_lifecycle.py`; and `row[0]` / `row[1]` in
  `orchestrator/analytics/read_dashboard.py` / `orchestrator/analytics/read_rollup.py`.
- Rule: `WPS204`
- Reason: These are void terminal calls (persist the pinned state) or
  trivial value expressions that each independent branch -- or each single-column row mapper --
  issues exactly once. Wrapping a repeated call in a helper produces the SAME repeated call
  expression and still trips `WPS204`; there is no single scope to hoist a `row[0]` / `str(wt)`
  intermediate into. The stateless-persist design (write pinned state before every branch return) is
  the source of the repetition.
- Protected by: the stage-handler suites (`tests/test_workflow_<stage>.py` family),
  `tests/test_workflow_base_sync_unit.py`, `tests/test_analytics_read_*.py`, and the dashboard tests.
- Reviewed: [ ]

### Positional row/column indices

- File and symbols: `row[11]`..`row[14]` in `orchestrator/analytics/read_raw.py` and `row[11]` in
  `orchestrator/analytics/read_dashboard.py`.
- Rule: `WPS432`
- Reason: These are DB column indices that position-match the `SELECT` list of the query feeding each
  row mapper. wemake flags only the indices above its 0--10 allowlist, so naming a lone index in an
  otherwise-literal positional sequence (`row[8]`, `row[9]`, `row[10]`, `row[11]`, ...) fragments the
  column layout instead of clarifying it.
- Protected by: `tests/test_analytics_read_tables.py` and `tests/test_analytics_read_breakdowns.py`.
- Reviewed: [ ]

### Axis-step ladder and one-off chart heights

- File and symbols: `orchestrator/dashboard_charts_usage.py`: the `2.5` rung of the nice-number tick
  ladder in `_nice_axis_max`, and the one-off empty-state height `330`; and the one-off empty-state
  height `150` in `orchestrator/dashboard_charts_throughput.py`.
- Rule: `WPS432`
- Reason: `2.5` is one step of the standard `1 / 2 / 2.5 / 5 / 10` "nice tick" ladder, whose literal
  rungs read as the algorithm; wemake flags only `2.5` (the rest fall inside its allowlist). `330` and
  `150` are empty-state chart heights used once each, where a named constant used a single time adds
  no clarity. The reused default height was named `_DEFAULT_CHART_HEIGHT`.
- Protected by: `tests/test_dashboard_charts.py`.
- Reviewed: [ ]

### Git CLI subcommand and flag tokens

- File and symbols: the `-c` config-override flag in `orchestrator/git_plumbing.py` and the
  `worktree` / `add` / `remove` / `--force` / `rev-parse` / `--verify` tokens in
  `orchestrator/worktree_lifecycle.py`.
- Rule: `WPS226`
- Reason: These are git argument tokens assembled into command-argument lists. A literal
  `["git", "worktree", "remove", "--force", str(wt)]` reads as the exact `git worktree remove --force`
  invocation; a constant per token (or a `_C = "-c"`) obscures the command without adding meaning. The
  `git` executable name and the `fetch` operation, which do carry meaning, were named.
- Protected by: `tests/test_workflow_*worktree*.py` and the conflict/base-sync git suites.
- Reviewed: [ ]

### Plotly layout and trace dict keys

- File and symbols: `orchestrator/dashboard_charts_usage.py`: the plotly trace key `color` (the
  per-backend / per-band `usage_over_time` stack colors). The other plotly keys (`height`, `paper`,
  `size`, `margin`, `text`, `yaxis`, and the single-character `t` / `h` / `y`) no longer repeat within a
  single module now that the chart families live in separate leaves, so those findings are gone.
- Rule: `WPS226`
- Reason: `color` is plotly's own trace-dictionary vocabulary. Naming it forces a reader to dereference a
  constant to recover the plotly attribute it stands for, which is less clear than the literal API key.
- Protected by: `tests/test_dashboard_charts.py`.
- Reviewed: [ ]

### Dashboard KPI-tile and stack-mode dict keys

- File and symbols: `orchestrator/dashboard_kpi_strip.py`: the KPI-tile dict keys `label`, `value`,
  `delta`, `sub`, `spark`; and `orchestrator/dashboard_widgets.py`: the `type` / `backend` stack-mode
  option values.
- Rule: `WPS226`
- Reason: The KPI-tile keys are the contract between the KPI builder in `dashboard_kpi_strip.py` and the
  HTML renderer in `dashboard_html.py`; they read clearest as the same literal keys at both ends, and
  `value` additionally cannot become a constant without tripping `WPS110` (a blacklisted generic
  name). `type` / `backend` are the two stack-mode radio option values in `dashboard_widgets.py`.
- Protected by: `tests/test_dashboard.py`.
- Reviewed: [ ]

### Categorical chart palette hues

- File and symbols: `orchestrator/dashboard_theme.py`: the hex hues `#e0913a`, `#d9534a`, and
  `#5b6cf0`.
- Rule: `WPS226`
- Reason: These three hues are reused across independent categorical color maps (token bands,
  backends, review-round buckets, workflow statuses, labels). The hex at each map entry documents the
  exact rendered color; a shared constant would either be a meaningless hue name or -- because two of
  the hues coincide with the semantic `WARNING` / `DANGER` delta colors -- wrongly couple unrelated
  concerns.
- Protected by: `tests/test_dashboard_theme.py` and `tests/test_dashboard_charts.py`.
- Reviewed: [ ]

### Numeric format-spec f-strings

- File and symbols: the money `,.2f` specs in `orchestrator/dashboard_kpi_strip.py` (`_cost_per_resolved`)
  and `orchestrator/dashboard_html.py` (`_money_or_dash`); the zero-pad `02d` hour label in
  `orchestrator/dashboard_charts_heatmap.py` (`hour_weekday_heatmap`); and the dynamic-precision
  `.{decimals}f` / `,.{decimals}f` specs in `orchestrator/dashboard_theme.py` (`fmt_tokens`) and
  `orchestrator/_trajectory_dashboard_html.py` (`_fmt_cost_usd`).
- Rule: `WPS237`
- Reason: `WPS237` accepts only a single simple format spec (e.g. `.2f`, `<10`, `x`, `,`); a combined
  or nested-placeholder spec such as `,.2f`, `02d`, or `.{decimals}f` trips the rule even when the
  interpolated value is a plain name. Each of these is an idiomatic numeric format on the lowest-level
  dashboard formatter, and the interpolation itself was already reduced to a bound local. The spec
  cannot be dropped without either changing the rendered money/token/hour string (forbidden -- these
  are operator-visible) or replacing `f"{x:,.2f}"` with a `format(x, ",.2f")` / `"{0:,.2f}".format(x)`
  call that reads worse than the f-string it dodges.
- Protected by: `tests/test_dashboard.py`, `tests/test_dashboard_charts.py`,
  `tests/test_dashboard_theme.py`, and `tests/test_trajectory_dashboard.py`.
- Reviewed: [ ]

### Hashable dashboard cache key

- File and symbol: `orchestrator/dashboard_state.py: cache_key`
- Rule: `WPS227`
- Reason: `cache_key` returns the six window-scoped filter fields (`start`, `end`, `repo`, `events`,
  `stages`, `issue`) as one hashable value used directly as a memoization key by the cached readers,
  and it is part of the historical `orchestrator.dashboard` export surface (`__all__`). `WPS227`'s
  suggested fixes do not apply: a list is unhashable, and `tests/test_dashboard.py` asserts the return
  equals a plain six-tuple and is `hash()`-able. The sibling internal sort key
  (`read_dashboard._skill_matrix_order_key`) was converted to a list because it is only ever a
  `sorted(key=...)` argument; this one cannot be.
- Protected by: `orchestrator.dashboard.__all__` and `tests/test_dashboard.py`.
- Reviewed: [ ]

### Decompose-handler cleanup guarantee

- File and symbol: `orchestrator/stages/decomposition.py: _handle_decomposing`
- Rule: `WPS229` and `WPS501`
- Reason: The `try` / `finally` guarantees the decompose worktree is cleaned up unless the completed
  run marked it keep-on-inspection. `run_plan` is rebound by `_prepare_decomposer_run` and then mutated
  in place by `_process_decomposer_run` (which sets `keep_worktree=True` before its own park/persist can
  raise), and the `finally` must observe that final `run_plan` even when `_process_decomposer_run`
  raises mid-run. Collapsing the two-statement `try` body to one loses the post-`_prepare` binding on
  the `_process` failure path, and relocating the identical `finally` into a decorated context manager
  would only dodge `WPS501` without changing behavior or clarity -- exactly the "alter cleanup
  guarantees solely to flatten code" the package forbids.
- Protected by: `tests/test_workflow_decomposition_*.py`.
- Reviewed: [ ]

### Force-exit shutdown finally

- File and symbol: `orchestrator/main.py: _shutdown`
- Rule: `WPS501`
- Reason: The `finally: os._exit(...)` guarantees the process exits even if
  `agents.terminate_all_running` raises, including a `BaseException` such as a nested signal. There is
  no resource to manage and therefore no `with`-statement equivalent; rewriting it as `try` / `except
  Exception` plus an unconditional `os._exit` would silently change the `BaseException` force-exit
  semantics. The clean resource-cleanup try/finally blocks (analytics read connection, question-run
  worktree teardown, scheduler drain) were converted to context managers instead.
- Protected by: `tests/test_main.py`.
- Reviewed: [ ]

### Nice-number axis-tick float comparison

- File and symbol: `orchestrator/dashboard_charts_usage.py: _nice_axis_max`
- Rule: `WPS459`
- Reason: The `norm <= 2.5` rung buckets a normalized magnitude against the standard
  `1 / 2 / 2.5 / 5 / 10` "nice tick" ladder (the same `2.5` rung already recorded as a `WPS432`
  remainder). `2.5` is exactly representable in binary floating point, so the representation-error
  concern `WPS459` targets does not arise, and rewriting the `<=` comparison to dodge the float literal
  (e.g. scaling by two to `2 * norm <= 5`) would obscure the ladder without changing the result.
- Protected by: `tests/test_dashboard_charts.py`.
- Reviewed: [ ]

### Core workflow and worktree compatibility facades

- File and symbols: `orchestrator/workflow.py` (`WPS201` imports 127, `WPS202` members 57, `WPS203` imported names 137,
  `WPS410` `__all__`) and `orchestrator/worktrees.py` (`WPS201` 52, `WPS203` 52, `WPS410` `__all__`).
- Rule: `WPS201`, `WPS202`, `WPS203`, `WPS410`
- Reason: These two modules are the historical `workflow.<name>` / `worktrees.<name>` re-export surfaces -- the same
  compatibility-hub role already accepted for `orchestrator.dashboard` and `orchestrator.analytics`. Live issues, the
  stage handlers (which reach helpers through `from orchestrator import workflow as _wf` and call `_wf._foo`), and the
  test suite bind against these names, so the import fan-in, member count, and re-exported-name count are the surface
  itself; shrinking any of them is a migration of that surface, not a cleanup. The `__all__` inventory is what makes the
  re-export surface auditable in one place and governs `from orchestrator.workflow import *`. The removable
  import-collision (`WPS458` on `orchestrator.config`) was fixed on `workflow.py` by dropping the duplicate
  `from orchestrator.config import RepoSpec` and qualifying uses as `config.RepoSpec`.
- Protected by: `orchestrator/stages/`, `tests/test_reexport_surface.py`, and the `tests/test_workflow_*` families.
- Reviewed: [x]

### Root package initialization

- File and symbol: `orchestrator/__init__.py` (`__version__` and package-init wiring).
- Rule: `WPS410` (`__version__`) and `WPS412` (logic in `__init__.py`)
- Reason: `__version__` is the standard distribution version metadata read by packaging tooling, and the `__init__.py`
  performs ordinary package-level wiring. This is the same accepted package-init shape already recorded for
  `orchestrator/analytics/__init__.py`; neither can move without changing the package's public metadata or import
  behavior.
- Protected by: package import and `orchestrator.__version__` consumers.
- Reviewed: [x]

### Cohesive core, orchestration, and stage-handler module magnitude

- File and symbols: the git / worktree and shared-workflow modules `orchestrator/base_sync.py` (60 members / 18
  imports), `orchestrator/workflow_messages.py` (51), `orchestrator/agents.py` (40 / 16), `orchestrator/main.py`
  (25 / 17), `orchestrator/worktree_lifecycle.py` (24), `orchestrator/branch_publication.py` (21 / 13),
  `orchestrator/config.py` (18), `orchestrator/github.py` (17 / 15), `orchestrator/git_plumbing.py` (14),
  `orchestrator/skill_catalog.py` (12), `orchestrator/verify.py` (12), `orchestrator/_repo_config.py` (11),
  `orchestrator/state_machine.py` (10), and `orchestrator/workflow_drift.py` (9); and the per-stage handlers
  `orchestrator/stages/implementing.py` (68 / 47, plus `WPS203` imported names 51), `stages/validating.py` (59 / 42),
  `stages/decomposition.py` (54 / 40), `stages/documenting.py` (36 / 35), `stages/fixing.py` (33 / 25),
  `stages/conflicts.py` (32 / 32), `stages/in_review.py` (25 / 22), and `stages/question.py` (22 / 20).
- Rule: `WPS201` (imports), `WPS202` (members), and `WPS203` (imported names, `stages/implementing.py`)
- Reason: Each is a single-responsibility unit already reduced to its cohesive minimum by the Stage 2--3 complexity work
  and the earlier 4.5 slices. The `WPS202` limit of 7 sits far below any real orchestration or stage module, and a
  further member split would only fragment one cohesive concern -- or relocate it to a new leaf that carries the same
  accepted member-count magnitude the analytics / dashboard cohesive leaves already establish -- while the constraint is
  explicit that these surfaces must not be broken merely to lower a count. The `WPS201` / `WPS203` import fan-in is the
  set of collaborators a per-tick coordinator or a stage handler inherently drives; trimming it would demand indirection
  that hides the dependency rather than clarifying it, and `stages/implementing.py`'s 51 imported names fall to 50-and-
  below only if that single stage is itself split. The findings that WERE removable across these modules -- the
  `WPS458` `orchestrator.config` import collisions, the `WPS300` relative `from ..` imports in `stages/implementing.py`,
  and the `WPS301` / `WPS458` `logging` collision in `main.py` -- are fixed.
- Protected by: `tests/test_workflow_*`, `tests/test_main.py`, `tests/test_agents.py`, `tests/test_config.py`,
  `tests/test_state_machine.py`, and the git / worktree suites.
- Reviewed: [x]

### Per-test module-reload idiom

- File and symbols: the `_reload` helpers in `tests/test_analytics.py` (93 call sites) and the
  `_reload_reader_world` helper in `tests/test_trajectory_reader.py`; the dotted
  `import orchestrator.config as config` / `import orchestrator.analytics as analytics` inside them and the
  `_, analytics = _reload(...)` unpack at every call site.
- Rule: `WPS301` (dotted raw import) and `WPS204` (the repeated `(_, analytics)` unpack target).
- Reason: Each test loads the analytics/config modules fresh against a hermetic env by `sys.modules.pop(...)`
  followed by a dotted `import X as Y`. The dotted form is load-bearing: `from orchestrator import analytics`
  rebinds the *stale* package attribute and skips the reload, so `WPS301`'s suggested `from`-import silently
  breaks every reload test. The `(_, analytics)` unpack is the one-line-per-test cost of that hermetic-reload
  design; hoisting it would require a shared mutable module handle that defeats the per-test isolation.
- Protected by: `tests/test_analytics.py` and `tests/test_trajectory_reader.py`.
- Reviewed: [x]

### Agent-wire-format fixture schema keys

- File and symbols: the `claude` / `codex` stream-json fixture builders in `tests/test_analytics.py`,
  `tests/test_agents.py`, and `tests/test_usage.py` (`_claude_stdout_with_skills`, `_assistant`, `_tool_use`,
  `_codex_cmd`, and the streaming/trajectory record fixtures) — the literal dict keys `type`, `tool_use`,
  `assistant`, `message`, `model`, `content`, `usage`, `system`, `subtype`, `init`, `id`, `session_id`, `input`,
  `input_tokens`, `output_tokens`, `name`, `text`, and `result`, plus short correlation identifiers and payload
  examples that make started/completed or tool-use/result pairs visible in place.
- Rule: `WPS226`
- Reason: These keys reproduce the exact on-the-wire `claude` / `codex` stream-json envelope the extractor
  parses. A fixture that reads as the literal payload (`{"type": "tool_use", "name": "Skill", ...}`) documents
  the wire contract under test; replacing each key with a constant forces a reader to dereference names to
  recover the provider schema and desyncs the fixture from the real stream it mimics. The analytics-record
  field keys the tests own (`steps`, `turns`, `backend`, ...) and recurring tool / event / status values are already
  named module constants.
- Protected by: `tests/test_analytics.py`, `tests/test_agents.py`, and `tests/test_usage.py`.
- Reviewed: [x]

### Single-use fixture-payload magic numbers and float-zeros in test data

- File and symbols: per-fixture cost / token / count / duration literals used once or twice across
  `tests/test_dashboard_charts.py`, `tests/test_analytics_read_tables.py`,
  `tests/test_analytics_read_breakdowns.py`, `tests/test_analytics.py`, `tests/test_usage.py`, and the trajectory
  tests, plus the genuine float-typed `0.0` cost / rate defaults (`total_cost_usd`, per-backend cost cells,
  expected-value arrays).
- Rule: `WPS432` (magic number) and `WPS358` (float zero).
- Reason: Each such literal is a single scenario's expected value. Naming it produces a single-use constant
  that adds no meaning, and the recurring numeric values are coincidental collisions across unrelated fixture
  fields (e.g. one stage's `cache_cost_usd` sharing a value with a different row's `total_cost_usd`), so a
  shared name would mislead rather than clarify. The usage-parser rate multipliers stay beside the expected-cost
  formulas they audit; moving each SKU's one-off price components to distant constants makes those formulas harder
  to verify against the fixture. The `0.0` defaults are genuine float zeros a `0` literal would mistype. Only
  values that recur with one domain meaning (issue / PR numbers, retry limits, the fixture year, cumulative token
  totals, shared reported costs) were named. This mirrors the production single-use `WPS432` and `WPS358`
  remainders already registered above.
- Protected by: `tests/test_dashboard_charts.py`, `tests/test_analytics_read_*.py`, `tests/test_analytics.py`,
  `tests/test_usage.py`, and the trajectory tests.
- Reviewed: [x]

### Hermetic entry-point reload and dispatch calls

- File and symbols: `_reload_main(_LEGACY_ENV)`, the `GitHubClient` / `workflow.tick` patch expressions,
  `main_mod.main(_ONCE_ARGS)`, and `main_mod._run_tick(clients, sched)` in `tests/test_main.py`.
- Rule: `WPS204`
- Reason: Each scenario reloads module-level configuration, installs the exact dispatch boundary it exercises, and
  directly invokes the entry point or one-tick coordinator. Hiding those calls behind a generic runner would make
  signal, scheduler, barrier, and patch lifetimes harder to audit while merely moving the repeated expression to a
  helper. Stable environment keys, argv, patch attributes, repo slugs, timing budgets, and exit-code components are
  already named in `tests/main_helpers.py`.
- Protected by: the 31 focused `tests/test_main.py` scenarios.
- Reviewed: [x]

### Direct usage-parser calls and field assertions

- File and symbols: the repeated `parse_claude_*` / `parse_codex_*` calls, optional-cost guards, and serialized
  `decoded[STEPS_FIELD]` assertions in `tests/test_usage.py`.
- Rule: `WPS204`
- Reason: Each test directly names the parser under test and independently asserts the fields its scenario protects.
  A generic parse/assert wrapper would obscure whether a Claude, Codex, skill, or trajectory parser is exercised and
  would hide distinct cost and serialization checks behind shared control flow. Repeated input shapes and protocol
  values with one domain meaning have already been consolidated.
- Protected by: the 102 focused `tests/test_usage.py` scenarios.
- Reviewed: [x]

### Scheduler and base-sync scenario density

- File and symbols: scenario tests in `tests/test_scheduler.py`, `tests/test_workflow_scheduler_routing.py`,
  `tests/test_workflow_base_sync_unit.py`, `tests/test_workflow_base_sync_real_git.py`, and
  `tests/test_workflow_worktree_serialization.py`.
- Rule: `WPS204`, `WPS210`, and `WPS213`.
- Reason: Shared schedulers, event gates, patch bundles, git results, state recorders, and worktree fixtures were
  extracted. Stable repo keys, wait budgets, command and event fields, and fixture identities are named. The remaining
  expressions and locals describe distinct ordered calls, state transitions, concurrency signals, or per-field
  outcomes. Further extraction either hides the scenario sequence behind a generic assertion helper or merely trades
  repeated expressions for additional locals.
- Protected by: the 167 focused Package 5.3 tests.
- Reviewed: [x]

### Scheduler and worktree cleanup barriers

- File and symbols: `tests/test_workflow_scheduler_routing.py: _SequentialIssueProcessor.__call__` and
  `tests/test_workflow_worktree_serialization.py: _ConcurrencyProbe.record`.
- Rule: `WPS501`.
- Reason: Each `finally` decrements an in-flight counter even when a gated worker times out or raises. Moving the same
  counter release into another context manager would relocate, rather than simplify, the cleanup guarantee.
- Protected by: scheduler family-bucket serialization and target-root plumbing concurrency tests.
- Reviewed: [x]

### Future callback registration race hook

- File and symbol: `tests/test_scheduler.py: ShutdownDrainRaceTest.test_shutdown_waits_for_callback_registration`
  (`gated_add`).
- Rule: `WPS430`.
- Reason: The callback must close over the original bound `Future.add_done_callback` method and two event gates while
  the test patches the descriptor. A module callable would need to expose that one-test timing bundle as mutable
  state and would obscure the exact registration race being exercised.
- Protected by: `ShutdownDrainRaceTest.test_shutdown_waits_for_callback_registration`.
- Reviewed: [x]

### Cohesive infrastructure-test shapes

- File and symbols: the Package 5.3 modules carrying `WPS202`, the workflow-helper imports in
  `tests/test_workflow_scheduler_routing.py`, and `_RefreshBaseRealGitFixture` in
  `tests/test_workflow_base_sync_real_git.py`.
- Rule: `WPS202`, `WPS230`, and `WPS235`.
- Reason: The modules are already divided by infrastructure contract and their formerly oversized classes were split
  into behavior groups. Splitting files at the seven-member threshold would duplicate setup across artificial module
  boundaries. The real-git fixture's seven public paths are named filesystem seams used by its tests, and the helper
  imports keep the workflow labels explicit at use sites.
- Protected by: the 167 focused Package 5.3 tests.
- Reviewed: [x]

### Decomposition, question, and documenting scenario density

- File and symbols: scenario tests across `tests/test_workflow_decomposition_*.py`,
  `tests/test_workflow_question*.py`, and `tests/test_workflow_documenting*.py`.
- Rule: `WPS204`, `WPS210`, and `WPS213`.
- Reason: Shared stage runners, issue-family seeds, documenting fixtures, question-round assertions, and ordered-write
  recorders were extracted. The remaining expressions and locals describe distinct pinned-state inputs, mock outcomes,
  trust-filter comments, git unwind commands, and per-branch assertions. Further extraction would hide the state-machine
  scenario or replace meaningful local names with an opaque options mapping.
- Protected by: the 225 focused Package 5.4 tests.
- Reviewed: [x]

### Cohesive decomposition, question, and documenting test shapes

- File and symbols: `tests/test_workflow_decomposition_decomposing.py`, `tests/test_workflow_question.py`, and
  `tests/test_workflow_documenting.py`.
- Rule: `WPS201` and `WPS202`.
- Reason: Oversized test classes are split into behavior-focused classes while their stage constants and fixtures stay
  beside the scenarios that consume them. Splitting the three modules at the seven-member threshold would duplicate
  those fixtures and make related state transitions harder to follow; the question module's imports are the direct
  collaborators used by its handler and real-git regression scenarios.
- Protected by: the 143 focused tests in these three modules.
- Reviewed: [x]

### Unittest context-manager capture variables

- File and symbols: `assertLogs` and `patch` captures in `tests/test_scheduler.py`,
  `tests/test_workflow_scheduler_routing.py`, `tests/test_workflow_branch_publication.py`,
  `tests/test_workflow_decomposition_blocked.py`, `tests/test_workflow_decomposition_decomposing.py`,
  `tests/test_workflow_decomposition_umbrella.py`, `tests/test_workflow_question.py`,
  `tests/test_workflow_question_routing.py`, `tests/test_workflow_documenting_routing.py`,
  `tests/test_workflow_conflicts_authed_fetch.py`, `tests/test_workflow_conflicts_routing.py`,
  `tests/test_workflow_conflicts_target_fetch.py`, `tests/test_workflow_in_review_checks.py`,
  `tests/test_workflow_validating_review.py`, and `tests/test_workflow_validating_squash.py`.
- Rule: `WPS441`.
- Reason: `unittest` populates log captures on context exit, and patch mocks are asserted after restoration. Reading
  each capture afterward is the standard assertion boundary; wrapping it in a helper would hide the patched operation,
  log level, or non-call assertion under test.
- Protected by: scheduler failure/skip logging, unsafe-transport refusal, stage routing, retry-budget, and dependency
  visibility tests.
- Reviewed: [x]

### Implementing and fixing scenario density

- File and symbols: scenario tests across `tests/test_workflow_implementing_*.py`, `tests/test_workflow_fixing.py`,
  `tests/test_workflow_fixing_paused.py`, and `tests/test_workflow_fixing_routing.py`.
- Rule: `WPS204`, `WPS210`, and `WPS213`.
- Reason: Shared implementing/fixing stage runners, behavior fixtures, agent-call mocks, comment-injection recorders,
  and patch-stack contexts remove the duplicated setup. The remaining expressions and locals describe distinct pinned
  state, agent outcomes, PR feedback surfaces, ordered calls, or per-field assertions; further extraction would hide
  the state-machine scenario or replace meaningful values with an opaque options mapping.
- Protected by: the 234 focused Package 5.5 tests and their 38 subtests.
- Reviewed: [x]

### Cohesive implementing and fixing test shapes

- File and symbols: `tests/test_workflow_fixing.py`, `tests/test_workflow_fixing_routing.py`,
  `tests/test_workflow_implementing_full_spec.py`, `tests/test_workflow_implementing_pr_reuse.py`, and
  `tests/test_workflow_implementing_retry.py`.
- Rule: `WPS201`, `WPS202`, and `WPS235`.
- Reason: Oversized test classes are split into behavior-focused groups while stage constants and fixtures remain
  beside the scenarios that consume them. Splitting these cohesive modules at the seven-member threshold would
  duplicate the shared state builders, and the direct workflow-helper imports keep backend, label, and prompt
  contracts explicit at use sites.
- Protected by: the 189 focused tests in these five modules.
- Reviewed: [x]

### Validating, in-review, and conflict scenario density

- File and symbols: scenario tests across `tests/test_workflow_validating_*.py`,
  `tests/test_workflow_in_review_*.py`, and `tests/test_workflow_conflicts_*.py`.
- Rule: `WPS204`, `WPS210`, and `WPS213`.
- Reason: Shared stage runners, behavior fixtures, authenticated-git recorders, watermark seeds, and real-git setup
  remove the duplicated plumbing. The remaining expressions and locals describe distinct pinned-state inputs, agent
  outcomes, comment surfaces, git safety probes, ordered writes, or per-branch assertions; further extraction would
  hide the workflow scenario or replace meaningful local names with an opaque options mapping.
- Protected by: the 282 focused Package 5.6 tests and their 29 subtests.
- Reviewed: [x]

### Cohesive validating, in-review, and conflict test shapes

- File and symbols: `tests/test_workflow_in_review_checks.py`, `tests/test_workflow_in_review_routing.py`,
  `tests/test_workflow_validating_review.py`, `tests/test_workflow_validating_verify.py`, and
  `tests/test_workflow_validating_watermarks.py`.
- Rule: `WPS201`, `WPS202`, and `WPS235`.
- Reason: Oversized test classes are split into behavior-focused groups while their stage constants, fixture mixins,
  and direct collaborators stay beside the scenarios that consume them. Splitting these cohesive modules at the
  import/member thresholds would duplicate shared state builders or obscure the workflow and security boundaries.
- Protected by: the 282 focused Package 5.6 tests and their 29 subtests.
- Reviewed: [x]

### Shared workflow test fakes and patch harness

- File and symbols: `tests/fakes.py`: `make_issue` and `FakeGitHubClient`; `tests/workflow_helpers.py`:
  `_agent`, `_PatchedWorkflowMixin`, and `_ResolvingConflictMixin`.
- Rule: `WPS201`, `WPS202`, `WPS210`, `WPS211`, `WPS214`, `WPS230`, and `WPS602`.
- Reason: The fake client deliberately mirrors the production GitHub client, including its static helper call shape,
  while exposing public histories that scenarios assert. The workflow mixins own one cohesive patch boundary and
  explicit stage controls. Splitting either surface or replacing its arguments with opaque option dictionaries would
  scatter the contract and make tests harder to inspect.
- Protected by: the 287 focused Package 5.7 tests and their 414 subtests.
- Reviewed: [x]

### Cohesive shared and cross-cutting test shapes

- File and symbols: `tests/test_line_length.py`, `tests/test_run_sh.py`, `tests/test_skill_catalog.py`,
  `tests/test_state_machine.py`, `tests/test_workflow_agent_analytics.py`, `tests/test_workflow_drain_terminals.py`,
  `tests/test_workflow_drift.py`, `tests/test_workflow_event_emission.py`,
  `tests/test_workflow_pr_lifecycle.py`, `tests/test_workflow_tick_parallel.py`,
  `tests/test_workflow_tracked_repos_prompts.py`, and `tests/test_workflow_usage_accumulator.py`.
- Rule: `WPS202` and `WPS235`.
- Reason: Oversized classes were split by behavior and repeated setup moved into named builders, recorders, and shared
  helpers. The remaining module members describe distinct parsers, concurrency probes, or workflow fixtures. Splitting
  at the seven-member threshold would duplicate their context, while the direct helper imports keep state, event, and
  backend contracts visible at each use site.
- Protected by: the 287 focused Package 5.7 tests and their 414 subtests.
- Reviewed: [x]

### Shared and cross-cutting workflow scenario density

- File and symbols: scenario tests in `tests/test_github_pinned_state.py`, `tests/test_skill_catalog.py`,
  `tests/test_state_machine.py`, `tests/test_workflow_agent_analytics.py`,
  `tests/test_workflow_community_contribution.py`, `tests/test_workflow_drain_terminals.py`,
  `tests/test_workflow_drift.py`, `tests/test_workflow_event_emission.py`,
  `tests/test_workflow_finalize_pr_merged.py`, `tests/test_workflow_list_pollable.py`,
  `tests/test_workflow_paused_agent_guard.py`, `tests/test_workflow_pr_lifecycle.py`,
  `tests/test_workflow_stage_analytics.py`, `tests/test_workflow_tick_parallel.py`, and
  `tests/test_workflow_tracked_repos_prompts.py`.
- Rule: `WPS204`, `WPS210`, and `WPS213`.
- Reason: Shared state builders, analytics readers, callable probes, patch runners, and event selectors remove the
  duplicated plumbing. The remaining expressions and locals describe distinct pinned-state inputs, analytics fields,
  terminal outcomes, concurrency gates, or per-field assertions. Further extraction would hide scenario order or
  replace meaningful values with a generic mapping.
- Protected by: the 287 focused Package 5.7 tests and their 414 subtests.
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
| 2026-07-16 | 3.6/fixing | Complete | Target WPS; 90 focused; full gate | Not committed | `validating.py` |
| 2026-07-16 | 3.6/documenting | Complete | Target WPS; 62 focused; full gate | Not committed | `validating.py` |
| 2026-07-16 | 3.6/validating | Complete | Target WPS; 129 focused; full gate | Not committed | `in_review.py` |
| 2026-07-16 | 3.6/in_review | Complete | Target WPS; 68 focused; full gate | Not committed | `conflicts.py` |
| 2026-07-16 | 3.6/conflicts | Complete | Target WPS; 70 focused; full gate | Not committed | Start Package 4.1 |
| 2026-07-16 | 4.1 | Complete | Target WPS; 24 remainders; full gate 2118 passed | Not committed | Start Package 4.2 |
| 2026-07-17 | 4.2 | Complete | Target WPS; 78 remainders; full gate 2120 passed | Not committed | Start Package 4.3 |
| 2026-07-17 | 4.3 | Complete | Target WPS; 6 remainders; full gate 2121 passed | Not committed | Start Package 4.4 |
| 2026-07-17 | 4.4 | Complete | Target WPS; 5 remainders; full gate 2124 passed | Not committed | Start Package 4.5 |
| 2026-07-17 | 4.5/usage-metrics | Complete | Target WPS; 21 fixed, 2 deferred; full gate | Not committed | 4.5 rest |
| 2026-07-17 | 4.5/usage-skills | Complete | Target WPS; WPS202 36->25; full gate | Not committed | 4.5 rest |
| 2026-07-17 | 4.5/usage-trajectory | Complete | Target WPS; 2 deferred fixed; full gate | Not committed | 4.5 rest |
| 2026-07-18 | 4.5/config-diagnostics | Complete | WPS363 17->0, WPS421 3->0; full gate | Not committed | 4.5 rest |
| 2026-07-18 | 4.5/repository-config | Complete | WPS202 27->18; new leaf 11; full gate | Not committed | 4.5 rest |
| 2026-07-18 | 4.5/analytics-recording | Complete | WPS202 55->0; leaf 56; full gate | Not committed | 4.5 rest |
| 2026-07-18 | 4.5/analytics-trajectories | Complete | WPS202 56->39; leaf 17; WPS342 fixed | Not committed | 4.5 rest |
| 2026-07-18 | 4.5/analytics-retention | Complete | WPS202 39->27; leaf 13; WPS420 1->0 | Not committed | 4.5 rest |
| 2026-07-18 | 4.5/dashboard-reads | Complete | WPS202 93->63; leaf 31; WPS234 5->1 | Not committed | 4.5 rest |
| 2026-07-18 | 4.5/dashboard-widgets | Complete | WPS202 63->21; leaf 42; WPS234 1->0 | Not committed | 4.5 rest |
| 2026-07-18 | 4.5/dashboard-skill-matrix | Complete | WPS202 58->44; leaf 14; full gate | Not committed | 4.5 rest |
| 2026-07-18 | 4.5/trajectory-reader | Complete | WPS202 36->19; leaf 17; full gate | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/analytics-sync | Complete | WPS202 40->27; leaf 13; WPS201 15->14 | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/dashboard-charts-cost | Complete | WPS202 59->35; leaf 24; full gate | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/dashboard-cards | Complete | WPS202 44->31; leaf 13; full gate | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/dashboard-kpi-strip | Complete | WPS202 42->30; leaf 12; full gate | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/trajectory-dashboard | Complete | WPS202 45->26; leaf 19; full gate | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/import-regroups | Complete | WPS235 read.py+dashboard.py ->0 | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/reader-reload | Complete | Reload A/B test; gate 2143 passed | Not committed | charts usage split |
| 2026-07-19 | 4.5/review-r1 | Complete | Chart cycle/reload/logger fixes; gate 2152 | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/review-r2 | Complete | charts->hub; register+inventory; gate 2151 | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/review-r3 | Complete | inventory: import/metadata/collision families | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/review-r4 | Complete | traj-dashboard WPS201 12; sync test-alias cleanup | Not committed | 4.5 rest |
| 2026-07-19 | 4.5/core-structure | Complete | WPS458/300/301 findings ->0; full gate 2151 | Not committed | 4.5 close |
| 2026-07-19 | 4.5 | Complete | Import-structure fixed; remainders documented | Not committed | Stage 4 [x] |
| 2026-07-19 | 5.1+6.1 | Partial | WPS 1464->786; read/traj/theme ->0; gate 2150p/3s | Not committed | 4 heavy modules |
| 2026-07-19 | 5.1+6.1 | Partial | 4 modules 786->374; gate 2150p/3s | Not committed | Resolve residual; boxes open |
| 2026-07-20 | 5.1+6.1 r2 | Partial | Restored read asserts; gate 2150p/3s | Not committed | Heavy-module residual |
| 2026-07-20 | 5.1+6.1 | Complete | WPS 470->446; WPS118->0; gate 2150p/3s | Not committed | Stage 5/6 1/7 |
| 2026-07-20 | 5.2 | Complete | WPS430 55->19, WPS338 16->1; gate 2116p/36s | Not committed | Start Package 5.3 |
| 2026-07-20 | 5.3 | Complete | WPS 681->363; 167 focused; gate 2150p/3s | Not committed | Start Package 5.4 |
| 2026-07-20 | 5.4 | Complete | WPS 748->697; 225 focused; gate 2150p/3s | Not committed | Start Package 5.5 |
| 2026-07-20 | 5.5 | Complete | WPS 680->560; 234 focused; gate 2149p/3s | Not committed | Start Package 5.6 |
| 2026-07-20 | 5.6 | Complete | WPS 1272->1161; 282 focused; gate 2149p/3s | Not committed | Start Package 5.7 |
| 2026-07-21 | 5.7 | Complete | WPS 851->650; 287 focused; gate 2161p/3s | Not committed | Start Package 6.2 |
| 2026-07-21 | 6.2 | Complete | WPS 566->288; target 374->130; 275 focused; 2173p/3s | None | Start 6.3 |
| 2026-07-21 | 6.3 | Complete | WPS 363->117; target 279->33; 167f; 2204p/3s | None | Start 6.4 |
| 2026-07-21 | 6.4 | Complete | WPS 697->141; target 555->0; 225f; 2204p/3s | None | Start 6.5 |

Package 6.4 is **complete**. The pass covered the decomposition handler families, question handling and routing,
documenting handling, paused/trust filtering, and documenting routing. Issue, PR, comment, watermark, usage, git,
branch, session, label, and pinned-state values now use names tied to their test domain. Ambiguous comprehension
variables were renamed, and true fixture constants moved to module scope while fixture-specific class attributes use
descriptive lowercase names. The collected set of 225 test methods is unchanged, and every scenario retains its
original assertions.

The scoped `--select=WPS` count over the fourteen modules fell from 697 to 141. The Package 6.4 rule set fell from
555 to 0: `WPS110` (4), `WPS111` (22), `WPS115` (14), `WPS226` (48), and `WPS432` (467) were cleared. One
`WPS237` long-tail finding also disappeared as a numeric interpolation became a named value. The 137 reviewed
structural findings assigned to Stage 5 remain unchanged: `WPS201` (1), `WPS202` (3), `WPS204` (43), `WPS210`
(55), `WPS213` (19), and `WPS441` (16). The four remaining Stage 7 findings are `WPS237` (1) and `WPS336` (3);
no new rule family was introduced.

All 225 focused tests and 4 focused subtests pass, as does repository-wide Ruff. The complete tracked suite passes
with 2,204 tests and 3 live-Postgres skips; both committed-range and working-tree diff checks are clean. A bare
repository-root collection also sees the ignored, externally owned `analytics-db/data` volume and receives
`PermissionError`; the complete tracked `tests/` tree is the recorded full gate for this session. The redundancy
audit confirms that the same 225 named behaviors remain collected and no new test abstraction was introduced.

Package 6.3 is **complete**. The pass covered scheduler execution and routing, base-sync unit and real-git scenarios,
branch publication, cleanup, worktree path resolution, and worktree serialization. Repository slugs, git commands and
outputs, branch and event fields, wait budgets, and issue / PR / comment identities now use names tied to their test
domain. Ambiguous call, event, subprocess-result, and outcome variables were renamed; the decompose issue constant
moved to module scope; and numeric crash-recovery / HTTP-status test names now describe their behavior.

The scoped `--select=WPS` count fell from 363 to 117. The Package 6.3 rule set fell from 279 to 33: `WPS110` (8),
`WPS111` (33), `WPS114` (3), `WPS115` (1), `WPS117` (4), `WPS226` (43), `WPS358` (1), and `WPS432` (153) were
cleared. The 33 retained `WPS204` findings are the reviewed direct scheduler and base-sync scenario expressions in the
accepted-remainder register. The 116 structural findings assigned to Stage 5 remain unchanged, as does the single
`WPS237` long-tail finding assigned to Stage 7; no new rule family was introduced.

All 167 focused tests and repository-wide Ruff pass. The complete tracked suite passes with 2,204 tests and 3
live-Postgres skips; both committed-range and working-tree diff checks are clean.

Package 6.2 is **complete**. The pass covered `test_agents.py`, `test_usage.py`, `test_main.py`, `test_config.py`,
and `main_helpers.py`. Backend / CLI / environment tokens, repository fixtures, timing budgets, signal exit codes,
provider statuses, cost sources, and cumulative usage records now use names tied to their test-domain meaning.
Ambiguous locals, numbered identifiers, upper-case class attributes, and overlong test names were cleared. Repeated
configuration failures share one error-message fixture, and the entry-point constants remain in the dedicated helper
module without adding another large import surface to `test_main.py`.

The scoped `--select=WPS` count fell from 566 to 288. The Package 6.2 rule set fell from 374 to 130: `WPS110` (20),
`WPS111` (12), `WPS114` (20), `WPS115` (3), `WPS118` (14), and `WPS358` (1) were cleared; `WPS204` fell from 23 to
15, `WPS226` from 112 to 29, and `WPS432` from 169 to 86. No new rule family was introduced. The retained findings
are the reviewed direct entry-point/parser call shapes, literal provider wire envelopes and correlation values, and
per-scenario pricing inputs / rate multipliers documented above.

All 275 focused tests and 55 focused subtests pass, as does repository-wide Ruff. The complete tracked suite passes
with 2,173 tests and 3 live-Postgres skips; both committed-range and working-tree diff checks are clean.

Package 5.7 is **complete**. The pass covered the shared GitHub fake and workflow patch harness plus the remaining
state-machine, metadata, routing, analytics, PR-lifecycle, prompt, and parallel-tick tests. Closed-sweep and review
filtering moved into named fake helpers; duplicate pinned-state, worktree, and analytics-record helpers were
consolidated in `workflow_helpers.py`; oversized classes were split by behavior; and nested callbacks became named
callable probes or inspectable mocks. The collected set of 287 focused tests and 414 subtests is unchanged, and every
scenario retains its original assertions.

The scoped `--select=WPS` count over the 32 Package 5.7 modules fell from 851 to 650. The structural subset fell from
273 to 113: `WPS220` (8), `WPS221` (6), `WPS229` (1), `WPS231` (9), `WPS232` (1), `WPS234` (1), `WPS338` (32),
`WPS420` (7), `WPS426` (1), `WPS430` (23), `WPS431` (1), `WPS441` (26), `WPS458` (1), and `WPS501` (4) were
cleared. `WPS214` fell from 12 to 1, `WPS602` from 17 to 7, `WPS204` from 31 to 27, `WPS210` from 47 to 37,
`WPS211` from 8 to 5, and `WPS213` from 21 to 16. The 113 retained structural findings are `WPS201` (1), `WPS202`
(12), `WPS204` (27), `WPS210` (37), `WPS211` (5), `WPS213` (16), `WPS214` (1), `WPS230` (1), `WPS235` (6),
and `WPS602` (7), covered by the three reviewed remainder entries above.

The other 537 findings are naming, repeated-string, numeric-literal, and long-tail format findings assigned to
Package 6.7 or Stage 7: `WPS110` (38), `WPS111` (15), `WPS115` (6), `WPS118` (1), `WPS226` (103), `WPS237` (1),
`WPS335` (2), `WPS336` (7), `WPS342` (1), `WPS407` (4), `WPS432` (345), `WPS435` (1), `WPS504` (8), `WPS509`
(1), `WPS527` (3), and `WPS615` (1).

All 287 focused tests, 414 focused subtests, and Ruff pass. The complete tracked suite passes with 2,161 tests and 3
live-Postgres skips; both clean `HEAD` and the modified tree collect exactly 2,164 tests. Both committed-range and
working-tree diff checks are clean. A bare repository-root run also sees the ignored, externally owned
`analytics-db/data` volume and receives `PermissionError`; the complete tracked `tests/` tree is the recorded full gate
for this session.

Package 5.6 is **complete**. The pass covered the validating review, verify, squash, drift, handoff, watermark,
paused, and terminal suites; in-review routing, feedback filtering, migration, drift, parks, checks, and watermarks;
conflict execution, recovery, resume, publication, authenticated fetch, and worktree restoration; plus verdict
parsing. Repeated stage lambdas moved into the shared `_run_validating`, `_run_in_review`, and
`_run_resolving_conflict` helpers; oversized classes were split by behavior; and nested git/token/process callbacks
became reusable callable recorders or inspectable mocks. The collected set of 282 focused tests and 29 subtests is
unchanged, and every scenario retains its original assertions.

The scoped `--select=WPS` count over the 35 Package 5.6 modules fell from 1,272 to 1,161. The structural subset fell
from 314 to 220: `WPS211` (1), `WPS214` (13), `WPS221` (2), `WPS338` (32), `WPS426` (2), `WPS430` (19), `WPS458`
(3), and `WPS602` (1) were cleared; `WPS204` fell from 84 to 67, `WPS210` from 101 to 96, and `WPS213` from 34 to
33. The 220 retained structural findings are `WPS201` (2), `WPS202` (4), `WPS204` (67), `WPS210` (96), `WPS213`
(33), `WPS235` (2), and `WPS441` (16), covered by the reviewed scenario-density, cohesive-module, and unittest
capture entries above. The other 941 findings are naming, repeated-string, numeric-literal, and long-tail format
findings assigned to Package 6.6 or Stage 7: `WPS110` (15), `WPS111` (30), `WPS114` (4), `WPS115` (66), `WPS226`
(184), `WPS237` (2), `WPS336` (2), `WPS342` (4), and `WPS432` (634).

All 282 focused tests, 29 focused subtests, and Ruff pass. The complete tracked suite passes with 2,149 tests, 3
live-Postgres skips, and 974 subtests; the modified tree still collects exactly 2,152 tests, matching clean `HEAD`'s
recorded count. Both committed-range and working-tree diff checks are clean. A bare repository-root collection also
sees the ignored, externally owned `analytics-db/data` volume and receives `PermissionError`; the complete tracked
`tests/` tree is the recorded full gate for this session.

Package 5.5 is **complete**. The pass covered the implementing timeout, retry, PR-reuse, drift, fresh-run, paused,
full-spec, and terminal suites plus fixing handling, paused behavior, and routing. Repeated stage lambdas moved into
the shared `_run_implementing` / `_run_fixing` helpers; oversized classes were split by behavior; nested agent/git
callbacks became inspectable mocks or callable recorders; and the six-patch drift tuple became an `ExitStack`-backed
context. The collected set of 234 focused tests and 38 subtests is unchanged, and every scenario retains its original
assertions.

The scoped `--select=WPS` count over the eleven Package 5.5 modules fell from 680 to 560. The structural subset fell
from 244 to 127: `WPS214` (11), `WPS338` (21), `WPS430` (30), `WPS236` (7), `WPS426` (6), and `WPS441` (8) were
cleared, as were the smaller `WPS211`, `WPS219`, `WPS221`, `WPS227`, `WPS234`, `WPS407`, `WPS458`, `WPS501`, and
`WPS602` families; `WPS204` fell from 54 to 44 and `WPS210` from 68 to 61. The 127 retained structural findings are
`WPS201` (2), `WPS202` (3), `WPS204` (44), `WPS210` (61), `WPS213` (15), and `WPS235` (2), covered by the two
reviewed remainder entries above. The other 433 findings are naming, repeated-string, numeric-literal, and long-tail
format findings assigned to Package 6.5 or Stage 7: `WPS110` (20), `WPS111` (9), `WPS114` (1), `WPS115` (16),
`WPS226` (94), `WPS336` (3), and `WPS432` (290).

All 234 focused tests, 38 focused subtests, and Ruff pass. The complete tracked suite passes with 2,149 tests and 3
live-Postgres skips; clean `HEAD` and the modified tree both collect exactly 2,152 tests. Both committed-range and
working-tree diff checks are clean. A bare repository-root collection also sees the ignored, externally owned
`analytics-db/data` volume and receives `PermissionError`; the complete tracked `tests/` tree is the recorded full
gate for this session.

Package 5.4 is **complete**. The pass covered the decomposition handler families, question handling and routing,
documenting handling, paused/trust filtering, and documenting routing. Oversized stage-test classes were split into
behavior-focused groups; repeated stage invocation and fixture setup moved into small mixins and module helpers; and
the three nested child-write observers plus the question relabel callback family moved into narrowly scoped callable
recorders. The collected set of 225 test methods is unchanged, and every scenario retains its original assertions.

The scoped `--select=WPS` count over the fourteen modules fell from 748 to 697, removing 51 findings. `WPS214` fell
from 7 to 0, `WPS338` from 17 to 0, `WPS430` from 8 to 0, `WPS235` from 2 to 0, and `WPS221` from 1 to 0; `WPS204`
fell from 51 to 43. The 137 retained structural findings are `WPS201` (1), `WPS202` (3), `WPS204` (43), `WPS210`
(55), `WPS213` (19), and `WPS441` (16), covered by the reviewed scenario-density, cohesive-module, and
context-capture entries above. The other 560 findings are naming, repeated-string, numeric-literal, and long-tail
format findings assigned to Package 6.4 or Stage 7: `WPS110` (4), `WPS111` (22), `WPS115` (14), `WPS226` (48),
`WPS237` (2), `WPS336` (3), and `WPS432` (467).

All 225 focused tests and Ruff pass. The complete tracked suite passes with 2,150 tests and 3 live-Postgres skips,
and both committed-range and working-tree diff checks are clean. The test-method redundancy audit confirms that the
same 225 named behaviors remain collected after the class splits. A bare repository-root collection also sees the
ignored, externally owned `analytics-db/data` volume and receives `PermissionError`; the tracked `tests/` tree is the
recorded full gate for this session.

Package 5.3 is **complete**. The pass covered scheduler execution and routing, base-sync unit and real-git
scenarios, branch publication, cleanup, worktree path resolution, and worktree serialization. Repeated event-gate
cleanup moved into `_release_on_exit`; shared issue processors and concurrency probes replaced one-test nested
callbacks; scheduler-routing setup moved into a common fixture; base-sync scenarios were divided into behavior-focused
classes; and git results, token resolution, local fetches, cleanup clients, path/state builders, and branch push
recorders moved into small reusable helpers. Every test scenario and its distinct observable assertions remain in
place.

The scoped `--select=WPS` count over the eight modules fell from 681 to 363, removing 318 findings. In particular,
`WPS430` fell from 60 to 1, `WPS501` from 51 to 2, and `WPS229` from 27 to 0; the method-order, local-import,
mutable-constant, and nested-callback families targeted by the package were cleared or reduced to the reviewed
remainders above. The 116 retained structural findings are `WPS202` (6), `WPS204` (33), `WPS210` (24), `WPS213`
(31), `WPS230` (1), `WPS235` (1), `WPS430` (1), `WPS441` (17), and `WPS501` (2). The other 247 findings are naming,
repeated-string, numeric-literal, and long-tail format findings assigned to Package 6.3 or Stage 7: `WPS110` (8),
`WPS111` (33), `WPS114` (3), `WPS115` (1), `WPS117` (4), `WPS226` (43), `WPS237` (1), `WPS358` (1), and
`WPS432` (153).

All 167 focused tests and Ruff pass. The complete tracked suite passes with 2,150 tests and 3 live-Postgres skips,
and both committed-range and working-tree diff checks are clean. A bare repository-root pytest collection also sees
the ignored, externally owned `analytics-db/data` volume and receives `PermissionError`; running pytest against the
tracked `tests/` tree exercises every tracked test file and is the recorded full gate for this session.

Package 3.1 retained 18 reviewed API findings and passed 2,099 tests, 3 skips, and 627 subtests.

Package 5.2 refactored `test_agents.py`, `test_usage.py`, `test_main.py`, and `test_config.py` for
structure and complexity. `test_main.py`'s `fake_client` / `fake_tick` closures were replaced by the
`_ClientFactory` / `_TickRecorder` recorders (plus the `_raise_on_slug` tick hook and the `_build_clients`
dispatch fixture) that collapse the 19 byte-identical `fake_client` closures and the pure-recording
`fake_tick` closures into reusable callables exposing `.by_slug` / `.calls` / `.slugs` / `.schedulers` /
`.threads`; the no-op `fake_tick` bodies became bare `patch.object(..., "tick")` mocks, dropping WPS430
there from 49 to 17. Those four fixtures live in a new `tests/main_helpers.py` (the per-subsystem helper
pattern already used by `tests/analytics_read_helpers.py`) rather than at `test_main.py` module scope:
keeping them in-module would have raised `test_main.py` from 7 members to 10 and tripped a fresh WPS202,
and hosting `_build_clients` there also clears the WPS338 it caused as a protected method above the
`AsyncPollingDispatchTest` tests. The 17 retained WPS430 are the barrier, worker-submission, and
signal-coordination ticks whose bodies are materially distinct (recording order across the barrier is
itself the regression signal, so those must not route through the recorder), plus the two `tracking_init`
closures that wrap `IssueScheduler.__init__` -- a callable class cannot receive the constructed instance
because Python only prepends `self` when `__init__` is a plain function.
`test_agents.py` lifted the four duplicated `killpg` side effects into module-level `_killpg_group_empty`
/ `_killpg_group_alive`, merged the SIGTERM/SIGKILL interruption tests into one `subTest` loop, unpacked
only the asserted tail of the 5-tuple returns with `*_` to clear WPS210, and moved the two protected
helpers below the public tests to clear WPS338. `test_config.py` extracted the 11 byte-identical
`_load_config` reload methods (and the Hitl variant) into a single module-level `_load_config(env=None)`,
which cleared WPS338 for those 11 classes and removed the per-class duplication; the dotted
`import orchestrator.config as config` after the `sys.modules.pop` is preserved so the reload is not
skipped.

`test_usage.py` was left structurally unchanged: it already builds records through module-level fixture
builders (`_jsonl`, `_assistant`, `_claude_usage`, ...), has no nested callbacks or method-order
findings, and its residual WPS214 / WPS221 / WPS213 / WPS210 are inherent -- the large parser classes
each verify a materially different per-SKU pricing contract (tiered vs flat, pro vs base, cached-blocked)
and the expression-heavy tests are single round-trips whose per-field assertions are their distinct
coverage; collapsing either would merge different scenarios or hide assertions.

The remaining structure findings are documented rather than forced: `test_main.py`'s residual WPS210 (12)
and WPS213 (4) sit on the concurrency tests whose lock / event / barrier / recorder locals are the
fixture's meaning; `test_config.py` keeps one WPS338 (`DotenvQuoteStrippingTest._reload_with_dotenv`, a
single-class hermetic-reload helper that reads best next to the class it serves) and one WPS430 (a
single-use raising `config_error` callback); and the WPS214 too-many-methods counts on the large cohesive
parser / config classes stay, since splitting a class to satisfy a count harms organization. The
WPS458 import-collision counts (the dual `from orchestrator import agents` + `from orchestrator.agents
import ...` pattern) are the standard test module-plus-symbols import idiom and are left in place.

Packages 5.1 and 6.1 were run as one subsystem pass and are now **complete**; the 5.1 and 6.1 checkboxes
are checked. The pass covered `test_analytics*.py`, `test_dashboard*.py`, `test_trajectory*.py`, and the
shared `tests/analytics_read_helpers.py`. The helper gained the `_reload_read` shim plus the
`_FakeConnection.as_connect` / `.first_query` accessors that collapse the read suite's repeated reload /
connector / `executed[0]` expressions, and `test_analytics.py` gained the `_analytics_sink` /
`_trajectory_sink` context managers that fold the recurring `TemporaryDirectory` + `_reload` sink
boilerplate behind a `(path, analytics)` yield; the dashboard and trajectory script-launch tests moved
their `sys.path` / `sys.modules` snapshot-and-restore into module helpers (`_script_launch_sandbox`,
`_arm_launch_cleanup`). `_YEAR = 2026` names the recurring fixture year. The read helpers reload with
`importlib.import_module` after `sys.modules.pop`, and `test_analytics.py` uses the dotted
`import orchestrator.X as X` after the same pop -- never `from orchestrator import ...`, because that
rebinds the stale package attribute and skips the reload.

The scoped `--select=WPS` count over those modules fell from 470 to 446. All `WPS118` long test names were
renamed to <=45 chars (6 -> 0), and `WPS243` (too-long `finally`) was cleared. `WPS210` fell from 42 to 32
and `WPS213` from 14 to 12 via the extracted sink / launch helpers and by inlining single-use
intermediates; the `WPS362` / `WPS478` / `WPS501` instances that survive are registered below rather than
claimed cleared. Every distinct per-field assertion was preserved: the earlier over-collapse into
whole-object and list-of-model equalities was reverted, and the review-round breakdown test now checks each
column independently (bucket, run / fail counts, total + per-role costs, and the per-role cache vs no-cache
split) through a `getattr` loop rather than one positional dataclass equality.

The 446 retained findings are documented by rule family below (the counts sum to 446); each is a single-use
fixture value, a wire-format contract, a required reload / cleanup idiom, or a structure count whose "fix"
would collapse a distinct assertion or split a cohesive module:

- `WPS432` (214) and `WPS358` (43): single-use fixture-payload magic numbers and float-zeros -- counts,
  costs, token totals, and timestamps that appear once inside a fixture tuple or record. Naming each would
  spawn the one-use constants the plan explicitly rejects and would not help a reader who checks the value
  against its assertion in place.
- `WPS226` (49): the agent-wire-format JSON keys (`ts`, `event`, `input`, `output`, `type`, `turn`,
  `tools`, `content`, `usage`, `session_id`, ...) the fixtures build records from; constant-izing a wire
  key reads worse than the literal it names, and the genuinely-semantic repeated strings already carry
  module constants.
- `WPS210` (32) and `WPS213` (12): the residual sits on tests whose local / expression count *is* their
  distinct-assertion density -- e.g. `test_codex_trajectory_record` (15 field checks over one emitted
  record), the read-model round-trips that unpack a DB column tuple then assert each field, and the
  prune / regression tests whose `now` / `old_ts` / `new_ts` locals are the fixture's meaning. Dropping
  under the limit there requires either collapsing a distinct assertion (forbidden by the issue) or
  inlining the timestamps into repeated `_ts_days_ago(..., now=PRUNE_NOW)` calls that trip `WPS204` and
  read worse.
- `WPS204` (35): module-wide overuse of the core test idioms (the `(_, analytics)` reload unpack,
  `rows[0]`, `len(records)`, `_run_sync(...)`); naming them across ~8 functions adds locals that re-trip
  `WPS210` for marginal clarity.
- `WPS214` (15 too-many-methods), `WPS202` (6 too-many-module-members, 29-74 per module), `WPS201`
  (3 too-many-imports, 17-57), and `WPS203` (1 too-many-imported-names, 62): whole-module counts inherent
  to large cohesive test modules; splitting a module or a test class to satisfy a count harms organization.
- `WPS211` (6 too-many-arguments): fixture builders such as `_claude_stdout_with_skills` whose keyword
  parameters are the fixture's knobs; reshaping the signature harms the callers.
- `WPS221` (8 high-Jones-complexity lines, 15-16 > 14): dense single lines that are fixture tuples or a
  `patch.dict(os.environ, {...})` env setup; splitting them adds a local that re-trips `WPS210`.
- `WPS441` (6 control-variable-after-block): the standard `with self.assertLogs(...) as cm:` /
  `assertRaises(...) as cm:` / capture-list idiom that must read `cm.output` / `cm.exception` / the
  captured list *after* the block, because that is when unittest populates them.
- `WPS430` (3): `gated_replace` / `appender` are the parallel-append race harness's callbacks -- they
  close over three `threading.Event`s plus the reloaded module and interleave with the test body's timing,
  so hoisting them forces `functools.partial` wrapping that obscures the coupling and risks the
  concurrency regression; `fake_sync` was hoisted experimentally and reverted because the mock handle
  pushed its test to 7 locals and a control-variable-after-block, trading one finding for two.
- `WPS362` (2) and `WPS478` (2): the `sys.path[:] = saved` in-place restore in the script-launch helpers.
  The full-slice assignment is required -- rebinding `sys.path = saved` would leave other holders on the
  mutated list, so the snapshot must restore the interpreter's own list object.
- `WPS301` (2): the dotted `import orchestrator.config` / `import orchestrator.analytics` in
  `test_analytics.py`'s `_reload`; a `from orchestrator import ...` would rebind the stale package
  attribute and skip the reload, so the dotted form is load-bearing.
- `WPS236` (2 too-many-unpack-targets, 7 and 11): the DB-column tuple unpacks in the analytics-sync
  round-trip tests, where each target *is* a named column of the asserted row.
- `WPS501` (1): the race harness's `try` / `finally` without `except` that unblocks and joins the appender
  thread even if the prune raises -- a cleanup barrier, not a swallowed error.
- `WPS230` (1 too-many-public-attributes): a fake connection whose attributes are the seams the tests read;
  underscoring them would change the fake's asserted surface.
- `WPS229` (1 long-try-body): the optional-dependency `try: import plotly ...` guard in
  `test_dashboard_charts.py`, kept whole so the skip boundary reads in one place.
- `WPS407` (1 mutable-module-constant): the shared `CONFIGURED_DB_ENV` reload-env dict the
  source-inspection tests pass to `_reload`; it is read-only in practice and a dict is what `_reload` takes.
- `WPS447` (1 alphabet-as-string): the `"0123456789"` truncation-fixture payload in `test_analytics.py`.

Optional-dependency boundaries were preserved exactly: the live-Postgres tests still skip when
`ANALYTICS_TEST_DB_URL` is unset, and the streamlit / plotly dashboard tests still skip cleanly when the
optional `dashboard` group is absent. Focused analytics / dashboard / trajectory suites, `ruff check`, and
the full `uv run pytest` gate pass at 2,150 passed and 3 skipped (the 3 skips are the live-Postgres tests).
The repo-wide finding-count table stays gated on a full-tree rescan (Package 7.1); this entry records only
the scoped 5.1 / 6.1 delta.

Package 3.2 retained two reviewed `WPS211` compatibility findings. All 246 focused tests and 2,100 full tests passed;
3 live-Postgres tests were skipped because `ANALYTICS_TEST_DB_URL` was unset.

Package 3.4 retained three reviewed `WPS211` compatibility findings. All 221 focused tests and 2,100 full tests
passed; 3 live-Postgres tests were skipped because `ANALYTICS_TEST_DB_URL` was unset.

Package 3.5 retained one reviewed `WPS211` compatibility finding. All 217 focused tests and 2,100 full tests passed;
3 live-Postgres tests were skipped because `ANALYTICS_TEST_DB_URL` was unset.

Packages 2.8, 3.2, 3.3, 3.4, and 3.5 ran `tests/` because root collection was blocked by the unreadable ignored
database volume.

Package 3.6 handler progress: `decomposition.py`, `implementing.py`, `fixing.py`, `documenting.py`,
`validating.py`, `in_review.py`, and `conflicts.py` are clear of the Stage 3 complexity rules; Package 3.6 is
complete. The
`implementing.py`
pass cleared its two remaining `WPS221` findings — the shared `silent_park_count` increment in `_park_session_limit`
and `_park_silent_failure` — by routing both through the new `_mark_agent_silent_park` persistence helper; no Stage 3
finding was retained. The `fixing.py` pass cleared all six over-limit functions (the `WPS210` local-variable, `WPS211`
argument, `WPS213` expression, `WPS231` cognitive-complexity findings, and the `WPS232` module-average) by threading
the per-tick handles through a new `_FixingContext` dataclass and extracting decision-free feedback-rescan, resume,
ACK, drift-classification, and batch-reconstruction helpers; the public `_handle_fixing`,
`_reconstruct_pending_fix_batch`, `_pending_fix_id_set`, and `_clear_pending_fix_bookmarks` signatures plus all
pinned-state, label, watermark, and event behavior were preserved, so the 90 focused fixing tests passed unchanged and
no Stage 3 finding was retained. All 90 focused fixing tests and 2,082 full tests passed (32 skipped for the optional
dashboard and live-Postgres dependencies).

The `documenting.py` pass cleared every over-limit function (the `WPS210` local-variable, `WPS211` argument, `WPS213`
expression, and `WPS231` cognitive-complexity findings across the drift-unwind, worktree-prep, docs-run, and
disposition helpers) by threading the per-tick handles plus the resolved branch and pinned `pr_number` through a new
`_DocumentingContext` dataclass, bundling the docs-run outcome into a new `_DocumentingRun` dataclass (so the router no
longer unpacks a five-value tuple), and extracting decision-free drift fetch/probe/reset, drift-announce/unwind-seed,
per-shape run (`_resume_documenting_dev`, `_recovered_documenting_run`, `_fresh_documenting_run`),
park/watermark/notice, and disposition helpers. The public `_handle_documenting` signature plus all pinned-state,
label, watermark, comment, and event behavior were preserved, so the 62 focused documenting tests passed unchanged and
no Stage 3 finding was retained. All 62 focused documenting tests and 2,082 full tests passed (32 skipped for the
optional dashboard and live-Postgres dependencies).

The `validating.py` pass cleared its sole remaining `WPS221` finding — the `review_round` increment in
`_bump_review_round`, whose single-line `state.set("review_round", int(state.get("review_round") or 0) + 1)`
read-modify-write tripped the Jones-complexity limit — by binding the read to a `current_round` local before the
`state.set`, mirroring `implementing.py`'s `_mark_agent_silent_park`. The stage-private decomposition that already sits
under the Stage 3 limits (reviewer freshness, verify/squash ordering, verdict routing, transient-park recovery,
in_review handoff watermarks, and the fixing/documenting handoffs) was untouched, so no helper moved and no
`workflow.py` alias or late-bound `_wf` call changed; the 129 focused validating tests passed unchanged and no Stage 3
finding was retained. All 129 focused validating tests and 2,082 full tests passed (32 skipped for the optional
dashboard and live-Postgres dependencies).

The `in_review.py` pass cleared all nine over-limit findings (the `WPS210` local-variable, `WPS211` argument, and
`WPS213` expression findings across `_bump_in_review_watermarks`, `_scan_fresh_pr_feedback`,
`_route_feedback_to_fixing`, `_handle_user_content_drift`, and `_handle_in_review`) by threading the per-tick handles
through a new `_InReviewContext` dataclass, bundling the drift dev-resume outcome into a new `_DriftResume` dataclass,
and extracting decision-free helpers: `_fresh_issue_space` (merged issue/PR-conversation surface),
`_record_pending_fix_bookmarks` (per-surface fixing bookmarks), `_head_is_approved` (approval-gate probe), the drift
steps `_drift_unread_pr_conv` / `_drift_worktree` / `_resume_dev_for_drift` / `_dispose_drift_result`, and the handler
splits `_consume_fresh_feedback` and `_park_missing_pr_number`. `_bump_in_review_watermarks` dropped its unused
`review_space_new` / `review_summary_new` parameters (no caller passed them and their blocks were no-ops — the
`fixing` handler owns advancing the review-surface watermarks). The re-exported `_handle_in_review` /
`_comment_created_at` signatures plus all pinned-state, label, watermark, comment, and event behavior were preserved;
every cross-module call
stays late-bound through `from .. import workflow as _wf`, every extracted helper stays stage-private, and no new WPS
finding was introduced (the `WPS110` names raised during extraction were renamed to domain terms). The 68 focused
in_review tests passed unchanged and no Stage 3 finding was retained. All 68 focused in_review tests and 2,082 full
tests passed (32 skipped for the optional dashboard and live-Postgres dependencies).

The `conflicts.py` pass cleared all over-limit findings (the `WPS211` argument findings across
`_emit_conflict_round_incremented`, `_resume_on_user_content_change`, `_guard_diverged_worktree`,
`_push_recovered_commits`, `_publish_clean_rebase`, `_resolve_conflicts_with_agent`,
`_post_conflict_resolution_result`, and `_finalize_conflict_resolution`, plus the `WPS210` local-variable, `WPS213`
expression, and `WPS231` cognitive-complexity findings in `_handle_resolving_conflict`,
`_resume_on_user_content_change`, and `_resume_awaiting_human`) by threading the per-tick handles through a new
`_ConflictContext` dataclass and bundling the step outcomes into three more frozen records — `_WorktreeSync` (worktree
measured ahead/behind its remote branch), `_DivergeDecision` (the diverged-worktree guard's park-or-publish verdict),
and `_ConflictResumeRun` (one dev resume's outputs). `_handle_resolving_conflict` split into `_drive_conflict_rebase`,
`_prepare_conflict_worktree`, and `_rebase_and_dispose`, and the shared park / pushed-round / resume tails moved into
decision-free helpers (`_park_conflict`, `_park_conflict_missing_pr_number`, `_park_diverged_worktree`,
`_fetch_pr_branch`, `_fetch_base_ref`, `_ensure_conflict_worktree`, `_still_behind_base`, `_merge_result`,
`_awaiting_human_followup`, `_run_conflict_resume`, `_hand_resolved_round_to_validating`, and
`_flip_base_up_to_date`). The re-exported `_handle_resolving_conflict` signature plus the directly-referenced
`_pr_head_orchestrator_produced` / `_already_rebased_onto_base` probe signatures were preserved; every cross-module
call stays late-bound through `from .. import workflow as _wf`, every extracted helper stays stage-private, and no new
WPS finding was introduced (the `WPS237` f-string trap was avoided by binding `spec` locals rather than double-dotting
`ctx.spec`). Authenticated fetches, dirty/diverged worktree parking, rebase recovery, publish guards, resume behavior,
command ordering, locks, and emitted events were all preserved, so the 70 focused conflicts tests passed unchanged and
no Stage 3 finding was retained. All 70 focused conflicts tests and 2,082 full tests passed (32 skipped for the
optional dashboard and live-Postgres dependencies), completing Package 3.6.

Package 4.1 resolved the production naming findings for `WPS110`, `WPS111`, `WPS114`, `WPS115`, `WPS117`, and `WPS122`
across all 26 affected modules, cutting the six-rule production count from 253 to 24 documented remainders. Renames used
domain terms drawn from the surrounding code (e.g. the config dotenv/agent-spec helpers, the `comment_trust`
`_CommentT` type var and login/comment loop vars, the analytics filter `bindings` and `_day`/`raw` fields, the
usage/trajectory parse helpers, and the dashboard chart locals). `WPS115` was cleared by moving `github.py`'s
`_RECORDED_EVENTS_CAP` to module scope; `WPS117` by renaming the `dashboard_html` `cls` locals to `css_class`; `WPS122`
by dropping the `dashboard_charts` `_ = rows` stub, renaming `main.py`'s `_running` / `_received_signal` process globals
(matching the underscore-free `active_scheduler`), and collapsing the poll-loop counter to a bare `_`. To keep public
keyword contracts intact, parameters of public functions were left at their original names -- `fmt_money(value)`,
`cost_horizontal_bars(items)`, `parse_skill_matrix_sort(params)`, `coerce_workflow_label(value)` /
`coerce_child_issue_label(value)`, `parse_record(obj)`, and the `dashboard.__all__` HTML helpers
`_delta_pill(value)` / `_sparkline_svg(values, w, h)` -- and the public `TrajectoryStepView.content` /
`TimelineEntry.content` view fields were preserved, with focused keyword-call tests added to lock those contracts.
All 24 documented `WPS110` / `WPS111` / `WPS114` remainders are public/keyword or serialized surfaces (also
`PinnedState.data`, `usage` `TrajectoryStep.content`, the analytics `record_*` `result` inputs,
`IssueEventRow.result`, and the `PRESET_3D` / `PRESET_7D` dashboard-export identifiers) and are recorded in the
accepted-remainder register. No public contract,
pinned-state key, watermark, comment marker, event shape, or compatibility re-export changed; the full gate passed with
2,118 tests and 3 live-Postgres skips (the optional dashboard group installed so the plotly chart tests ran).

Package 4.2 resolved the production literal and repetition findings for `WPS204`, `WPS226`, `WPS358`, and `WPS432`,
cutting the four-rule production count from 318 to 78 documented remainders. `WPS226` was cleared by naming genuine
repeated concepts as module-local constants that hold the exact same string -- pinned-state keys and park-reason /
handler-outcome values in the stage handlers and `base_sync`, GitHub check/review/issue-state values in `github.py`,
the Claude/Codex usage-JSONL protocol field names in `usage.py`, the record field names in `analytics/sync.py`, and
the dotenv/backend tokens in `config.py` -- plus two small helper extractions (`_as_blockquote` for the repeated
`"> " + text.replace(...)` blockquote, shared by `workflow_messages` and `implementing`). `WPS358` was cleared
wherever a value is coerced by rewriting `float(x or 0.0)` to the behavior-identical `float(x or 0)` (and a
`_cost_cell` helper for the repeated `float(_row_value(...) or 0.0)` DB-column reads in the analytics readers, with a
focused equivalence test), plus `max(..., default=0)` clamps and `== 0` comparisons. `WPS432` was cleared by naming
domain values -- HTTP status codes, the signal-exit base, unit scales (`_MILLION` / `_BILLION`), SHA/snippet lengths,
the hex parse base, chart heights, and the UTC-offset range bounds. The 78 retained findings are documented in the
accepted-remainder register in eight grouped entries: irreducible float zeros (typed defaults, accumulator seeds, and
rate floors), terminal per-branch state writes and single-use value expressions (`WPS204`, where a helper would be the
same repeated call), positional DB row/column indices, the nice-number axis ladder and one-off empty-state heights,
git CLI argument tokens, plotly layout dict keys, the KPI-tile / stack-mode dict keys (`value` also blocks on
`WPS110`), and the categorical chart palette hues. No public contract, pinned-state key, watermark, comment marker,
event shape, or compatibility re-export changed, and no new WPS category was introduced in any touched file; the full
gate passed with 2,120 tests and 3 live-Postgres skips (the `test_workflow_list_pollable` default-tick assertion fails
only when `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` is exported in the shell -- an environment artifact reproducible on
`origin/main`, not a diff regression).

Package 4.3 resolved the production string-construction findings for `WPS237` and `WPS336`, cutting the two-rule
production count from 118 to 6 documented remainders (`WPS237` 74 -> 6, `WPS336` 44 -> 0). `WPS336` was cleared by
replacing every same-line explicit `+`/`+=` string concatenation with an f-string, an implicit-adjacency join into a
bound local, or -- for the two recurring blockquote and comment-quote fragments -- a shared helper. `WPS237` from
complex interpolation was cleared by binding the offending sub-expression to a domain-named local before the f-string:
`spec = context.spec` / `ctx.spec` to break the `context.spec.remote_name` double-dot chains, short-SHA locals
(`local_short`, `pre_rebase_short`, `before_short`, `pr_head_short`) for the `sha[:8]` slices, and locals for the
arithmetic (`elided`, `round_display`), boolean-or defaults (`author`, `session_id`, `allowed_text`), ternaries
(`current_label`, `exit_display`), and deep chains (`slug_token`, `owner_login`); the triple-nested
`", ".join(f"#{n}" ...)` decomposition messages were flattened by hoisting the join into a `_issue_ref_list` helper.
Two shared helpers were extracted to remove genuine duplication surfaced by the cleanup: `_quote_comment_line` (in
`workflow_messages.py`, re-exported through `workflow.py`) folds the `@author[label]: body` formatting that the
resume/PR-followup prompt builders and the fresh-comment stage handlers all repeated, and the already-existing
`_as_blockquote` was re-exported so the `conflicts`, `decomposition`, `documenting`, `validating`, and `workflow_drift`
blockquote sites reuse it instead of inlining `"> " + text.replace(...)`. Every extraction stayed under the Stage 3
complexity limits (`_read_remote_recovery_head` reused `remote_error` for the truncated snippet, `_run_parallel_tick`
inlined `min(...)`, `_dirty_worktree_message` inlined its single-use `quoted`, and `fixing`'s stale-head reason and
`decomposition`'s held-dependency line moved to helpers) so no new `WPS210`/`WPS226` was introduced. The 6 retained
`WPS237` findings are all irreducible numeric format specs in the dashboard formatters -- money `,.2f`
(`dashboard.py`, `dashboard_html.py`), zero-pad `02d` (`dashboard_charts.py`), and dynamic-precision `.{decimals}f` /
`,.{decimals}f` (`dashboard_theme.py`, `trajectory_dashboard.py`) -- recorded in the accepted-remainder register; each
would otherwise require changing the rendered output or replacing an idiomatic `f"{x:,.2f}"` with a `format()` dodge
that reads worse. No operator-visible message, prompt, SQL, persisted content, pinned-state key, watermark, comment
marker, event shape, or compatibility re-export changed, and no new WPS category was introduced in any touched file; a
focused `_quote_comment_line` keyword/fallback test was added and the full gate passed with 2,121 tests and 3
live-Postgres skips (the optional dashboard group installed so the plotly chart tests ran).

Package 4.4 resolved the production control-flow and expression-shape findings, cutting the sixteen-rule production
count from 127 to 5 documented remainders. Fully cleared: `WPS527` (38 `frozenset({...})` set-literal args rewritten as
tuple args, single-element cases keeping a trailing comma), `WPS504` (32 negated conditions -- 29 `is not None` / `not`
ternaries inverted to their positive form and 3 `if`/`else` statements with swapped branches, each provably
evaluation-order-identical since a ternary only evaluates the selected branch), `WPS229` (20 of 21 long `try` bodies
reduced to a single statement by extracting the protected body into a one-statement helper, combining protected calls,
or moving only a trivial non-raising tail out -- every step that can raise stays inside the guard so cleanup boundaries
are preserved), `WPS212` (7 over-limit return counts split at natural
boundaries: `_prune_jsonl_records`, `_recover_pending_auto_base_rebase_context`, `_sync_pr_worktree_to_base`,
`_handle_documenting`, `_fixing_preflight`, `_assess_question_outcome`, `_comment_body_for_hash`), `WPS509` (5 nested
ternaries lifted to `(cc or {}).get(...)`, a shared `_nested_usage_field` helper, and a bound local), `WPS529` (4
implicit `.get()` uses -- the codex step assembler uses a module `_MISSING` sentinel so a present-but-null
`aggregated_output` still emits its result step, pinned by a new `test_usage.py` regression test), `WPS435` (4 list
multiplications rewritten as comprehensions / generators), `WPS335` (2 `for ... in (genexpr)` loops rewritten with
`map()`), and one each of `WPS505` (nested `try` replaced by `Path.unlink(missing_ok=True)`), `WPS518` (`range(len())`
-> `enumerate`), `WPS328` (first-or-`None` loop -> `next(iter(...), None)`), `WPS238` (four-`raise` spec parser split
into a tokenizer helper), and `WPS219` (a five-deep `page.controls.filters.window.start` chain bound to a `filters`
local). Three clean resource-cleanup `try` / `finally` blocks (`WPS501`: the analytics read connection, the
question-run worktree teardown, and the main-loop scheduler drain) were converted to `@contextmanager` helpers. The 5
retained findings are recorded in the accepted-remainder register: the hashable public `cache_key` tuple (`WPS227`),
the decompose-handler cleanup guarantee whose `run_plan` is rebound then mutated mid-run (`WPS229` + `WPS501`), the
force-exit `os._exit` shutdown `finally` (`WPS501`), and the exactly-representable `2.5` nice-tick float comparison
(`WPS459`). No public contract, pinned-state key, watermark, comment marker, event shape, or compatibility re-export
changed, and the whole-tree `--select=WPS` diff introduced no new category (`WPS420` even dropped by one from removing a
now-unneeded `pass`); the full gate passed with 2,124 tests and 3 live-Postgres skips (the optional dashboard group
installed so the plotly chart tests ran). The `test_workflow_list_pollable` default-cadence test fails only when
`CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` is exported in the shell -- an environment artifact, not a diff regression.

Package 4.5 opened with the `usage-metrics` slice: the `UsageMetrics` dataclass and the claude / codex token, model,
turn, pricing, and cost parsing reached through `parse_agent_usage` (`parse_claude_usage` / `parse_codex_usage` plus
their price tables, event iterator, token decoders, model-path resolution, and turn counting) moved out of the
1,891-line `usage.py` into a focused private `orchestrator/_usage_metrics.py`. `orchestrator.usage` re-exports exactly
that public surface (`UsageMetrics` / `parse_agent_usage` / `parse_claude_usage` / `parse_codex_usage`) so `agents`,
`workflow`, and `analytics` keep importing from the same site, and it reuses the private module's shared event iterator,
token decoders, and `_claude_estimate_cost` price path for its sibling skill-trigger and trajectory extractors -- which
stay in `usage.py` -- so the resilience contract and cost precedence stay defined once. The re-export follows the
`worktrees.py` hub convention (absolute `from orchestrator._usage_metrics import ...`, grouped at eight names per
statement) so the split introduced no new `WPS300` local-import or `WPS235` too-many-names category, and the module
split cut the peak `WPS202` member count from 81 to 45 (`_usage_metrics`) + 36 (`usage`). The move resolved the 21
in-slice target findings: `WPS234` (7 -- the `_CLAUDE_RATES` / `_CODEX_RATES` row tables, the `_codex_rates` return,
and the four codex usage-event annotations, each lifted to a named alias `_TokenBucket` / `_ClaudeRateRow` /
`_CodexRateRow` / `_CodexUsageEvent`), `WPS338` (1 -- `_CodexPrice` reordered so the public `estimate` precedes the
private `_multipliers` / `_input_cost`), `WPS339` (12 -- the meaningless trailing zeros in the price literals, e.g.
`0.50` -> `0.5`, values unchanged), and `WPS420` (1 -- `_num`'s `except ValueError: pass` rewritten as
`contextlib.suppress(ValueError)`). The two remaining `WPS234` (`_ClaudeTurnUsageBuilder.by_key`) and `WPS338`
(`_CodexTrajectoryBuilder`) findings sit in the trajectory extractor that stays in `usage.py`; they are deferred to a
later Package 4.5 trajectory slice rather than fixed across the module boundary. Dataclass shapes, cost precedence,
pricing values, model fallback behavior, and every existing caller were preserved; a focused `test_usage.py`
compatibility test pins the re-export identity and each symbol's module of record, and `docs/observability.md` gained a
module-layout note. Ruff is clean and the full suite passed 2,096 tests (33 skipped for the optional dashboard / live
Postgres; the same `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` shell-export artifact accounts for the one otherwise-green
failure). Package 4.5 is not complete -- its remaining module-structure findings are untouched.

Package 4.5 continued with the `usage-skills` slice: the `SkillTriggers` dataclass and the claude / codex offered-skill
parsing and skill collectors reached through `parse_agent_skills` (`parse_claude_skills` / `parse_codex_skills` plus
`_collect`, `_claude_skill_name`, `_claude_offered_skills`, the `_ClaudeSkillCollector` / `_CodexSkillCollector`
collectors, and the `_CODEX_SKILL_PATH_RE` heuristic) moved out of `usage.py` into a focused private
`orchestrator/_usage_skills.py`. `orchestrator.usage` re-exports exactly that public surface (`SkillTriggers` /
`parse_agent_skills` / `parse_claude_skills` / `parse_codex_skills`) so `analytics` keeps importing from the same site,
and it reuses the private module's offered-set init-frame helpers (`_claude_init_field` / `_ordered_unique_names`) and
shared skill/trajectory JSONL vocabulary (`_CONTENT_KEY` / `_COMMAND_EXECUTION`) for its sibling trajectory classifier
-- which stays in `usage.py` -- so the init-frame parsing stays defined once. The re-export follows the same hub
convention as the `usage-metrics` slice (absolute `from orchestrator._usage_skills import ...`, grouped at eight names
per statement, public names aliased `as`), so the split introduced no new `WPS300` local-import or `WPS235`
too-many-names category; the new module's only WPS finding is the accepted `WPS202` module-member count (11 > 7, the
same cohesive-parser magnitude `_usage_metrics` carries), and the move cut `usage.py`'s `WPS202` count from 36 to 25.
The skill section carried no per-member target findings, so this slice fixes none beyond the member-count reduction; the
two deferred trajectory findings (`WPS234` `_ClaudeTurnUsageBuilder.by_key`, `WPS338` `_CodexTrajectoryBuilder`) and a
`WPS110` `content` still sit in the trajectory extractor that stays in `usage.py`. `SkillTriggers` shape, first-seen
ordering, per-name deduplication / counts, malformed-event handling, the names-only Privacy contract, and every
analytics consumer were preserved; the `test_usage.py` compatibility test now pins the skill re-export identity and each
symbol's module of record alongside the usage-metric one, and `docs/observability.md`'s module-layout note gained the
`_usage_skills` split. Ruff is clean and the full suite passed 2,097 tests (33 skipped for the optional dashboard / live
Postgres; the `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` shell-export artifact is unset for the run). Package 4.5 is not
complete -- its remaining module-structure findings are untouched.

Package 4.5 continued with the `usage-trajectory` slice: the `TrajectoryStep` / `TurnUsage` / `AgentTrajectory`
dataclasses and the claude / codex trajectory classifier reached through `parse_agent_trajectory`
(`parse_claude_trajectory` / `parse_codex_trajectory` plus the offered-tools / final-output / turn-key helpers, the
`_ClaudeTrajectoryBuilder` / `_ClaudeTurnUsageBuilder` / `_CodexTrajectoryBuilder` builders, and the
`_codex_assemble_steps` / `_turn_usage_from_row` assemblers) moved out of `usage.py` into a focused private
`orchestrator/_usage_trajectory.py`. `orchestrator.usage` re-exports exactly that public surface (`TrajectoryStep` /
`TurnUsage` / `AgentTrajectory` / `parse_agent_trajectory` / `parse_claude_trajectory` / `parse_codex_trajectory`) so
`analytics` keeps importing from the same site, and with this last slice `usage.py` becomes a pure facade -- three
re-export blocks over the private modules and zero module-level members of its own. The trajectory classifier reuses
`_usage_metrics`'s shared event iterator, token decoders, `_TokenBucket` alias, and `_claude_estimate_cost` price path
and `_usage_skills`'s init-frame helpers (`_claude_init_field` / `_ordered_unique_names`), shared skill/trajectory JSONL
vocabulary (`_CONTENT_KEY` / `_COMMAND_EXECUTION`), and skill-trigger extractors (`SkillTriggers` /
`parse_claude_skills` / `parse_codex_skills`) directly rather than through the facade it feeds, so the resilience
contract and cost precedence stay defined once and no import cycle forms. The re-export follows the same hub convention
as the earlier slices (absolute `from orchestrator._usage_trajectory import ...`, grouped names, public names aliased
`as`), so the split introduced no new `WPS300` local-import or `WPS235` too-many-names category, and it cut `usage.py`'s
`WPS202` member count from 25 to 0. This slice also cleared the two findings the `usage-metrics` slice deferred into the
trajectory extractor: `WPS234` (the `_ClaudeTurnUsageBuilder.by_key` `dict[str, tuple[int, str, dict[str, int]]]`
annotation, lifted to a named `_TurnUsageRow = tuple[int, str, _TokenBucket]` alias reused on `_turn_usage_from_row`'s
`row` parameter) and `WPS338` (`_CodexTrajectoryBuilder` reordered so the public `build` precedes the private `_item_id`
/ `_add_command` / `_add_message`). The new module's only remaining findings are the accepted `WPS202` member count
(25 > 7, the same cohesive-parser magnitude `_usage_metrics` carries) and a single `WPS110` `content` -- a serialized
`TrajectoryStep` field name that cannot be renamed without breaking the persisted trajectory schema. Serialized field
names, event ordering, final-output selection, tool-call / result pairing, per-turn cost source, malformed-stream
resilience, and every analytics consumer were preserved; the `test_usage.py` compatibility test now pins the trajectory
re-export identity and each symbol's module of record alongside the metric and skill ones, and
`docs/observability.md`'s module-layout note gained the `_usage_trajectory` split. Ruff is clean and the full suite
passed 2,097 tests (33 skipped for the optional dashboard / live Postgres; the `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS`
shell-export artifact is unset for the run). Package 4.5 is not complete -- its remaining module-structure findings are
untouched.

The `config-diagnostics` slice cleared all 20 configuration failure-path findings in `config.py` -- the 17 `WPS363`
`raise SystemExit` sites and the 3 `WPS421` direct-`print` sites -- by routing every one through two new module
helpers: `_config_error(message)` aborts import via `sys.exit(message)` (so `str(exc)` is still the message and the
exit code is still 1, which the import-time validation tests assert on) and `_config_warning(message)` writes the
diagnostic to `sys.stderr`. Exact messages, exit codes, the stderr-not-stdout stream, import-time validation, the
dotenv secret / token-file / missing-target warnings, and all parser edge cases were preserved, so every existing
`test_config.py` case passed unchanged; a new `ConfigDiagnosticsTest` pins the two helpers' message / exit-code /
stream contract at the producer level and the missing-target case now also asserts stdout stays empty. The two new
helpers nudge `config.py`'s `WPS202` member count (25 -> 27); both are cohesive configuration-diagnostics functions
that move together in any later module split, and that member-count finding is left for the rest of Package 4.5. Ruff
is clean and the full suite passed 2,099 tests (33 skipped for the optional dashboard / live Postgres; the
`CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` shell-export artifact is unset for the run). Package 4.5 is not complete -- its
remaining module-structure findings are untouched.

The `repository-config` slice cleared the config-diagnostics slice's deferred member-count pressure by extracting the
repository-configuration surface into a new private `orchestrator/_repo_config.py`: the `RepoSpec` per-repo identity
dataclass, the `_RepoEnvEntry` row model, the whole `REPOS` parser (entry tokenizing, owner/name and option validation,
duplicate-slug detection, remote-name defaulting, per-repo `parallel_limit` parsing) reached through `parse_repos_env`,
and the `build_repo_specs` default-spec construction that falls back to the legacy single-repo `REPO` /
`TARGET_REPO_ROOT` / `BASE_BRANCH` / `REMOTE_NAME` / `MAX_PARALLEL_ISSUES_PER_REPO` trio when `REPOS` is unset. The new
module is a stdlib-only leaf: the abort-on-invalid `_config_error` and warn-to-stderr `_config_warning` diagnostics stay
in `config.py` (its single configuration-failure funnel) and are injected as `config_error` / `config_warning`
callables, and the per-entry parallel-limit default is injected too, so `config.py` keeps importing nothing back and the
parser stays testable in isolation. `orchestrator.config` re-exports `RepoSpec` (`from orchestrator._repo_config import
RepoSpec as RepoSpec`) and keeps `_parse_repos_env` (a narrow wrapper that injects `MAX_PARALLEL_ISSUES_PER_REPO` and
the two diagnostics) and `default_repo_specs` (the cached-list accessor over `_REPO_SPECS =
_repo_config.build_repo_specs(...)`) so every keyword caller, `from orchestrator.config import RepoSpec` importer, and
`patch.object(config, ...)` target resolves unchanged. `RepoSpec` field shape and defaults, entry ordering,
duplicate detection, remote defaulting, parallel-limit validation, every error / warning message verbatim, the
stderr-not-stdout stream, and import-time abort semantics were all preserved, so every existing `test_config.py` case
passed unchanged; a new `RepositoryConfigModuleTest` pins the `RepoSpec` module of record, the two compatibility
wrappers' `orchestrator.config` home, and the injected-diagnostics leaf boundary. The move dropped `config.py`'s
`WPS202` member count from 27 to 18; the new module's only finding is the accepted `WPS202` (11 > 7, the same
cohesive-parser magnitude `_usage_metrics` carries), and the whole-tree `--select=WPS` diff introduced no new category.
`docs/architecture.md`'s module map gained the `_repo_config.py` entry and the `config.py` re-export note. Ruff is clean
and the full suite passed 2,104 tests (33 skipped for the optional dashboard / live Postgres; the
`CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` shell-export artifact is unset for the run). Package 4.5 is not complete -- its
remaining module-structure findings are untouched.

The `analytics-recording` slice extracted the analytics event-recording surface out of the 1,291-line
`orchestrator/analytics/__init__.py` into a focused private `orchestrator/analytics/_recording.py`: the six sink-knob
parsers (`_parse_log_path` / `_parse_retention_days` / `_parse_db_url` / `_parse_track_skill_triggers` /
`_parse_trajectory_log_path` / `_parse_trajectory_retention_days`), the JSONL append primitives (`build_record`,
`_append_jsonl_record`, `append_record`, `append_trajectory_record`) and their file locks, the prune machinery
(`prune_old_records` / `prune_trajectory_records` / `prune_with_retention_logging` and the shared temp-file rewrite
core), the stage recorders (`record_stage_enter` / `record_stage_evaluation`), repository-skill recording
(`record_repo_skill_catalog`), and the whole `record_agent_exit` arc including the opt-in trajectory sink (usage /
skill / codex-catalog parsing, redaction, head/tail + total-record truncation). `orchestrator.analytics` stays a
compatibility facade: it re-exports that public surface plus the test-visible names the flat module carried
(`log`, `os`, `usage`, `AgentResult`, `_FILE_LOCK`, `_TRAJECTORY_FILE_LOCK`, the `_TRAJECTORY_RECORD_BUDGET` /
`_TRAJECTORY_FIELD_HEAD` / `_TRAJECTORY_FIELD_TAIL` caps), keeps `__all__` byte-for-byte, and binds the six knobs as
its own package attributes by calling the parsers at import -- so `patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`,
the autouse conftest sink-disable, and `test_analytics.py`'s pop-and-reload of `orchestrator.config` +
`orchestrator.analytics` all keep landing on the package. The recorders read those knobs -- and call their sibling
recorders (`record_stage_enter` -> `append_record`, `prune_with_retention_logging` -> `prune_old_records`) -- back off
the facade at call time via `_recording._live_settings`, the same late-binding `workflow.py`'s stage modules use, so a
patched or reloaded value takes effect. To preserve the `_reload` A/B-world isolation the old in-`__init__` layout gave
for free (a submodule shared across reloads would otherwise bind its facade reference to whichever package instance
imported it last, hijacking stale holders such as `workflow` / the conftest / the recorder tests), the package
`__init__` evicts any cached `_recording` before importing so each package instance gets its own copy, and `_recording`
binds `_facade` to `sys.modules[__package__]` -- the specific instance importing it -- rather than resolving the package
off `sys.modules` at call time. The move cut the package `__init__`'s `WPS202` member count from 55 to 0 (a facade with
no members of its own), leaving `_recording` at the accepted cohesive-module magnitude (56 > 7, same class as
`_usage_metrics`), and resolved the slice's two `WPS234` overly-complex-annotation findings by lifting
`Optional[dict[str, list[str]]]` and `Optional[tuple[list[str], int]]` to the named `_SkillPaths` / `_KeptRemoved`
aliases. The facade's re-export block uses absolute `from orchestrator.analytics._recording import ...` grouped at eight
names per statement (the `worktrees.py` hub convention), so the split introduced no new `WPS300` local-import or
`WPS235` too-many-names category; the facade's remaining `WPS410` (`__all__`) and `WPS412` (init logic) and
`_recording`'s `WPS201` / `WPS211` / `WPS110` / `WPS342` / `WPS420` are pre-existing or public/keyword remainders the
flat module already carried. Redaction, event shapes, locks, fail-open persistence, skill discovery, and database-URL
handling were all preserved, so every existing analytics / workflow-analytics / skill-catalog / trajectory test passed
unchanged; a new `RecordingFacadeTest` pins the recorders' module of record, the facade routing of an internal
`append_record`, and the cross-reload isolation. `docs/observability.md` gained a module-layout note and its
settings-ownership pointer now names `_recording.py`, `docs/configuration.md`'s parse-site pointer was updated the same
way, and the `_usage_trajectory.py` cross-reference now points at `analytics._recording._maybe_record_trajectory`. Ruff
is clean and the full suite passed 2,107 tests (33 skipped for the optional dashboard / live Postgres; the
`CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` shell-export artifact is unset for the run). Package 4.5 is not complete -- its
remaining module-structure findings are untouched.

The `analytics-trajectories` slice split the opt-in trajectory sink out of `orchestrator/analytics/_recording.py` into a
focused sibling `orchestrator/analytics/_trajectories.py`: the head/tail + total-record truncation caps
(`_TRAJECTORY_FIELD_HEAD` / `_TRAJECTORY_FIELD_TAIL` / `_TRAJECTORY_RECORD_BUDGET`), the dedicated
`_TRAJECTORY_FILE_LOCK`, the budgeting dataclasses (`_TrajectoryHeadline` / `_TrajectoryBudget`), the redaction /
truncation helpers (`_truncate_head_tail` / `_redact_tree` / `_redact_and_truncate`), the headline / step / turn
builders (`_trajectory_usage` / `_trajectory_headline` / `_bounded_trajectory_turns` / `_trajectory_step` /
`_bounded_trajectory_steps` / `_build_trajectory_record`), the codex provider-change extraction
(`_codex_trajectory_changes` / `_agent_trajectory`), and the record producer / persistence
(`_persist_trajectory_record` / `_maybe_record_trajectory` / `append_trajectory_record` /
`prune_trajectory_records`). The move cut `_recording`'s `WPS202` member count from 56 to 39
(dropping the now-unused `dataclasses.replace` import to `WPS201` 16 -> 15) and left `_trajectories` at the accepted
cohesive magnitude (17 > 7, same class as `_usage_metrics` / `_recording`); the `WPS110` `result` params, `WPS211`
`record_agent_exit` argument count, and `WPS420` `pass` that stay in `_recording` are pre-existing public / keyword
remainders the flat module already carried. `_trajectories` depends one-directionally on `_recording` (absolute
`from orchestrator.analytics._recording import ...` grouped at seven names -- `build_record`, the shared
`_append_jsonl_record` / `_prune_jsonl_records` cores, `_live_settings`, `log`, and the `_AgentExitContext` /
`_CodexCatalog` shapes), so it introduced no `WPS300` local-import or `WPS235` too-many-names category; `_recording`
reaches back to the trajectory hand-off through the facade (`_live_settings()._trajectories._maybe_record_trajectory`)
rather than a direct import, so a `_recording` instance always dispatches to the same package instance's trajectory
recorder and the `_reload` A/B isolation is preserved. The package `__init__` now evicts `_trajectories` alongside
`_recording` and re-exports the six trajectory names (the three caps, the lock, `append_trajectory_record`,
`prune_trajectory_records`) from `_trajectories`, keeping `__all__` and every facade attribute byte-for-byte. The slice
also resolved the module's one `WPS342` implicit-raw-string finding by making `_redact_tree`'s backslash-carrying
docstring an explicit raw string (rendered text unchanged) and lifted the previously untyped `redact` callable to a
named `_Redactor = Callable[[str], str]` alias so the six redaction signatures annotate it without a `WPS234`
overly-complex-annotation finding. Redaction order, byte / step budgets, usage summaries, skill / tool catalog fields,
the `agent_trajectory` JSONL schema, the dedicated lock, and fail-open persistence were all preserved, so every existing
analytics / workflow-analytics / trajectory test passed unchanged; `RecordingFacadeTest` was split so
`append_trajectory_record` / `prune_trajectory_records` are pinned to `_trajectories` and the rest stay pinned to
`_recording`. `docs/observability.md`'s module-layout and settings-ownership notes name `_trajectories`, and both the
`_usage_trajectory.py` and `docs/observability.md` writer cross-references now point at
`analytics._trajectories._maybe_record_trajectory`. Ruff is clean and the full suite passed 2,108 tests (33 skipped for
the optional dashboard / live Postgres; the `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` shell-export artifact is unset for the
run). Package 4.5 is not complete -- its remaining module-structure findings are untouched.

The `analytics-retention` slice moved the by-age retention machinery out of `orchestrator/analytics/_recording.py` (and
the one trajectory prune wrapper out of `orchestrator/analytics/_trajectories.py`) into a focused sibling
`orchestrator/analytics/_retention.py`: the shared temp-file prune core (`_prune_jsonl_records`), the existence probe /
timestamp parse / line normalizer (`_probe_exists` / `_prune_timestamp` / `_normalized_jsonl_line`), the `_PruneScan`
kept/removed model and its `_KeptRemoved` alias, the read-and-partition step (`_read_kept_records`), the atomic-rewrite
helpers (`_unlink_quietly` / `_flush_fd_and_replace` / `_atomic_rewrite` / `_rewrite_pruned_file`), and the three public
prune entry points (`prune_old_records` / `prune_trajectory_records` / `prune_with_retention_logging`). `_retention`
depends one-directionally on its siblings (absolute `from orchestrator.analytics._recording import ...` for `_FILE_LOCK`
/ `_live_settings` / `log`, plus `from orchestrator.analytics._trajectories import _TRAJECTORY_FILE_LOCK`), so the
analytics and trajectory prunes hold the same per-sink locks their append side holds and neither can race an
`append_record` onto the soon-unlinked inode; `prune_with_retention_logging` still delegates through the facade
(`_live_settings().prune_old_records()`) so `patch.object(analytics, "prune_old_records", ...)` keeps intercepting it.
The package `__init__` now evicts `_retention` alongside `_recording` / `_trajectories` (each package instance imports
its own copy bound to the same facade its `_live_settings` reads) and re-exports the three prune names from
`_retention`, keeping `__all__` and every facade attribute byte-for-byte -- so `main._run_tick`, the trajectory-prune
cron viewer, and every focused prune test keep importing them from `orchestrator.analytics` unchanged. The move cut
`_recording`'s `WPS202` member count from 39 to 27 (dropping the now-unused `tempfile` / `timedelta` /
`dataclasses.field` imports, `WPS201` 15 -> 14) and `_trajectories`' from 17 to 16 (dropping its `datetime` /
`_prune_jsonl_records` imports), left `_retention` at the accepted cohesive magnitude (13 > 7, same class as
`_recording` / `_trajectories`), and resolved the slice's one `WPS420` empty-`except` finding by rewriting
`_unlink_quietly`'s `try / except OSError: pass` as `contextlib.suppress(OSError)`; the module already carried its
overly-complex prune-scan annotation as the named `_KeptRemoved` alias, so no `WPS234` finding travels with it. The
`WPS110` `result` params and `WPS211` `record_agent_exit` argument count that stay in `_recording`, and the `WPS410`
(`__all__`) / `WPS412` (init logic) on the facade, are pre-existing public / keyword remainders. File locks,
missing-file behavior, malformed-line retention, timestamps, fsync / `os.replace` ordering, temp-file permissions, and
failure logging were all preserved, so every existing analytics / trajectory / main prune test passed unchanged;
`RecordingFacadeTest` was split again so the three prune entry points are pinned to `_retention` while the recorders
stay pinned to `_recording` / `_trajectories`. `docs/observability.md`'s module-layout note and the `_recording` /
`_trajectories` / package docstrings now name `_retention` as the retention home. Ruff is clean and the full suite
passed 2,109 tests (33 skipped for the optional dashboard / live Postgres; the `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS`
shell-export artifact is unset for the run). Package 4.5 is not complete -- its remaining module-structure findings are
untouched.

The `dashboard-skill-matrix` slice moved the per-skill trigger matrix out of `orchestrator/dashboard_html.py` into a
focused private `orchestrator/dashboard_skill_matrix.py`: the `mtx_sort` / `mtx_dir` sort-param parser
(`parse_skill_matrix_sort`), the sortable inline-HTML table (`_skill_matrix_html`), and their supporting column model
(`_SkillMatrixColumn` / `_SKILL_MATRIX_COLUMNS` / `_SKILL_MATRIX_NUMERIC_KEYS` / `_SKILL_MATRIX_SORT_KEYS`), the sort
helpers (`_sort_skill_matrix_rows` / `_default_sort_skill_matrix_rows` / `_skill_matrix_default_sort_key`), the
clickable-header builders (`_SkillMatrixHeaderState` / `_skill_matrix_header_state` / `_skill_matrix_header_cell` /
`_skill_matrix_header_html`), the row view (`_SkillMatrixRowView` / `_skill_matrix_row_view` /
`_skill_matrix_row_html`), the muted offered-but-quiet zero-cell helper (`_muted_zero_html`), and the empty-state notice
(`SKILL_MATRIX_EMPTY_MESSAGE` / `SKILL_MATRIX_SORT_PARAM` / `SKILL_MATRIX_DIR_PARAM` / `_SKILL_MATRIX_EXTRA_CSS`). The
new module depends one-directionally on `dashboard_html` -- it imports the shared compact-table primitives (`_table_css`
/ `_table_html`) and the `_UNKNOWN` placeholder, which stay with the sibling issues / skill-trigger tables -- so no
import cycle forms. `orchestrator.dashboard` re-exports `_skill_matrix_html` and `parse_skill_matrix_sort` from the new
module under their original names (the two names moved from the `dashboard_html` import block to a new
`dashboard_skill_matrix` block), keeping `__all__` byte-for-byte, so `dashboard._skill_matrix_html` /
`dashboard.parse_skill_matrix_sort` and the `tests/test_reexport_surface.py` inventory resolve unchanged; the only other
consumer, `dashboard_widgets`, imports the two names from the new module too. The move dropped `dashboard_html.py`'s
`WPS202` member count from 58 to 44 (also dropping the now-unused `Callable` and `SkillTriggerMatrixRow` imports). The
new module retains its own `WPS202` member count (14 > 7) -- the accepted cohesive-leaf magnitude, the same class as
`_usage_metrics` / `_retention`, and the one extra `WPS202` finding is the inherent cost of splitting one over-limit
module into two cohesive ones. Its other two findings, the `_SKILL_MATRIX_SORT_KEYS` mutable-constant (`WPS407`) and the
`parse_skill_matrix_sort(params)` keyword-input (`WPS110`, already in the accepted-remainder register), are pre-existing
and travelled verbatim with the code, so tree-wide `WPS110` (17) and `WPS407` (20) counts are unchanged and the split
introduced no new rule category. The one test that reached `dashboard_html._sort_skill_matrix_rows` directly now targets
`dashboard_skill_matrix._sort_skill_matrix_rows`; every other test reaches the matrix through the `dashboard` facade
unchanged. Sort-param parsing, default / column sort order, the clickable-header markup, the muted zero cells, the
empty-state notice, and every consumer were preserved, so all 192 focused dashboard / reexport tests passed unchanged;
`docs/observability.md`'s module-layout note names the new module. Ruff is clean and the full suite passed 2,134 tests
(3 skipped for live Postgres; the optional dashboard group installed so the plotly chart tests ran, and the
`CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` shell-export artifact is unset for the run). Package 4.5 is not complete -- its
remaining module-structure findings are untouched.

The `trajectory-reader` slice split the record shapes and JSONL read side out of the 885-line
`orchestrator/trajectory_reader.py` into a focused private `orchestrator/_trajectory_records.py`: the event / timeline /
fixture constants and the `UNCONFIGURED_LOG_MESSAGE` banner, the five frozen record / view dataclasses (`TrajectoryRun`
plus its `TrajectoryStepView` / `TimelineEntry` / `TurnUsageView` / `RunUsageView` sub-views), the log-path resolution
(`resolve_log_path` / `log_unconfigured_message`), and the defensive coercion / parse / read pipeline (`_coerce_int` /
`_coerce_float` / `_coerce_str` / `_coerce_str_tuple` / `_as_list` / `_parse_step` / `_parse_run_usage` /
`_parse_turn` / `parse_record` / `_parse_trajectory_line` / `_read_trajectory_file` / `read_trajectories`).
`orchestrator.trajectory_reader` stays the public module: it re-exports that read surface under its original names (two
`from orchestrator._trajectory_records import ...` statements grouped at eight and five names so the split introduced no
new `WPS235` too-many-names category) and keeps the free-text filtering plus the filter-option / summary aggregation
(`FilterOptions` / `RunFilterOptions` / `_RunFilters` / `TrajectorySummary` and `filter_options` / `filter_runs` /
`summarize` with their `_matches_*` / `_normalize_*` helpers), which read the parsed `TrajectoryRun` from the leaf. The
new leaf depends one-directionally on `orchestrator.analytics` (for the live `TRAJECTORY_LOG_PATH` module attribute) and
stays import-light so importing either half never pulls Streamlit onto the polling tick's import surface; the only
consumer, `orchestrator.trajectory_dashboard`, and every test reach the models, constants, and read functions through
`trajectory_reader` unchanged. The move cut the peak `WPS202` member count from 36 to 19 (`_trajectory_records`) + 17
(`trajectory_reader`); every other finding travelled verbatim to whichever module now owns the code -- the two `WPS110`
`content` view-field names and the `WPS110` `parse_record(obj)` keyword input moved to the leaf (their
accepted-remainder register entry now names `_trajectory_records.py`), the `WPS214` twelve-method and `WPS338`
method-order findings on `TrajectoryRun` moved with the class, and the `WPS358` `total_cost_usd` float zero stayed on
`TrajectorySummary` in the facade -- so the tree-wide counts for those rules are unchanged and the split introduced no
new rule category (the sole net change is the one extra `WPS202`, the inherent cost of splitting one over-limit module
into two cohesive ones). Serialized `agent_trajectory` field names, newest-first ordering, the normalized timeline, run-
and per-turn usage views, the synthetic-fixture predicate, malformed-line resilience, and every consumer were preserved,
so all 91 focused trajectory-reader / trajectory-dashboard tests passed unchanged; a new `ModuleLayoutTest` pins each
moved symbol's module of record, the re-export identity through `trajectory_reader`, and that the filter surface stays
defined on the facade, and `docs/observability.md`'s read-model note names the new leaf. Ruff is clean and the full
suite passed 2,107 tests (33 skipped for the optional dashboard / live Postgres; the `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS`
shell-export artifact is unset for the run). Package 4.5 is not complete.

The `analytics-dashboard-trajectory-cleanup` slice (a PR-review follow-up on the trajectory-reader split) resolved the
first-round cohesive splits and the two feasible import regroups the remaining-work note below previously listed, and
closed the reload-isolation gap that split left. Five over-limit production modules each shed a cohesive leaf,
re-exported through the owning facade with `__all__` byte-for-byte intact: `orchestrator/analytics/sync.py` -> the
driver-free `orchestrator/analytics/_sync_rows.py` (record -> row mapping, the promoted-column / JSONB schema, and the
canonical-JSON content hash; `WPS202` 40 -> 27, leaf 13, `WPS201` 15 -> 14), `orchestrator/dashboard_charts.py` -> the
cost-bar `orchestrator/dashboard_charts_cost.py` and the shared-primitive `orchestrator/dashboard_charts_base.py`
(59 -> 28, cost leaf 24, base 7), `orchestrator/dashboard_html.py` -> inline-HTML
card family `orchestrator/dashboard_cards.py` (44 -> 31, leaf 13), `orchestrator/dashboard_widgets.py` -> the KPI-strip
aggregations `orchestrator/dashboard_kpi_strip.py` (42 -> 30, leaf 12, `WPS201` 24 -> 25 for one import a re-imported
leaf adds), and `orchestrator/trajectory_dashboard.py` -> the Streamlit-free HTML builders
`orchestrator/_trajectory_dashboard_html.py` (45 -> 26, leaf 19). The two `WPS235` regroups split every over-eight
re-export block into <= eight-name statements (the hub convention): `orchestrator/analytics/read.py`'s 18-name
`read_models` block -> 0, and `orchestrator/dashboard.py`'s 31- / 12- / 11-name blocks -> 0 (12-name `dashboard_html`
block dropped by the card split), clearing four `WPS235` findings (one on `read.py`, three on `dashboard.py`) at the
cost of `WPS201` 27 -> 33 on the facade, whose import count is an already-accepted re-export-surface remainder. The
reload fix: `trajectory_reader` now evicts its cached
`_trajectory_records` leaf before re-importing, so an A/B reload of `orchestrator.analytics` +
`orchestrator.trajectory_reader` binds the fresh reader to the fresh analytics instance and resolves its own world's
`TRAJECTORY_LOG_PATH` (regression `test_reload_binds_reader_to_matching_analytics`). The facade-backed splits each got a
module-of-record + facade-identity test (`CardHtmlExtractionTest` / `KpiStripExtractionTest`; the chart hub's
`ChartHubExtractionTest` is described below), plus the reads / widgets extraction tests updated for the moved members;
the private leaves got a module-of-record test (`SyncRowMappingExtractionTest`, `TrajectoryHtmlExtractionTest`); and
`docs/observability.md` names every new module. A
follow-up review round removed a leftover chart import cycle -- the cost leaf now takes the shared primitives from
`dashboard_charts_base`, which neither chart module's builders import, so a direct `import dashboard_charts_cost` is
cycle-free (`DirectLeafImportTest`) -- made the reload regression restore the `orchestrator` package's rebound submodule
attributes (not just `sys.modules`), and kept `read_trajectories`'s unreadable-file warning on the historical
`orchestrator.trajectory_reader` logger with a covering `test_unreadable_file_warns_and_returns_empty`. A third review
round finished the deferred chart split and carried it to its end: the usage-over-time hero family (`usage_over_time` /
`backend_per_day` plus the roll-up / trace / axis / layout helpers, 20 members), the weekday-hour heatmap
(`hour_weekday_heatmap`, 4), and the per-day throughput bars (`done_per_day_bars`, 4) each moved to a focused
`dashboard_charts_usage` / `dashboard_charts_heatmap` / `dashboard_charts_throughput` leaf, leaving
`orchestrator/dashboard_charts.py` a pure re-export hub (0 defined members, its `WPS202` gone) over those leaves plus
`_cost` and `_base` -- the same hub pattern as `dashboard` / `analytics.read`; a single looped `DirectLeafImportTest`
and a `ChartHubExtractionTest` cover every chart leaf's clean-import and hub re-export identity, and the
accepted-remainder register's cost / KPI / formatter / plotly-key entries were repointed at the modules that now own
those findings. Every re-export, optional-dependency / Streamlit boundary, package reload isolation, persisted event /
trajectory shape, and operator-visible output was preserved; ruff is clean and the full suite passed 2,151 tests (3
skipped for live Postgres; the dashboard group installed so the plotly chart tests ran; the
`CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` shell-export artifact unset for the run). Package 4.5 is not complete.

The `core-structure` slice closed Package 4.5 across the orchestrator core, git / worktree modules, workflow /
worktrees facades, and stage handlers. The removable import-structure findings were fixed: the fourteen `WPS458` import
collisions (thirteen `orchestrator.config` collisions on `base_sync`, `branch_publication`, `git_plumbing`,
`workflow_messages`, `worktree_lifecycle`, `workflow`, and the seven `stages/*` handlers, resolved by dropping the
duplicate `from orchestrator.config import RepoSpec` and qualifying uses as `config.RepoSpec`; plus the `logging`
collision in `main.py`), the three `WPS300` relative `from .. import workflow as _wf` imports in
`stages/implementing.py` (converted to the absolute `from orchestrator import workflow as _wf` every other stage already
uses), and the `WPS301` dotted `import logging.handlers` in `main.py` (converted to `from logging.handlers import
RotatingFileHandler`). No symbol moved, so no re-export surface, late-bound `_wf` call, state label, pinned-state key,
marker, watermark, event, command order, lock, or provider payload changed; the architecture doc, the developer /
review skills, and the `workflow.py` re-export comments were repointed from the relative to the absolute `_wf` import
form to match the now-uniform convention. The remaining module-structure findings are the documented-accepted remainders
below: the `workflow.py` / `worktrees.py` compatibility facades (`WPS201` / `WPS202` / `WPS203` / `WPS410`), the root
`orchestrator/__init__.py` metadata / init logic (`WPS410` / `WPS412`), and the cohesive core / orchestration / stage
modules whose `WPS202` member and `WPS201` / `WPS203` import counts a further split would only fragment (the same
cohesive-leaf magnitude the analytics / dashboard leaves carry). Ruff is clean, `git diff --check` is clean, and the
full suite passed 2,151 tests (3 skipped for live Postgres; the dashboard group installed so the plotly chart tests ran;
the `CLOSED_ISSUE_SWEEP_EVERY_N_TICKS` shell-export artifact unset for the run). Package 4.5 and Stage 4 are complete.

Package 4.5 module-structure inventory (the triage record, resolved by the `core-structure` slice above -- the
collision and import-shape findings were fixed and the count findings were accepted in the register). Package 4.5 is
"module and import structure" across the whole production codebase, so
its scope is not limited to the analytics / dashboard / trajectory slice this issue covered. The outstanding production
module-structure findings elsewhere are `WPS201` (imports), `WPS202` (members), `WPS203` (imported-name count),
`WPS300` / `WPS301` (local-folder / dotted imports), `WPS402` (`noqa` overuse), `WPS410` / `WPS412` (package metadata /
`__init__` logic), and `WPS458` (import collisions); `WPS235` (over-eight imported-name statements) is now clear
tree-wide. Each next action is a cohesive split or a public-surface-preserving change (or, for a metadata / import-shape
/ collision finding, a recorded accepted remainder).

- Analytics / dashboard / trajectory (this issue's slice) is fully split: `orchestrator/dashboard_charts.py` is now a
  pure re-export hub (0 defined members, no `WPS202`) over the per-family leaves `dashboard_charts_usage` (20),
  `dashboard_charts_cost` (24), `dashboard_charts_heatmap` (4), `dashboard_charts_throughput` (4), and
  `dashboard_charts_base` (7); every other extracted analytics / dashboard / trajectory module is a
  single-responsibility cohesive leaf. What remains here is only the accepted-remainder register (below), not a further
  split.
- Rest of the codebase (future per-module slices, each the same triage: extract a cohesive leaf, or record an accepted
  cohesive / compat remainder where a split would only fragment one cohesive unit). Compatibility re-export hubs,
  accepted like `dashboard`: `orchestrator/workflow.py` (`WPS202` 57 / `WPS201` 128) and `orchestrator/worktrees.py`
  (`WPS201` 52). Modules still carrying a member-count / import-count finding (members / imports): `base_sync.py`
  (60 / 19), `stages/implementing.py` (68 / 48), `stages/validating.py` (59 / 43), `stages/decomposition.py` (54 / 41),
  `workflow_messages.py` (51), `_usage_metrics.py` (45), `agents.py` (40 / 16), `stages/documenting.py` (36 / 36),
  `stages/fixing.py` (33 / 26), `stages/conflicts.py` (32 / 33), `stages/in_review.py` (25 / 23), `main.py` (25 / 17),
  `_usage_trajectory.py` (25), `worktree_lifecycle.py` (24), `stages/question.py` (22 / 20),
  `branch_publication.py` (21 / 14), `config.py` (18),
  `github.py` (17 / 15), `git_plumbing.py` (14), `skill_catalog.py` (12), `verify.py` (12), `_repo_config.py` (11),
  `_usage_skills.py` (11), `state_machine.py` (10), and `workflow_drift.py` (9). Each is decided per slice.
- Other outstanding module-structure families (record as future or accepted work, same triage):
  - `WPS203` (module with too many imported names, > 50): `stages/implementing.py` (52) trims when that stage's import
    fan-in is split, and the `workflow.py` (138) / `worktrees.py` (52) compatibility hubs are accepted like `dashboard`
    (141, already recorded below) because re-exporting the historical surface is their whole role.
  - `WPS300` (local-folder import): three `from . import ...` statements in `stages/implementing.py`, which convert to
    absolute imports when that stage is sliced.
  - `WPS301` (dotted raw import): one `import logging.handlers` in `main.py`, which converts to `from logging import
    handlers` when `main.py` is sliced.
  - `WPS402` (`noqa` overuse): 23 `noqa` on `orchestrator/dashboard.py` -- accepted; they mark the intentional
    E402-below-`sys.path`-shim compatibility re-exports, joining the register entry below.
  - `WPS410` (non-standard package metadata): the root `orchestrator/__init__.py` (`__version__` package version) and
    the `workflow.py` / `worktrees.py` `__all__` inventories -- accepted, joining the already-recorded `dashboard` /
    `analytics` / `analytics.read` `__all__` entries below.
  - `WPS412` (logic in an `__init__.py`): the root `orchestrator/__init__.py` -- accepted package-init wiring, joining
    the already-recorded `analytics/__init__.py` knob binding below.
  - `WPS458` (import collision -- a submodule imported both as `from pkg import x` and `import pkg.x`): fourteen
    findings, thirteen on `orchestrator.config` (`base_sync.py`, `branch_publication.py`, `git_plumbing.py`,
    `workflow.py`, `workflow_messages.py`, `worktree_lifecycle.py`, and the seven `stages/*` handlers) plus one on
    `logging` in `main.py`. Each resolves by choosing a single import form; recorded as future work.
- Documented-accepted remainders (do not split -- record in the accepted-remainder register instead): the `dashboard`
  and `analytics` / `analytics.read` compatibility facades carry `WPS410` (`__all__`), `WPS412` (package-init knob
  binding), `WPS203` (re-exported-name count on `dashboard`), and `WPS402` (`noqa` count on `dashboard`) because their
  whole role is to re-export the historical surface; `WPS201` import counts on `orchestrator/analytics/_recording.py`
  (14), `orchestrator/analytics/sync.py` (14, whose imports are all used -- the two test-only `_sync_rows` aliases were
  dropped without changing the statement count, since `WPS201` counts import statements, not names),
  `orchestrator/dashboard.py` (33, raised by the hub-convention regroup), and `orchestrator/dashboard_widgets.py` (25)
  trim only if a member split moves importers out (`trajectory_dashboard.py` was already cleared to 12 by dropping an
  unused `logging` import, no split needed); and the cohesive leaf / driver / page modules within the accepted `WPS202`
  magnitude --
  `analytics/sync` 27, `_sync_rows` 13, `_recording` 27, `_retention` 13, `_trajectories` 16, `connection` 11,
  `predicates` 10, `read_models` 18, `read_raw` 22, `read_rollup` 33, `read_dashboard` 31, `dashboard_reads` 31,
  `dashboard` 21, `dashboard_html` 31, `dashboard_cards` 13, `dashboard_kpi_strip` 12, `dashboard_charts_usage` 20,
  `dashboard_charts_cost` 24, `dashboard_widgets` 30, `dashboard_skill_matrix` 14, `dashboard_state` 15,
  `trajectory_dashboard` 26, `_trajectory_dashboard_html` 19, and the trajectory-reader modules 19 / 17 -- carry the
  accepted cohesive magnitude a further split would only fragment or move (`dashboard_charts` is a 0-member hub and
  `dashboard_charts_base` / `_heatmap` / `_throughput` sit at or below the 7-member limit, so they carry no finding).
