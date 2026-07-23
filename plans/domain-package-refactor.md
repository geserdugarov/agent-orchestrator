# Domain-Based Package Refactor

## Summary

The repository currently has 449 production Python files (270 directly under `orchestrator/`) and 501 test files in
one flat directory. Refactor both trees into domain packages, replace prefixed filenames with responsibility-based
module names, and remove the broad private-helper compatibility system.

Runtime behavior, workflow labels, pinned-state JSON, GitHub interactions, and subprocess semantics remain unchanged.
Delivery will be a phased PR series with a green full suite after every phase.

## Target Structure

```text
orchestrator/
  __init__.py
  __main__.py
  cli.py

  config/
    __init__.py
    environment.py
    repositories.py
    credentials.py
    models.py

  agents/
    __init__.py
    models.py
    runner.py
    environment.py
    sessions.py
    processes.py
    backends/
      codex.py
      claude.py

  github/
    __init__.py
    client.py
    pinned_state.py
    issues.py
    pull_requests.py
    reviews.py
    checks.py
    labels.py
    events.py

  scheduler/
    __init__.py
    models.py
    service.py

  workflow/
    __init__.py
    state.py
    engine/
      tick.py
      dispatch.py
      pickup.py
      terminals.py
      comments.py
      prompts.py
      messages.py
      drift.py
      usage.py
      guards.py
    stages/
      decomposition/
      implementing/
      documenting/
      validating/
      in_review/
      fixing/
      conflicts/
      question/
        __init__.py
        handler.py
        models.py
        state.py
        <responsibility>.py

  git/
    commands.py
    authentication.py
    locks.py
    worktrees/
    publication/
    base_sync/
    verification/

  observability/
    analytics/
      recording/
      query/
      sync/
      trajectories/
      config.py
      retention.py
    usage/
    dashboard/
    trajectory_viewer/

  skills/
    catalog.py
    discovery.py

  apps/
    bootstrap.py
    analytics_dashboard.py
    trajectory_dashboard.py
```

Tests will mirror these domains:

```text
tests/
  config/
  agents/
  github/
  scheduler/
  workflow/
    engine/
    stages/<stage>/
  git/
  observability/
    analytics/
    usage/
    dashboard/
    trajectory_viewer/
  skills/
  apps/
  support/
```

Test directories become Python packages, allowing concise names such as
`tests/workflow/stages/implementing/test_retry.py`. Stage-local fixtures belong in the nearest `conftest.py`;
reusable GitHub fakes, factories, and real-git helpers move under `tests/support/`.

## Interfaces and Implementation

- Replace dynamic export manifests, `__getattr__` compatibility registries, and broad helper re-exports with ordinary
  explicit imports and narrow `__all__` declarations.
- Keep intentional package APIs only:
  - `orchestrator.agents`: result/options models, `run_agent`, and process termination.
  - `orchestrator.config`: repository/configuration models and resolved settings.
  - `orchestrator.github`: `GitHubClient`, pinned-state model, and documented constants.
  - `orchestrator.scheduler`: scheduler and submission models.
  - `orchestrator.workflow`: `tick`, label types, transition guards, and workflow exceptions.
- Internal code imports the module that owns a symbol. Tests patch that owning module or an injected collaborator rather
  than a compatibility facade.
- Enforce dependency direction: infrastructure and observability cannot import workflow or application entrypoints;
  workflow may consume infrastructure; `cli` and `apps` compose all domains.
- Preserve lazy loading only where optional Streamlit/Plotly imports require it, implemented with local imports inside
  application entry functions rather than export resolvers.
- Introduce canonical launch commands:
  - `uv run agent-orchestrator` via `orchestrator.cli:main`.
  - `uv run python -m orchestrator` as an equivalent module entrypoint.
  - `uv run streamlit run orchestrator/apps/analytics_dashboard.py`.
  - `uv run streamlit run orchestrator/apps/trajectory_dashboard.py`.
- Update `run.sh`, README, environment examples, and architecture/configuration/observability documentation. Remove the
  old `python -m orchestrator.main` and top-level dashboard script paths in the final phase.
- Do not change dependencies, environment-variable semantics, workflow labels, pinned-state fields, prompts, event
  shapes, or GitHub-visible behavior.

## Phased Delivery

1. **Foundations and runtime**
   - Add the new CLI/application entrypoints.
   - Convert config, agents, GitHub, and scheduler modules into packages.
   - Move their tests and support code concurrently.

2. **Git and worktree infrastructure**
   - Move plumbing, worktree lifecycle, branch publication, base synchronization, and verification into `git/`
     subpackages.
   - Replace facade callbacks with explicit owning-module imports.

3. **Workflow engine**
   - Move labels, transition guards, tick scheduling, dispatch, pickup, messages, drift, prompts, usage, and terminal
     handling into `workflow/`.
   - Preserve all state-machine and pinned-state behavior.

4. **Workflow stages**
   - Move each stage into its own package, one or two related stage families per PR.
   - Keep handlers thin and place models, state, recovery, persistence, and routing in responsibility-named modules.
   - Relocate matching tests in the same PR.

5. **Observability and applications**
   - Restructure analytics into recording, query, sync, retention, and trajectory packages.
   - Move usage parsing, dashboards, trajectory reading, and skill discovery into their final domains.
   - Switch documentation and operator commands to the new application entrypoints.

6. **Compatibility removal and enforcement**
   - Delete legacy forwarding modules, export manifests, dynamic resolver hooks, obsolete `.pyi` files, and old flat
     test support files.
   - Add architecture tests preventing new domain-prefixed files at the package root and forbidden cross-layer imports.
   - Update the authoritative module map and repository guide.

Temporary forwarding modules may keep intermediate PRs deployable, but contain no implementation and are removed in
phase 6.

## Test Plan and Acceptance Criteria

- Run Ruff, `git diff --check`, and the complete pytest suite after every PR.
- Preserve existing behavioral coverage for state transitions, agent lifecycle, GitHub operations, git recovery,
  analytics, dashboards, and operator shutdown.
- Add tests for:
  - The new console and `python -m orchestrator` entrypoints.
  - Both relocated Streamlit scripts without dashboard dependencies installed during ordinary imports.
  - Exact narrow package exports and absence of legacy helper exports.
  - Import-cycle-free package initialization and permitted dependency direction.
  - The required directory layout and absence of `_workflow_*`, `_dashboard_*`, `_usage_*`, and similar flattened
    production filenames.
  - Test collection from nested packages with no duplicate-module collisions.
- Final acceptance requires no behavior/schema migration, no legacy compatibility manifests, no production
  implementation leaves flattened at the repository package root, and no `*_test_support.py` families remaining in
  the test root.
