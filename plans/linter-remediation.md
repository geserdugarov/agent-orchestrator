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
| 4 | Remaining production style and structure | 4/5 | [ ] |
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

- File and symbol: `orchestrator/usage.py: TrajectoryStep.content`
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
  `value`), `orchestrator/dashboard_charts.py` (`cost_horizontal_bars`: `items`), and
  `orchestrator/dashboard_html.py` (`parse_skill_matrix_sort`: `params`).
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

- File and symbols: `orchestrator/trajectory_reader.py`: `TrajectoryStepView.content` and `TimelineEntry.content`
  (fields) plus `parse_record` (the `obj` parameter).
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
  (`duration_s` field default), `orchestrator/dashboard_charts.py` (`_empty_token_bucket` band seeds),
  `orchestrator/dashboard.py` (`_topbar_html(spend_in_range=0.0)`, the `[0.0, 0.0]` daily
  cost/token accumulator, and the rework-share `else 0.0`), `orchestrator/dashboard_html.py`
  (`_relative_width_pct` / `_safe_ratio` zero returns), `orchestrator/dashboard_kpis.py` (the
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
  `st.container(border=True)` in `orchestrator/dashboard.py`; `str(wt)` in
  `orchestrator/worktree_lifecycle.py`; and `row[0]` / `row[1]` in
  `orchestrator/analytics/read_dashboard.py` / `orchestrator/analytics/read_rollup.py`.
- Rule: `WPS204`
- Reason: These are void terminal calls (persist the pinned state, open a Streamlit container) or
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

- File and symbols: `orchestrator/dashboard_charts.py`: the `2.5` rung of the nice-number tick ladder
  in `_nice_axis_max`, and the one-off empty-state heights `330` and `150`.
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

- File and symbols: `orchestrator/dashboard_charts.py`: the plotly configuration keys `height`,
  `paper`, `color`, `size`, `margin`, `text`, `yaxis`, and the single-character margin/size keys `t`,
  `h`, `y`.
- Rule: `WPS226`
- Reason: These are plotly's own layout/trace dictionary vocabulary. Naming them -- especially the
  single-character `t` / `h` / `y` -- forces a reader to dereference a constant to recover the plotly
  attribute it stands for, which is less clear than the literal API key.
- Protected by: `tests/test_dashboard_charts.py`.
- Reviewed: [ ]

### Dashboard KPI-tile and stack-mode dict keys

- File and symbols: `orchestrator/dashboard.py`: the KPI-tile dict keys `label`, `value`, `delta`,
  `sub`, `spark`; the `summary` read-result key; and the `type` / `backend` stack-mode option values.
- Rule: `WPS226`
- Reason: The KPI-tile keys are the contract between the KPI builder in `dashboard.py` and the HTML
  renderer in `dashboard_html.py`; they read clearest as the same literal keys at both ends, and
  `value` additionally cannot become a constant without tripping `WPS110` (a blacklisted generic
  name). `type` / `backend` are the two stack-mode radio option values.
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

- File and symbols: the money `,.2f` specs in `orchestrator/dashboard.py` (`_cost_per_resolved`) and
  `orchestrator/dashboard_html.py` (`_money_or_dash`); the zero-pad `02d` hour label in
  `orchestrator/dashboard_charts.py` (`hour_weekday_heatmap`); and the dynamic-precision `.{decimals}f` /
  `,.{decimals}f` specs in `orchestrator/dashboard_theme.py` (`fmt_tokens`) and
  `orchestrator/trajectory_dashboard.py` (`_fmt_cost_usd`).
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

- File and symbol: `orchestrator/dashboard_charts.py: _nice_axis_max`
- Rule: `WPS459`
- Reason: The `norm <= 2.5` rung buckets a normalized magnitude against the standard
  `1 / 2 / 2.5 / 5 / 10` "nice tick" ladder (the same `2.5` rung already recorded as a `WPS432`
  remainder). `2.5` is exactly representable in binary floating point, so the representation-error
  concern `WPS459` targets does not arise, and rewriting the `<=` comparison to dodge the float literal
  (e.g. scaling by two to `2 * norm <= 5`) would obscure the ladder without changing the result.
- Protected by: `tests/test_dashboard_charts.py`.
- Reviewed: [ ]

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

Package 3.1 retained 18 reviewed API findings and passed 2,099 tests, 3 skips, and 627 subtests.

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
