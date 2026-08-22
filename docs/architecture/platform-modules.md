# Platform modules

This page maps the packages the workflow layer runs on: the package root and its two launch forms, the polling
`runtime/`, and the `config/`, `github/`, `agents/`, `scheduler/`, `git/`, and `skills/` domains. It is split out of
[`../architecture.md#top-level-layout`](../architecture.md#top-level-layout), which keeps the top-level map and the
naming rules that hold for the tree as a whole. The `workflow/` package is in
[`workflow-modules.md`](workflow-modules.md).

Each entry below is the responsibility its module owns, and it answers there and on no second site. What a stage does
with these owners is in [`../state-machine.md`](../state-machine.md).

## Enforced boundaries

Each rule below names what holds it, so a module that breaks one fails the suite rather than the next reader. The
last is held by the loader itself rather than by a check.

- **Layer position.** `config/` is the bottom layer and names nothing above itself; `github/`, `git/`, `agents/`,
  `scheduler/`, and `skills/` sit above it and below `workflow/`; `runtime/` and the two launch forms compose the
  lot. `tests/repository/test_layering.py` reads that direction twice, because deferring an import weakens where it
  lands but not whether it belongs.
- **At module scope, one exception.** The only name a lower layer may bind above itself is `workflow/state.py`, for
  the label vocabulary it is typed by, and only `github/` and `git/` may bind it — matched on the module boundary in
  the same check, so a sibling of the state owner cannot inherit the exemption by wearing the same prefix.
- **Over every scope, three more, each declared per module.** A base sync runs in the git layer but reports to the
  issue it was started for: `base_sync/conflicts.py`, `base_sync/persistence.py`, and `base_sync/publication.py`
  reach `workflow/engine/comments.py`, and `persistence` also `workflow/engine/guards.py`, inside the call that needs
  them. The same check declares those three per module: an undeclared hop fails wherever it is written, and one of
  these fails if it is bound at module scope after all — where it would be a cycle, since the workflow imports base
  sync back.
- **Package surfaces.** `github/`, `agents/`, and `scheduler/` publish a narrow `__all__` of their owners' own
  objects and nothing else; `runtime/`, `skills/`, `git/`, and every `git/` subpackage publish nothing at all, so
  naming one costs no owner behind it. `config/` is the deliberate exception: its initializer binds each resolved
  setting as a module attribute, which is the reload and patch target every caller reads one through. Each package's
  own tests hold its surface — a `test_imports.py` in the domains, `tests/config/test_surface.py` for the settings
  module — and `tests/repository/test_package_exports.py` holds the publish-or-front-nothing rule over the tree.
- **No second site.** No domain here sits behind a facade. Where a package replaced flat modules — `git/` and four
  of its six subpackages, `runtime/`, `skills/` — its own `test_imports.py` asserts that nothing resolves at the
  retired spelling, that no inventory or resolver hook names one as a target, and that no aggregate over the git
  domains sits above them — `tests/git/publication/test_imports.py` carries that last one. `git/measurement/` and
  `git/snapshots/` replaced nothing and hold the surface assertion anyway.
- **Operator log channels.** Four names are spelled literally rather than derived from `__name__`, because an
  operator's level and handler selection is keyed on them: `orchestrator.git_plumbing` (`git/authentication.py`,
  `git/snapshots/refs.py`, and the two `git/measurement/` owners that log, which all report on the same `ls-remote`,
  fetch, push, and diff plumbing),
  `orchestrator.base_sync` (`git/base_sync/state.py`), `orchestrator.worktree_lifecycle` (the four `git/worktrees/`
  owners that log), and `orchestrator.branch_publication` (`git/publication/rewrite.py`). A module moved between
  packages does not take its channel with it, and each of the four names is asserted where its owner is tested —
  `tests/git/test_authentication.py`, `tests/git/base_sync/test_state.py`, `tests/git/worktrees/test_imports.py`, and
  `tests/git/publication/test_imports.py`.
- **Import cost.** `import orchestrator` costs the root module and no owner behind it, and importing a `runtime/`
  owner plants neither the CLI nor an app — `tests/runtime/test_imports.py` and `tests/apps/test_imports.py`.
- **Direction inside `skills/`.** Neither owner may reach the workflow engine, a stage, or an application entry
  point: a catalog is observation the tick drives, not state a handler consults — `tests/skills/test_imports.py`.
- **Secrets.** `GITHUB_TOKEN` is read from the process environment or a token file outside `REPO_ROOT`, never from
  the `.env` an agent with sandbox bypass could read out of a sibling worktree: `config/_dotenv.py` skips every
  secret key it finds there and warns instead of loading it — see
  [`../configuration.md#github-personal-access-token`](../configuration.md#github-personal-access-token).

## The map

A package line names what its initializer publishes; where it names nothing, the initializer is a marker and callers
import an owner directly.

```
orchestrator/
  __init__.py           the distribution version and the `__all__` naming it, and nothing else
  cli.py                the `agent-orchestrator` console script: the polling process's composition point
  __main__.py           the `python -m orchestrator` launch form over `cli.main`, and what `run.sh` starts
  runtime/              the polling process's own owners
    state.py            the mutable state one run carries and the shell-style code a signal stop exits with
    logs.py             the stderr and rotating-file destinations a run settles before its first client
    startup.py          the run options, one client per configured repo, and the scheduler every tick shares
    ticks.py            one pass over the configured repos: the per-repo tick, the fan-out, and the reap / prune
                        drains
    loop.py             one-shot vs recurring polling, the interruptible wait, and the guaranteed scheduler drain
    self_update.py      the git probes behind the self-restart guard
    shutdown.py         the signal handler, the bounded-drain watchdog, and the forced exit it ends at
  config/               the resolved settings surface, bound as module attributes
    environment.py      the env-value parsers and the `_SettingsResolver` that reads and validates every knob
    _dotenv.py          the non-secret `.env` loader
    credentials.py      process / token-file credential resolution and the secret redactor the verify output, the
                        agent stderr diagnostics, and the trajectory writer mask with
    models.py           the `RepoSpec` / `RepoEnvEntry` repository-config types
    repositories.py     `REPOS` entry parsing, validation, and default-spec construction
  github/               publishes `GitHubClient` and `PinnedState`
    client.py           the authenticated client over the mixin chain: PyGithub setup, the worker-thread clone, and
                        the cached label reads with their confirmed-absent retry window and the one line a sweep's
                        absent legacy spellings are reported in
    aliases.py          the descriptor a stateless helper is bound onto the client with, so class, instance, and
                        module access all answer alike
    checks.py           status / check-run normalization, failure-before-pending folding, and the fail-closed check
                        reads
    comments.py         the `ALLOWED_ISSUE_AUTHORS` trust policy a caller filters a thread or gates one author
                        through; the low-level comment and review readers stay raw
    events.py           audit event record construction and the optional JSONL sink
    issues.py           issue polling and writes, the query options, the wire issue-state vocabulary,
                        and the closed predicate every reader of it asks through
    labels.py           the label vocabulary and bootstrap specs, and the in-place rename of a pre-namespace label
    pinned_state.py     the pinned durable-state model, the comment body it is written as and the length GitHub
                        takes, its parser, and the comment watermarks beside it
    pull_requests.py    PR lookup by open state, by commit, and when GitHub could not be asked at all, plus creation,
                        comments, body, labels, SHA-pinned merge, and remote-branch delete
    reviews.py          current-head review aggregation: approval verdicts and unread-feedback watermarks
  agents/               publishes the run models, `run_agent`, and `terminate_all_running`
    models.py           the agent result, run-option, and subprocess-result models
    environment.py      credential filtering and the injected git identity
    sessions.py         session-id and Claude final-message JSONL parsing
    processes.py        the shared process registry and the subprocess-group lifecycle
    runner.py           `run_agent`: backend dispatch, result assembly, and spawn logging
    backends/
      codex.py          Codex command construction, scratch output, and execution
      claude.py         Claude command construction and execution
  scheduler/            publishes `IssueScheduler` and `SubmissionRequest`
    models.py           the typed submission, the historical `submit` binding, and field normalization
    service.py          the concrete scheduler: the caps, the tracked claims, the family mutex, dispatch, and shutdown
  git/
    authentication.py   the per-repo token and askpass session, the authenticated fetches, the remote-ref reads that
                        answer what a branch or a whole refname is at without a local one, the lease-pinned branch
                        push, and the lease-pinned ref write and delete an immutable namespace is owned through
    commands.py         plain / hardened git execution, the argv hardening and no-prompt environment, the per-call
                        environment pin a caller adds over it, the absolute `--work-tree` argument a working-tree
                        operation names its tree with, and the unsafe local-transport probe
    locks.py            the per-target-root re-entrant lock registry and its accessor
    base_sync/          the per-tick base fetch and the auto-rebase of every worktree behind it
      refresh.py        the authenticated base fetch, worktree discovery, the sync gates, and the per-worktree route
      eligibility.py    the label, park, open-PR, recovery, and clean-tree gates one PR sync clears
      pre_pr.py         the hardened rebase / merge probes and the aborting pre-PR local rebase
      pr.py             the order a PR-having worktree's gates, rebase, and publication are asked in
      startup.py        the pre-rebase HEAD guard and the anchor persisted before git runs
      publication.py    the post-rebase checks, the lease-pinned force-push, and what an accepted push writes
      conflicts.py      the counter, notice, event, and relabel a genuinely conflicted rebase is handed to its stage
                        with
      guards.py         the no-op completion and the unreadable-HEAD, dirty-tree, and failed-push refusals
      snapshot.py       the branch fetch, the local / remote head reads and divergence counts, and the abort an
                        unreadable one takes
      recovery.py       the order a crash recovery asks its questions in, and the dirty-guarded reissued push
      outcomes.py       the already-published, unknown-comparison, diverged, dirty, and failed-push answers
      persistence.py    the parks, the reset-and-park tail, and the state / notice / event writes a recovery ends in
      models.py         the frozen contexts, requests, snapshots, and decisions
      state.py          the pinned-state keys, park reasons, refresh detour labels, and the shared logger
    publication/        what a branch becomes before review reads it
      planning.py       the merge-base, HEAD, dirty, and subject preconditions plus the squash message they select
      probes.py         the subject vocabulary and predicates, the ahead/behind counts, and the first-commit and
                        recent-base subject reads
      rewrite.py        the soft reset, the orchestrator-identity commit, the lease force-push, and the rollback a
                        post-reset failure takes
      squash.py         the plan-then-rewrite entry point a stage handler calls
      titles.py         subject-prefix inference and PR-title selection
    measurement/        how large a committed candidate is, and why a size is sometimes unknown
      models.py         the typed failure vocabulary, one frozen end of a diff, and the measurement record
      commits.py        the remote-authoritative base freeze (fetched once when the object is missing) and the
                        candidate proof that an id resolves, is held here, and peels to the commit it names
      additions.py      the `--numstat` added-line count over the frozen pair — read under the candidate's own
                        attributes and a named algorithm, pinned where git consults the environment last, and
                        refusing outright on the attribute file and diff-driver config no pin reaches — and the
                        measurement composing the three steps
    snapshots/          the immutable remote copy a superseded candidate is preserved as
      namespace.py      the one `refs/orchestrator/late-split/...` namespace a snapshot may occupy, built from a
                        generation's own identity and refused for anything else, plus the
                        `refs/orchestrator/late-split-local/<repository>/...` name this host's copy of one lands
                        under -- qualified because several configured repositories may share a clone, and bounded
                        because configuration bounds a slug at nothing
      refs.py           create-or-verify against the exact commit with no overwrite, the fetch-and-resolve that
                        proves a child could obtain it (one locked step, onto this repository's own local name),
                        and the absent-is-success delete -- leased at the preserved commit, so a re-pointed ref is
                        refused rather than reclaimed, and taking this host's copy down with the remote one
    verification/       what a verify run is, and the reads a checkout is judged by
      models.py         the `VerifyResult` statuses and fields, and the output budget
      output.py         the redact-then-truncate pass over captured verify output
      probes.py         the HEAD reads, the porcelain status in both its answers (the paths, and whether git could be
                        asked), and the two a named commit is judged by
      process.py        one command's group spawn / kill / drain and its verdict
      runner.py         the stripped child environment and the fail-fast command sequencing
    worktrees/          the per-issue checkouts an agent runs in
      paths.py          slug sanitization, git-ref-safe branch segments, path, branch, and pinned/legacy
                        resolution, and the exact set of names one issue's branch can be published under
      creation.py       issue and PR worktree creation, stale-worktree reuse and the probe it turns on, and the one
                        move that re-anchors a reused checkout onto a PR head or its merged base
      cleanup.py        lock-held worktree removal and local branch deletion, each behind its best-effort boundary,
                        plus the fail-closed read a caller that has to RECORD the teardown asks afterwards
      recovery.py       candidate-branch discovery, the unpushed-commit probe, and the tip read a recorded SHA is
                        compared against
      decomposition.py  the decomposer scratch path, its detached creation, and its best-effort removal
      terminal.py       question-stage teardown and terminal local and remote branch cleanup
  skills/
    catalog.py          the per-tick `git ls-tree` of a repo's `SKILL.md` definitions, the `project` level it
                        classifies every one of them at, and the one `repo_skill_catalog` record it appends
    discovery.py        the per-run scan of what a codex run was loaded with and the `project` / `user` /
                        `harness` level that defined each name, plus the skill roots, marker, and level
                        vocabulary `catalog.py` reads back
```

## Inside `git/`

The six subpackages bind their collaborators directly, so the dependency direction reads off the owner rather than
off a facade:

- `publication/` — `probes` calls `commands`; `titles` calls `probes`; `planning` calls `commands`, both siblings,
  and the verification probes; `rewrite` calls `commands`, `authentication`, and those probes; `squash` calls
  `planning` and `rewrite`.
- `verification/` — `output` calls `models`, `process` calls `output` and `probes`, and `runner` calls `process`.
- `measurement/` — `models` carries only data. `commits` calls `commands`, `authentication`, and the verification
  probes for the two object reads; `additions` calls `commands` and `commits`. Nothing here reaches the workflow
  layer, so the ceiling a count is compared against, and the verdict that comparison earns, stay with the caller.
- `snapshots/` — `namespace` is string policy and reaches nothing, which is what lets the late domain's lineage
  record consult it on every pinned read without paying for the transport; `refs` calls `authentication` for the
  remote read, the lease-pinned write and delete, and the fetch, and `commands` for the hardened local resolution
  that proves what the fetch brought. The workflow decides WHEN a snapshot is taken and what its absence costs; this
  package decides only what a snapshot ref IS and refuses everything outside it.
- `worktrees/` — the creators call `commands`, `locks`, `authentication`, and their `paths` / `recovery` siblings;
  `decomposition` resolves its own path helper; `terminal` composes its local teardown from `cleanup`.
- `base_sync/` — `models` and `state` carry only data. On the sync side `refresh` calls `pre_pr` and `pr`, `pr` asks
  `eligibility`, `startup`, and `publication` in that order, and `guards` ends in `persistence`. On the recovery
  side `recovery` calls `snapshot`, `outcomes`, and `persistence`. The three keyword-call adapters — the PR sync,
  the conflict route, and the crash recovery — still take the argument lists their callers spell and normalize each
  into the typed context entry point beside it.
