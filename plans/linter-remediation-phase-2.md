# Remaining Linter Findings Remediation Plan

## Goal

Remove every finding in the final Phase 1 inventory. This plan contains only currently open work; completed cleanup
and historical session notes are intentionally omitted.

Completion means the exact repository scan exits successfully with no findings. There is no accepted-remainder
fallback in this phase.

## Baseline

The 2026-07-22 clean-worktree scan used Python 3.12.3, Flake8 7.3.0, and wemake-python-styleguide 1.6.2:

```sh
uvx --no-cache --from wemake-python-styleguide flake8 orchestrator tests \
  --max-line-length=120 --extend-ignore=E741
```

| Scope | Findings | Affected files | Target |
|---|---:|---:|---:|
| Production | 223 | 61 | 0 |
| Tests | 1,350 | 102 | 0 |
| Total | 1,573 | 163 | 0 |

The scan has no standard `E...` or `F...` findings. All remaining findings are WPS rules.

## Rule inventory

| Rule | Production | Tests | Total |
|---|---:|---:|---:|
| `WPS110` wrong variable name | 18 | 1 | 19 |
| `WPS111` short name | 2 | 0 | 2 |
| `WPS114` underscored numeric name | 4 | 0 | 4 |
| `WPS201` too many imports | 19 | 11 | 30 |
| `WPS202` too many module members | 52 | 37 | 89 |
| `WPS203` too many imported names | 5 | 0 | 5 |
| `WPS204` overused expression | 10 | 269 | 279 |
| `WPS210` too many local variables | 0 | 318 | 318 |
| `WPS211` too many arguments | 26 | 16 | 42 |
| `WPS213` too many expressions | 0 | 130 | 130 |
| `WPS214` too many methods | 3 | 31 | 34 |
| `WPS226` overused string literal | 18 | 99 | 117 |
| `WPS227` oversized return tuple | 1 | 0 | 1 |
| `WPS229` long `try` body | 1 | 0 | 1 |
| `WPS230` too many public attributes | 0 | 3 | 3 |
| `WPS235` too many names imported from one module | 0 | 13 | 13 |
| `WPS237` complex f-string | 6 | 0 | 6 |
| `WPS301` dotted raw import | 0 | 2 | 2 |
| `WPS358` float zero | 30 | 43 | 73 |
| `WPS402` excessive `noqa` comments | 1 | 0 | 1 |
| `WPS407` mutable module constant | 1 | 0 | 1 |
| `WPS410` metadata variable | 6 | 0 | 6 |
| `WPS412` logic in `__init__.py` | 2 | 0 | 2 |
| `WPS430` nested function | 0 | 1 | 1 |
| `WPS432` magic number | 11 | 317 | 328 |
| `WPS441` control variable used after block | 0 | 49 | 49 |
| `WPS459` direct float comparison | 1 | 0 | 1 |
| `WPS501` `finally` without matching `except` | 2 | 2 | 4 |
| `WPS602` static method | 4 | 7 | 11 |
| `WPS615` unpythonic getter/setter | 0 | 1 | 1 |
| **Total** | **223** | **1,350** | **1,573** |

## Constraints

- Do not add `# noqa`, blanket ignores, per-file ignores, or weaker thresholds. A package is complete only when its
  findings disappear from the unchanged scan command.
- Preserve workflow labels, pinned-state keys, comment markers, watermarks, analytics records, provider payloads, and
  operator-visible messages.
- Preserve existing import paths, keyword-call behavior, facade attribute identity, and test patch points. When a
  static facade cannot satisfy the module limits, use the compatibility mechanism described below rather than deleting
  its surface.
- Do not reduce the test inventory by deleting scenarios or merging materially different behavior. Parameterization is
  appropriate only for cases with the same setup, operation, and assertion shape.
- Do not add dependencies. Every new Python or shell file must carry the repository license header.
- Split by responsibility, not by line count. Each extracted module must have a clear owner and remain below the
  relevant import/member threshold after its own scan.
- When stage handlers, workflow helpers, or public facades move, update `AGENTS.md`, architecture/workflow docs, module
  docstrings, and re-export tests in the same package.

## Shared remediation patterns

### Compatibility facades and package exports

The facade findings cannot be cleared by deleting compatibility names. Replace large static re-export modules with a
single, explicit compatibility registry:

1. Put each name-to-module mapping in responsibility-based private manifests with at most seven module members.
2. Import a small `__getattr__` and `__dir__` hook into the facade. The hook resolves only names present in the
   manifest and caches the resolved object in the facade namespace.
3. Serve the historical `__all__` and package version through the same hook instead of assigning metadata variables in
   the facade or package initializer.
4. Add `.pyi` stubs for static analysis if runtime imports no longer expose a statically declared name.
5. Extend `tests/test_reexport_surface.py` to verify every historical name, object identity, `from ... import ...`,
   wildcard export behavior, and `patch.object(workflow, ...)` interception.

This mechanism is limited to existing compatibility facades and package initializers. Normal implementation modules
must use direct imports and cohesive splits.

For `orchestrator/dashboard.py`, move direct-script path setup and Streamlit launch behavior into a bootstrap helper.
The compatibility hook must run after bootstrap without E402 suppressions, while both module import and the documented
direct-launch command remain covered by tests.

### Public functions and data models

- Replace argument-heavy implementations with frozen request/filter/context dataclasses.
- Preserve legacy keyword calls with thin `*args`/`**kwargs` adapters that bind against an explicit signature schema,
  reject unknown or duplicate arguments with the existing error type, and immediately delegate to the typed API.
- Publish the intended signature through adapter metadata and verify it with focused signature and keyword-call tests.
- Move stateless class helpers to module functions. Preserve required class access through aliases or small descriptors,
  and test instance/class invocation identity.
- Replace oversized return tuples with named frozen result objects that retain iteration/index compatibility during the
  migration.
- Split method-heavy classes by responsibility; keep only the stable coordinating surface on the original class.

### Modules, expressions, and literals

- Split modules before adding helpers so extracted code does not create another `WPS201`, `WPS202`, `WPS203`, or
  `WPS235` finding.
- Replace repeated terminal writes and query fragments with named decision/persistence helpers.
- Replace positional database rows with typed row objects or named unpacking at the query boundary.
- Replace protocol strings, dictionary keys, git tokens, status values, and rendering modes with nearby constants,
  enums, `TypedDict` definitions, or constructors according to their role.
- Use `field(default_factory=float)`, `float(0)`, or an existing numeric identity instead of `0.0` where the float type
  matters.
- Replace dynamic numeric format-spec f-strings with one tested formatting helper.
- Replace direct float comparison with `math.isclose` using a named tolerance.
- Replace mutable module registries with an owning object protected by the existing lock boundary.
- Express cleanup through a context manager or a small `try`/`except`/`finally` boundary whose protected body delegates
  to extracted work.

### Test structure

- Represent complex scenario input with frozen case dataclasses and small builders. Keep expected values visible at the
  test call site.
- Split scenario execution, projection, and assertions so each test stays within the local/expression limits without
  hiding the behavior behind one opaque helper.
- Split large test classes by behavior and large test modules by production owner. Keep every new test module below the
  import/member limits.
- Move protocol payloads and repeated domain values into focused fixture modules; do not create a repository-wide bag
  of unrelated constants.
- Replace context-manager capture reuse with a recorder object or helper that returns the populated capture after the
  context exits.
- Replace nested callbacks with module-level callable recorders and replace getter/setter-shaped fakes with properties.

## Progress summary

| Stage | Goal | Findings owned | Packages complete | Status |
|---|---|---:|---:|---:|
| 1 | Production cleanup | 223 | 2/3 | [ ] |
| 2 | Test cleanup | 1,350 | 0/7 | [ ] |
| 3 | Final zero-finding validation | 0 | 0/1 | [ ] |

Package counts are ownership counts from the baseline scan. Refactoring can move line numbers or expose a new finding,
but no package may use that drift to drop an item: new findings belong to the package that introduced them and must be
cleared before it closes.

## Stage 1 — Production cleanup

### Package 1.1 — Runtime core (22 findings, 9 files)

Scope: `orchestrator/__init__.py`, `_repo_config.py`, `agents.py`, `config.py`, `github.py`, `main.py`, `scheduler.py`,
`skill_catalog.py`, and `state_machine.py`.

Baseline: `WPS110` 2, `WPS201` 3, `WPS202` 7, `WPS211` 1, `WPS214` 2, `WPS410` 1, `WPS412` 1, `WPS501` 1,
and `WPS602` 4.

- [x] Move root-package initialization and version exposure behind the compatibility export hook.
- [x] Split the seven oversized implementation modules into configuration parsing, process lifecycle, GitHub query,
  scheduler state, and state-coercion leaves that each satisfy module limits.
- [x] Replace `IssueScheduler.submit`'s scheduling controls with a typed submission request plus a legacy keyword
  adapter.
- [x] Move GitHub stateless helpers to module functions while preserving class-level access.
- [x] Separate coordinating methods from the method-heavy scheduler and GitHub client classes.
- [x] Rename the two remaining generic public inputs through compatibility adapters.
- [x] Move forced-exit cleanup into an explicit shutdown context.
- [x] Run the focused agent/config/GitHub/main/scheduler/state tests and reduce this package's 22 findings to zero.

Completion gate: all nine scoped files and every new leaf scan clean; public package/version, scheduler, and GitHub
compatibility tests pass.

### Package 1.2 — Workflow, git, worktrees, and stages (54 findings, 17 files)

Scope: `base_sync.py`, `branch_publication.py`, `git_plumbing.py`, `verify.py`, `worktree_lifecycle.py`, `worktrees.py`,
`workflow.py`, `workflow_drift.py`, `workflow_messages.py`, and all eight `orchestrator/stages/*.py` handlers.

Baseline: `WPS201` 12, `WPS202` 16, `WPS203` 3, `WPS204` 7, `WPS211` 4, `WPS226` 7, `WPS229` 1, `WPS407` 1,
`WPS410` 2, and `WPS501` 1.

- [x] Read `docs/state-machine.md` and `docs/workflow.md` before changing this package.
- [x] Split git probing, synchronization decisions, state persistence, prompt construction, and each stage's routing,
  execution, and terminal tails into cohesive leaves.
- [x] Convert `workflow.py` and `worktrees.py` to the tested compatibility export registry while keeping late-bound
  workflow patching intact.
- [x] Group base-sync, conflict-routing, recovery, and developer-resume arguments into typed context/result objects with
  legacy adapters.
- [x] Replace repeated terminal state writes with named transition decisions and persistence helpers.
- [x] Model git subcommands/flags as immutable command fragments close to the plumbing layer.
- [x] Replace the mutable target-root lock mapping with a registry object that preserves the current lock lifetime.
- [x] Extract the decompose work from its cleanup `try` and express decompose/main cleanup through tested context
  boundaries.
- [x] Update facade inventories, architecture docs, workflow docs, and module docstrings after every move.
- [x] Run the focused workflow/git/worktree/stage tests and reduce this package's 54 findings to zero.

Completion gate: labels, pinned state, comments, commands, locks, events, and patch targets are unchanged; all 17 scoped
files and their new leaves scan clean.

### Package 1.3 — Analytics, dashboard, usage, and trajectory (147 findings, 35 files)

Scope: `orchestrator/analytics/`, `_usage_*.py`, `_trajectory_*.py`, `trajectory_*.py`, and every
`orchestrator/dashboard*.py` module in the baseline scan.

Baseline: `WPS110` 16, `WPS111` 2, `WPS114` 4, `WPS201` 4, `WPS202` 29, `WPS203` 2, `WPS204` 3, `WPS211` 21,
`WPS214` 1, `WPS226` 11, `WPS227` 1, `WPS237` 6, `WPS358` 30, `WPS402` 1, `WPS410` 3, `WPS412` 1,
`WPS432` 11, and `WPS459` 1.

- [ ] Split analytics reads by query/result family, recording by event family, usage by provider payload, and dashboard
  rendering by component so every implementation module satisfies import/member limits.
- [ ] Convert `analytics`, `analytics.read`, and `dashboard` to compatibility export hooks with complete `.pyi` and
  runtime surface tests.
- [ ] Separate the Streamlit direct-launch bootstrap and remove all E402 `noqa` annotations.
- [ ] Introduce typed filter/request objects for the 21 argument-heavy analytics and dashboard readers/helpers while
  preserving legacy keyword calls.
- [ ] Move trajectory views and analytics result models to domain-specific internal field names with compatible public
  properties/serialization.
- [ ] Replace the dashboard numeric preset identifiers with descriptive names and compatibility aliases.
- [ ] Replace positional row access with typed query-boundary rows and replace the dashboard cache tuple with a named
  hashable key object.
- [ ] Consolidate Plotly keys, modes, palette hues, and numeric rendering into typed constructors and focused
  formatters.
- [ ] Replace all float-zero literals and the direct axis-step float comparison without changing numeric output.
- [ ] Split the method-heavy trajectory model by view/aggregation responsibility.
- [ ] Run analytics/dashboard/usage/trajectory tests and reduce this package's 147 findings to zero.

Completion gate: database rows, JSON/events, pricing, charts, HTML, direct launch, cache behavior, and all historical
facade names remain compatible; all 35 scoped files and their new leaves scan clean.

## Stage 2 — Test cleanup

Perform test-module splits after the corresponding production package settles, so moved APIs are updated only once.
For every package, record collected test and subtest counts before editing and account for any change.

### Package 2.1 — Analytics, dashboard, and trajectory tests (474 findings, 18 files)

Baseline: `WPS110` 1, `WPS201` 3, `WPS202` 6, `WPS204` 41, `WPS210` 35, `WPS211` 10, `WPS213` 12,
`WPS214` 19, `WPS226` 70, `WPS230` 1, `WPS301` 2, `WPS358` 43, and `WPS432` 231.

- [ ] Split large files and classes by analytics read family, persistence path, dashboard component, chart family, and
  trajectory view.
- [ ] Build typed row/event/chart cases that replace positional payload construction, 231 magic numbers, 43 float
  zeros, and 70 repeated protocol/rendering strings.
- [ ] Extract only shared setup and projection steps needed to clear local/expression/argument findings while leaving
  expected rows, traces, HTML, and usage totals visible.
- [ ] Replace the reload idiom's dotted imports with a module-level reload helper that restores `sys.modules` and
  package attributes.
- [ ] Replace the external-API test-double name through a compatibility-signature fake.
- [ ] Reduce all 474 findings to zero without dropping an analytics/dashboard/trajectory scenario.

### Package 2.2 — Agent, usage, main, and configuration tests (161 findings, 4 files)

Baseline: `WPS201` 2, `WPS202` 3, `WPS204` 14, `WPS210` 9, `WPS211` 1, `WPS213` 4, `WPS214` 11,
`WPS226` 29, `WPS235` 2, and `WPS432` 86.

- [ ] Split tests by provider, parser event, process lifecycle, repository configuration, and error behavior.
- [ ] Represent Claude/Codex wire payloads, pricing inputs, token totals, exit statuses, timeouts, and signals with
  focused case models and protocol fixture builders.
- [ ] Replace repeated reload/dispatch expressions with named entry-point helpers that preserve environment isolation.
- [ ] Split oversized classes and reduce helper arguments/locals without merging distinct malformed-payload cases.
- [ ] Reduce all 161 findings to zero while preserving provider-envelope and subprocess-cleanup coverage.

### Package 2.3 — Scheduler, base-sync, publication, and worktree tests (116 findings, 8 files)

Baseline: `WPS202` 6, `WPS204` 33, `WPS210` 24, `WPS213` 31, `WPS230` 1, `WPS235` 1, `WPS430` 1,
`WPS441` 17, and `WPS501` 2.

- [ ] Split scheduler, synchronization, publication, cleanup, and serialization tests by decision branch and failure
  boundary.
- [ ] Replace command/result setup with typed scenarios and separate operation from ordered-call assertions.
- [ ] Replace context capture reuse with populated recorders and the nested race callback with a module-level callable
  probe.
- [ ] Express scheduler/worktree cleanup barriers with context helpers that retain exception and lock ordering.
- [ ] Reduce all 116 findings to zero while keeping real-git and concurrency coverage intact.

### Package 2.4 — Decomposition, question, and documenting tests (137 findings, 11 files)

Baseline: `WPS201` 1, `WPS202` 3, `WPS204` 43, `WPS210` 55, `WPS213` 19, and `WPS441` 16.

- [ ] Split stage tests by routing, execution, resume, trust filtering, drift, cleanup, and terminal behavior.
- [ ] Use typed pinned-state/comment/agent-outcome cases to reduce repeated expressions and locals without obscuring
  transition inputs.
- [ ] Return populated log/patch captures from focused helpers instead of reading bound context variables later.
- [ ] Reduce all 137 findings to zero with identical labels, comments, manifests, and ordered-write assertions.

### Package 2.5 — Implementing and fixing tests (128 findings, 11 files)

Baseline: `WPS201` 2, `WPS202` 3, `WPS204` 44, `WPS210` 62, `WPS213` 15, and `WPS235` 2.

- [ ] Split by fresh run, retry/backend behavior, drift, full spec, PR reuse, timeout, pause, feedback, and terminal
  routing.
- [ ] Replace large scenario setup blocks with typed state and agent-result cases while keeping per-field expectations
  in each test.
- [ ] Import production collaborators through modules or smaller focused groups after splitting files.
- [ ] Reduce all 128 findings to zero without combining different retry, feedback, or terminal routes.

### Package 2.6 — Validating, in-review, and conflict tests (220 findings, 30 files)

Baseline: `WPS201` 2, `WPS202` 4, `WPS204` 67, `WPS210` 96, `WPS213` 33, `WPS235` 2, and `WPS441` 16.

- [ ] Split by review, verify, squash, handoff, watermark, checks, feedback filtering, migration, rebase, recovery,
  publication, resume, authenticated fetch, and worktree restoration.
- [ ] Model watermark/state/comment/command inputs as typed cases and separate setup, operation, and event assertions.
- [ ] Replace post-context capture reads with focused recorder results.
- [ ] Reduce all 220 findings to zero while preserving command ordering, security probes, retry budgets, and review-loop
  semantics.

### Package 2.7 — Shared fakes, workflow harness, and cross-cutting tests (114 findings, 20 files)

Scope includes `tests/fakes.py`, `tests/workflow_helpers.py`, and affected state-machine, skill, drift, lifecycle,
analytics, event, polling, prompt, and terminal tests not owned above.

Baseline: `WPS201` 1, `WPS202` 12, `WPS204` 27, `WPS210` 37, `WPS211` 5, `WPS213` 16, `WPS214` 1,
`WPS230` 1, `WPS235` 6, `WPS602` 7, and `WPS615` 1.

- [ ] Split `FakeGitHubClient` into cohesive stores/services behind the existing fake-client surface and move stateless
  helpers to module functions.
- [ ] Replace the getter/setter pair with a property and group public histories into typed read-only views.
- [ ] Replace argument-heavy workflow mixins with typed patch/run contexts and split them by stage family.
- [ ] Split remaining large modules/classes and simplify scenario setup without hiding state/event assertions.
- [ ] Reduce all 114 findings to zero and rerun every workflow suite because these helpers are shared broadly.

Stage 2 completion gate: the complete `tests/` tree has zero findings, all 2,207 baseline tests remain accounted for,
and no helper erases materially different setup or assertions.

## Stage 3 — Final validation

### Package 3.1 — Zero-finding repository gate

- [ ] Start from a clean worktree and run the exact baseline Flake8 command; require exit code 0 and empty output.
- [ ] Confirm the rule inventory has 0 production findings, 0 test findings, and no newly exposed rule family.
- [ ] Run `.venv/bin/python -m ruff check orchestrator tests`.
- [ ] Run `git diff --check origin/main...HEAD` and `git diff --check`.
- [ ] Run `.venv/bin/python -m pytest tests`; require every tracked test to pass apart from the three explicitly skipped
  live-Postgres integration tests when `ANALYTICS_TEST_DB_URL` is unset.
- [ ] Audit test collection against the 2,207-test baseline and explain every added, removed, or parameterized case.
- [ ] Run re-export, direct-launch, public-signature, serialized-shape, and real-git focused suites once more.
- [ ] Update this plan's progress tables and close all packages only after the zero-finding scan passes.

Completion gate: 0 parsed findings, 0 unique findings, Ruff clean, diff checks clean, and the complete tracked test
suite green.

## Validation gate for every package

- [ ] The package's scoped scan reaches zero findings.
- [ ] A repository-wide scan shows no new finding and no increase outside the package.
- [ ] Focused tests for every changed behavior pass.
- [ ] Ruff and both diff checks pass.
- [ ] The full tracked test suite passes after a production contract/facade change and at each stage boundary.
- [ ] Added or modified tests protect distinct behavior and avoid duplicated setup.
- [ ] Comments and test docstrings describe current invariants rather than the refactor history.
- [ ] Documentation and compatibility inventories match every moved symbol.
