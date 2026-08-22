# Architecture

Single-process **polling orchestrator** that drives GitHub issues through a label-based state machine, delegating coding
work to a configurable coding-agent CLI (`codex` or `claude`) running as a subprocess in isolated git worktrees.

State lives in GitHub: a workflow label exposes the current stage and a pinned JSON comment holds per-issue durable
state. The orchestrator process is stateless and can restart at any time.

This page is the system overview: the design constraints, the top-level module map, the process model, the agent
subprocess and the environment boundary around it, the hardened git and push path, and the schema diagram. Every
other area keeps its heading here as a summary that names the page owning it, so a link written against this file
still lands beside the material it pointed at. The per-package inventory under the map is in
[`architecture/platform-modules.md`](architecture/platform-modules.md),
[`architecture/workflow-modules.md`](architecture/workflow-modules.md), and
[`architecture/observability-modules.md`](architecture/observability-modules.md); the label set, per-stage internals,
what one tick reads and writes per issue, and the pinned-state schema in [`state-machine.md`](state-machine.md) and
the pages under it; agent roles, conversation contracts, and command-spec semantics in [`workflow.md`](workflow.md)
and the pages under it; and the observation-only sinks and the two pages over them in
[`observability.md`](observability.md).

## Design constraints

GitHub Issues are the orchestrator's task tracker and durable state surface. The process intentionally avoids an
internal database: workflow labels expose the current stage, and the pinned JSON comment holds the per-issue state that
the next tick needs. This keeps progress visible to humans on github.com and lets the process restart without
reconstructing hidden local state.

The orchestrator is not fully autonomous. When a stage hits uncertainty, an unsafe repository state, a malformed agent
response, or an exhausted retry cap, it parks with `awaiting_human` and mentions `HITL_HANDLE`; a later human issue
comment is the resume signal for the parked agent session.

The workflow is deliberately fixed instead of planner-selected: decomposition, implementation, validation, and
acceptance are mandatory phases. Routing is explicit and label-driven.

Agents run on the host as CLI subprocesses with broad local permissions
(`codex --dangerously-bypass-approvals-and-sandbox`, `claude --dangerously-skip-permissions`). The host, container, or
VM around the orchestrator is therefore the real sandbox boundary; token handling and hardened git operations are
designed around that assumption.

## Top-level layout

A responsibility answers on the owner module that defines it and nowhere else — the workflow package, the whole
analytics tree, and both Streamlit pages included — so a patch targets that module and there is no second site a mock
could be left on. Where a leaf does resolve a name at call time it is to read a knob rather than to borrow a helper,
off the one holder every owner that reads it resolves through. Each of those boundaries is named where its owner is
described, on the pages named below.

A bare tag — `implementing`, `fixing`, `validating` — names the *stage*: the handler and the subpackage holding it,
mapped in [`architecture/workflow-modules.md`](architecture/workflow-modules.md). For a stage the orchestrator labels
itself, the GitHub label an issue carries is a different string, spelled `workflow:<tag>` here and everywhere else in
these docs. `in_review`, `question`, `discussion`, and
the `done` / `rejected` terminals were never namespaced, so for those the two coincide; see
[Workflow labels](#workflow-labels).

The map is split by area, and each page below is where the owners of the packages it names are described:

- [`architecture/platform-modules.md`](architecture/platform-modules.md) — the package root and both launch forms,
  `runtime/`, `config/`, `github/`, `agents/`, `scheduler/`, `git/`, and `skills/`.
- [`architecture/workflow-modules.md`](architecture/workflow-modules.md) — `workflow/`: the package API and the state
  owner beside it, the `engine/` owners one tick is composed of, the `late_split/` domain the late size gate is
  defined by, and the nine stage subpackages the label dispatch routes into.
- [`architecture/observability-modules.md`](architecture/observability-modules.md) — `observability/`: the analytics
  sink and everything downstream of it, the usage parser, the Streamlit analytics page, and the file-backed
  trajectory viewer, together with the two `streamlit run` targets under `apps/` that compose the pages.

The rules under the map hold for the whole tree, the packages on those pages included.

```
orchestrator/
  __init__.py           the package version and the `__all__` naming it, bound
                        here so `import orchestrator` costs no owner behind it
  cli.py                `agent-orchestrator` console-script entry point and
                        the polling process's composition point
  __main__.py           `python -m orchestrator` launch form over `cli.main`;
                        the target `run.sh` launches
  runtime/              the polling process's own owners: the state one run
                        carries, the log destinations, startup, one pass over
                        the configured repos, the polling loop, the
                        self-restart probes, and shutdown
  config/               the bottom layer: the non-secret `.env` loader, the
                        env parsers and resolver behind the settings surface,
                        credential resolution and secret redaction, and the
                        repository-config types
  github/               the composed `GitHubClient` and the pinned durable-
                        state model over one owner per GitHub surface: issues,
                        labels, comments, pull requests, reviews, checks, and
                        audit events
  agents/               the agent-CLI subprocess layer: shared dispatch and its
                        result models, credential filtering, session parsing,
                        the process registry, and one module per backend
  scheduler/            the `IssueScheduler` every tick shares and the typed
                        submissions it takes
  workflow/             the state machine: the label vocabularies and the
                        transition guard, the `engine/` owners one tick is
                        composed of, the `late_split/` domain a late generation
                        is recorded and reported by, and nine stage subpackages
                        holding the twelve labelled handlers the dispatch
                        routes into
  git/                  local git: execution, locks, and authenticated
                        transport, under the worktree lifecycle, the per-tick
                        base sync, branch publication, verify runs, the
                        added-line measurement of a committed candidate, and
                        the immutable ref namespace one is preserved under
                        when a split supersedes it
  observability/        the four surfaces that watch a run without steering
                        it: the analytics sink and everything downstream of
                        it, the parser that meters one finished agent run, the
                        Streamlit page over the operator's Postgres target,
                        and the file-backed trajectory viewer beside it
  apps/                 the two Streamlit pages a `streamlit run` names; the
                        polling loop is launched at cli.py instead
  skills/               the two skill-enumeration owners: the per-tick repo
                        catalog and the per-run local discovery it reads its
                        marker back off
```

Five rules hold for the tree as a whole, each with a check under `tests/repository/` that finds its subjects on disk so
a module added anywhere is covered the day it lands. The root is the three files above plus the ten packages under
them, held to that exact inventory: a module parked beside them would be importable next to the package that owns the
responsibility, and both would answer. No module wears one of the retired domain families as a prefix. Every family is
forbidden in the private spelling its compatibility leaves carried (`_dashboard_read_core.py`), and the families whose
word names a domain package and nothing else — `dashboard_`, `workflow_`, `git_`, `state_machine` — in the public
spelling as well, so `workflow_state.py` fails one level down exactly as it would at the root. A word that also names a
responsibility *inside* a package keeps its public spelling: `charts/usage_axis.py` and `usage/trajectory_models.py`
are owners under the family's own package rather than that family flattened out of it. Nothing is named for an
inventory of names either — `exports.py`, `manifest.py`, `compatibility.py`, whole or as the tail of a prefixed one,
the decomposer's output manifest excepted — and nothing carries a `.pyi` stub or a module-level `__getattr__` /
`__dir__`: a re-export is the owner's own object bound at import, so a lookup lands on the module that defines the name
rather than on something answering for it.

Imports run one way through four layers — `config/` at the bottom, the domains that do the work above it, `workflow/`
deciding with them, and `cli.py` / `__main__.py` / `runtime/` / `apps/` composing the lot. The direction is read
twice, because deferring an import weakens where it lands but not whether it belongs. At module scope, where an import
decides what a package costs to load and whether it can be loaded at all, nothing points up but `workflow/state.py` —
named exactly, and only by the two layers its labels type, `github/` and `git/`. Over every scope, the only reaches
left are declared one by one in `tests/repository/test_layering.py`: three base-sync owners posting a notice or a park
through the workflow's comment and guard owners, deferred to a call because at module scope they would be a cycle. An
undeclared hop fails wherever it is written, and a declared one fails if it is bound at module scope after all. The
launch forms compose each other and are reached from nothing below them at any scope, and no import anywhere is
relative, because a relative target names its module by position and no layer can be read off it. And a package either
publishes an explicit `__all__` of its owners' own objects, with nothing else of the package's own left in its
namespace beside it, or fronts nothing and imports nothing at all — the submodules on a marker package are what other
modules' imports planted there, not what its initializer loaded, so naming the package costs no owner behind it. That
second half is read from the initializer's source, because the namespace cannot tell an eager sibling import from
somebody else's; what an initializer imports from outside the package for its own use is a helper rather than a
surface, and is held to neither. The eight that publish are listed under
[`configuration/operations.md#continuous-integration`](configuration/operations.md#continuous-integration), where each
is also a scoped lint waiver.

The test tree mirrors this one, and two more checks hold it there: every package above has a mirrored tests package,
and every directory the suite collects from carries an initializer of its own, with nothing at the tests root but the
suite-wide fixtures. The mirror is why the same short module name recurs once per domain — one `test_imports.py` per
package — and those initializers are what keep the recurrences distinct at collection.

## Workflow labels

An issue should have at most one workflow label at a time; where a pre-namespace spelling survives beside the
namespaced one, the namespaced label is the state and the bare one is outranked rather than counting as a second. The
names are part of the public contract because live GitHub issues already carry them. The `workflow:` prefix marks the
states the orchestrator writes itself, so a repository's own vocabulary cannot collide with them; the five a human
also applies or reads — `in_review`, `question`, `discussion`, and the `done` / `rejected` terminals — keep the bare
spelling, as do the `backlog` and `paused` control labels an operator types. Membership in `WorkflowLabel` /
`ControlLabel` is what closes both sets rather than the prefix, so a `workflow:`-prefixed name outside them —
Dependabot's service labels on its own update PRs — is not a state, routes nowhere, and survives a label write
untouched.

The namespace is a GitHub label spelling and stops at that boundary, which is the distinction the stage map in
[`architecture/workflow-modules.md`](architecture/workflow-modules.md) reads by: a bare tag there names the *stage* —
the handler, the subpackage under `orchestrator/workflow/stages/` holding it, and the identifier analytics rows,
audit event payloads, and agent-session attribution have always carried — while the wire label an issue carries is
spelled `workflow:<tag>`. `workflow/state.py` owns both directions: `stage_name` strips the prefix for those sinks,
and `label_for_name` resolves either spelling back to its member.

The whole set, what each state means, and the control-label semantics are in
[`state-machine/labels-and-state.md#workflow-labels`](state-machine/labels-and-state.md#workflow-labels). The startup
bootstrap that migrates a repository off the pre-namespace spellings, and the three reads that still take a bare
label wherever that rename could not run, are in
[`state-machine/labels-and-state.md`](state-machine/labels-and-state.md#legacy-labels-and-the-migration-off-them).

## Process model

There is **only one long-lived process**: `python -m orchestrator`. It is wrapped by `run.sh` so the loop can
self-exit and be restarted with new code.

- **Trigger**: started manually (or by a wrapper). Optional `--once` for a single tick.
- **Tick cadence**: every `POLL_INTERVAL` seconds (default 60).
- **Self-restart guard** (`runtime.self_update.self_modifying_merge_happened`): each tick fetches
  `origin/<ORCHESTRATOR_BASE_BRANCH>` (default `main`); if it advanced past the process's startup SHA *and* the new
  commits touch `orchestrator/`, the loop
  exits 0 so the wrapper can re-exec the new code. The branch is decoupled from `BASE_BRANCH` so a target repo with a
  different default branch does not interfere with self-update detection.
- **Self-update resilience** (`run.sh self_update`): before each launch — at startup and after every
  self-modifying-merge restart — the wrapper fast-forwards the orchestrator checkout to
  `origin/<ORCHESTRATOR_BASE_BRANCH>`. It skips the pull and warns to stderr if a non-base branch is checked out, and
  warns and continues (rather than exiting) if the fast-forward fails (diverged base branch, rebase in progress, network
  error); either way it launches the existing working tree. A clean fast-forward still updates the tree before launch,
  so the self-modifying-merge flow keeps picking up new code. This is deliberate: under the production systemd unit
  (`Restart=always`) exiting on a self-update failure silently crash-loops the service with the orchestrator never
  running, so a stale-but-running process plus a journal warning is preferred — the warning is the operator's signal
  to restore the checkout.
- **Signals**: SIGINT/SIGTERM set a flag and call `scheduler.shutdown(wait=False)` synchronously so the submit path is
  closed mid-tick; the loop then stops at the next tick boundary and drains. The drain terminates in-flight agent and
  verify subprocess groups up front (`agents.terminate_all_running`) so a worker parked in a long agent / verify run
  unwinds in seconds instead of holding the process for up to `AGENT_TIMEOUT`. A daemon watchdog backstops the drain: if
  it overruns, the watchdog terminates those same groups and hard-exits (`os._exit(128+signum)`) so total signal→exit
  stays within `SHUTDOWN_GRACE_SECONDS` no matter what a thread is blocked on. A second Ctrl+C hits the re-armed kernel
  default handler and kills immediately.

The coding agent runs as a **transient child subprocess**, not a daemon — spawned per tick when work is needed.

## Per-tick flow (`workflow.tick`)

Each tick the polling loop fans `workflow.tick(gh, spec, scheduler=...)` out across **every configured repo** via
`runtime.ticks.run_tick`: single-repo deployments stay in-thread, multi-repo deployments use a `ThreadPoolExecutor`
sized to the repo count. A single long-lived `IssueScheduler` (global cap `MAX_PARALLEL_ISSUES_GLOBAL`, per-repo cap
`MAX_PARALLEL_ISSUES_PER_REPO`) is shared across all `tick` calls, so those caps bound the whole deployment rather
than one repo's thread.

One repo's pass is owned by `workflow/engine/tick.py` — the base refresh, the community-contribution PR sweep, the
skill-catalog emission, and then either the scheduler handoff or the in-tick sequential / bounded-parallel loop, in
that order. The refresh runs first because every step after it reads what that fetch left behind, and the sweep and
the catalog emission both precede the scheduler / in-tick split so each fires exactly once per tick on either path.
The dispatch behind that split folds every family-aware issue (`workflow:decomposing` / `workflow:blocked` /
`workflow:umbrella` / unlabeled — the labels that write cross-issue parent ↔ child state) into ONE bucket submit per
repo that drains sequentially on a single worker, so a stale child cannot starve the parent umbrella issue, and
submits everything else one callable per issue.

Per-issue durable state lives in a single **pinned comment** on the issue (`<!--orchestrator-state {...json...}-->`).
The orchestrator process is stateless; the label and the pinned JSON are the entire dispatch input.

For the full per-tick sequence — eligible-issue enumeration, the cap exemption a no-agent bucket earns, what the base
refresh rebases and pushes, the read-only skip the `question` and `discussion` labels take, the per-tick
external-merge sweeps, and the complete pinned-state JSON schema — see
[`state-machine/labels-and-state.md#per-tick-flow-workflowtick`](state-machine/labels-and-state.md#per-tick-flow-workflowtick).

## Stage handlers

Twelve workflow labels dispatch to a `_handle_<label>` function under `orchestrator/workflow/stages/`, spread over
nine stage subpackages: `decomposition/` answers for `workflow:decomposing`, `workflow:ready`, `workflow:blocked`,
and `workflow:umbrella`, while `implementing`, `documenting`, `validating`, `in_review`, `fixing`, `conflicts`,
`question`, and `discussion` answer for one label each. The thirteenth dispatch target is the unlabeled entry, which
`workflow/engine/pickup.py` answers rather than a stage package; the `done` / `rejected` terminals are no-ops the
dispatch leaves alone. Each subpackage is a set of responsibility-named owners with nothing flat beside them,
and the dispatcher reaches a handler by importing the module its label is paired with in `_STAGE_HANDLER_TARGETS` and
reading the handler off it — as `pickup.py` does for the stage it starts an issue on — so a patch that has to
intercept a dispatched handler targets that module. A stage-to-stage call is named the same way: the decomposition
disabled-rollout and `ready` paths name `stages/implementing/handler.py` for `_handle_implementing`.

Most stage handlers run the user-content drift hook (`_compute_user_content_hash` → `_detect_user_content_change`) so
an out-of-band human edit re-routes the issue back to `workflow:decomposing` (when no dev session exists yet), resumes
the locked dev session with the updated body (implementing, validating, in_review, resolving_conflict), or unwinds
back to `workflow:validating` without resuming dev (documenting). Both halves of that hook sit on the
`workflow/engine/drift.py` owner the stage leaves import directly, so a patch aimed at the hook targets that owner.
`_handle_fixing`, `_handle_question`, and `_handle_discussion` skip the drift hook.

Which owner holds each decision inside a stage, which helpers it borrows from a sibling stage or from `git/`, and the
checks that keep a stage answering on one module are in
[`architecture/workflow-modules.md`](architecture/workflow-modules.md). For per-stage internal flow — pickup, drift
handling, decomposing, ready, blocked, umbrella, implementing, documenting, validating, in_review, fixing, and
resolving_conflict — see [`state-machine/delivery-stages.md`](state-machine/delivery-stages.md), with the two
operator-applied conversation stages in
[`state-machine/conversation-stages.md`](state-machine/conversation-stages.md) and the drift hook's filters and
per-handler routing in
[`state-machine/delivery-stages.md#user-content-drift-detection`](state-machine/delivery-stages.md#user-content-drift-detection).

## Agent subprocess (`agents.run_agent`)

`run_agent(backend, prompt, cwd, ...)` dispatches to the per-backend runner (`codex.run_codex` /
`claude.run_claude`); `backend` is one of `"codex"` / `"claude"` and is re-validated at call time so a
misuse fails loudly. Both runners return a unified
`AgentResult(session_id, last_message, exit_code, timed_out, stdout, stderr, interrupted, usage)`. `interrupted`
(default `False`) flags a run the runner observed exiting on SIGTERM/SIGKILL — the shape the orchestrator's
shutdown sweep (`terminate_all_running`) produces when it kills an in-flight agent group — and is distinct
from `timed_out` (the orchestrator's own `AGENT_TIMEOUT` firing). `usage` (default `None`) is the parsed
`UsageMetrics` -- the one on `observability/usage/metrics.py` -- that `recording.record_agent_exit` attaches during a
tracked run so callers can read token / cost metrics off the result without re-parsing stdout; it stays `None` for a
result that never flowed through
`_run_agent_tracked` or whose usage parse failed (fail-open). What the spawning handlers do with it — the per-issue
counters `_accumulate_issue_usage` folds each run into, and the one visible receipt comment
`_format_issue_usage_verdict` reads them back out of at a terminal — is in
[`state-machine/labels-and-state.md#pinned-state`][pinned-state] and
[`observability/usage.md`](observability/usage.md); nothing gates on the figure. `CodexResult` is kept as a
transitional alias.

The role command specs (`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT`), their parsing, the durable per-session
lock, and the resume mechanic are documented in
[`workflow/command-specs.md`](workflow/command-specs.md). Which stage spawns which role is in
[`workflow/roles.md`](workflow/roles.md). What follows is the subprocess shape only.

- **Codex command**:
  `codex exec [-C cwd | resume <sid>] --dangerously-bypass-approvals-and-sandbox --json -o <tempfile> <prompt>`. The
  `-o` path is a per-spawn `tempfile.mkstemp` outside the worktree (so target repos without `.codex-*` in `.gitignore`
  don't see it as untracked); `last_message` is read from it and the tempfile is cleaned up on any exit path by a
  per-spawn context manager (`codex.codex_last_message_file`).
- **Claude command**:
  `claude -p --dangerously-skip-permissions --output-format stream-json --include-partial-messages --verbose <prompt>`
  (with `--resume <sid>` when resuming). `last_message` is parsed from the stream-json: prefers the terminal
  `{"type":"result","result":...}` event (honored regardless of how the run ended), falls back to the last
  `assistant`/`message` text content for schema-drift forward-compat. The fallback is gated to clean, completed runs
  (`exit_code == 0`, not timed out, not interrupted); an interrupted or non-zero run with no terminal `result` event
  exposes an empty `last_message` rather than a partial transcript chunk.
- **Input**: prompt string; optional resume session id; timeout (`AGENT_TIMEOUT` / `REVIEW_TIMEOUT`).
- **Output**: `AgentResult(...)`. `session_id` is harvested by walking the JSONL events for any UUID-shaped value at
  `session_id` / `conversation_id` / etc. (shared between both backends).
- **Timeout cleanup** (`processes.terminate_process_group`): on timeout expiry the runner SIGTERMs the agent's whole
  process group (every spawn uses `start_new_session=True`), waits for the leader, then — mirroring the shutdown sweep
  (`terminate_all_running`) — probes the group with `killpg(_, 0)` and SIGKILLs any surviving descendant. Without the
  probe a build grandchild the agent forked (Maven, gradle, a JVM test runner) could keep mutating the worktree after
  the timeout was recorded — the failure mode that stranded a late clean commit behind the implementing-stage
  `agent_timeout` park.

### Environment filtering (`agents.environment.filter_agent_env`)

The agent subprocess env is filtered to keep host secrets and the orchestrator's own GitHub credentials out of agent
reach. The same filter runs for the verify-command runner (with `allow_provider_auth=False`, which also strips provider
keys).

- **GitHub-token-bearing env vars** are stripped (`GITHUB_TOKEN`, `GH_TOKEN`, etc. — the `_FORBIDDEN_AGENT_ENV`
  exact-match set) so a prompt-injected agent cannot push or call the GitHub API.
- **Production-secret-shaped env vars** are stripped by name shape: anything matching `_AGENT_SECRET_SUFFIXES`
  (`_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD`, `_PAT`, `_CREDENTIAL`) or the bare-name set (`TOKEN`, `KEY`, `SECRET`,
  `PASSWORD`, `PAT`, `CREDENTIAL`). Without this a `STRIPE_API_KEY` / `DATABASE_PASSWORD` set on the host would ride
  into a sandbox-bypassed agent or into the operator-configured verify shell.
- **Credential-file locators** are stripped too (`*_TOKEN_FILE`, `*_KEY_FILE`, `*_SECRET_FILE`, `*_PASSWORD_FILE`,
  `*_CREDENTIAL_FILE`, `*_CREDENTIALS`, `*_CREDENTIALS_FILE`, plus bare `TOKEN_FILE` / `CREDENTIALS` /
  `CREDENTIALS_FILE`). The most important case is `ORCHESTRATOR_TOKEN_FILE`, the orchestrator's own write-credential
  locator.
- **Write-credential locators** (`_AGENT_WRITE_CREDENTIAL_LOCATORS`: `SSH_AUTH_SOCK`, `SSH_ASKPASS`, `GIT_ASKPASS`,
  `GIT_SSH_COMMAND`) are stripped by exact name. The orchestrator's own push path constructs its own `GIT_ASKPASS`
  tempfile.
- **Provider auth** required to reach the agent's own model is allowlisted by exact name in
  `_AGENT_PROVIDER_AUTH_ALLOWLIST` (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
  `OPENAI_API_KEY`) for agent subprocesses only. The verify runner passes `allow_provider_auth=False` and strips them
  too — a verify shell executes untrusted agent-produced code, and the verify-failure park comment publishes the
  offending command verbatim. Advanced deployments (Bedrock, Vertex, custom proxies) extend the allowlist explicitly.
- **`GIT_AUTHOR_*` / `GIT_COMMITTER_*`** are injected from `AGENT_GIT_NAME` / `AGENT_GIT_EMAIL` (default
  `agent-orchestrator <agent-orchestrator@users.noreply.github.com>`) so agent commits are stamped with the
  orchestrator's identity regardless of the host's `~/.gitconfig`.

### Hardened local git (`git.commands._git_hardened`)

Every local git operation inside a worktree the agent can write to runs through this envelope: the `-c` overrides
that neutralize `core.hooksPath` / `core.fsmonitor` / `credential.helper` / commit signing, `GIT_CONFIG_GLOBAL` and
`GIT_CONFIG_SYSTEM` detached from `~/.gitconfig` and `/etc/gitconfig`, and the orchestrator's committer identity.

Git's own output is decoded with `surrogateescape` rather than strictly, for the same class of reason. A repository
path is bytes, and a committed file whose name is not valid UTF-8 makes a strict decode raise inside `subprocess`
before any caller sees a return code -- taking the tick out where the probe should have reported the extra path and
parked the artifact it invalidates.

It also turns object replacement off — `GIT_NO_REPLACE_OBJECTS=1` for `refs/replace/<oid>` and
`GIT_GRAFT_FILE=/dev/null` for the graft file. Neither of those is config, so nothing above reaches them, and each is
writable by an agent whose linked worktree shares the clone's refs and git dir. Left on, they change what git says a
commit's tree and parents ARE without changing the commit anyone named: a decomposer could stand a synthetic commit
carrying its code in for the base and have the plan check measure against that, while the push — which names the real
SHA — carries the code as well. Both are disabled for every hardened read, so a probe and the push it gates are
talking about the same objects.

Two more sit on the working-tree operations themselves rather than in the envelope, because neither is config either.
`core.worktree` in a linked worktree's own `config.worktree` — which an agent
enables by writing `extensions.worktreeConfig` into the clone it shares — points every path operation at any directory
it likes, and a `-c core.worktree=` override does NOT win against it, so the tree is named with `--work-tree` instead:
by `verification/probes._worktree_status`, which would otherwise report on a clean shadow checkout, and by the
`reset --hard` in `worktrees/creation._move_branch_onto`, which would otherwise report success and move the ref while
writing the reviewed commit's files into that other directory -- leaving the issue's checkout on the plan it had, the
handoff baseline naming a tip the tree is not on, and whatever was in the redirected directory overwritten.
Both build the flag through `commands._work_tree_arg`, which spells the path absolutely: every command carrying it
runs with its cwd set to the worktree, and git resolves a relative `--work-tree` against THAT cwd — so a relative
`WORKTREES_DIR` would name a directory beneath the worktree, and git would refuse to run the command at all.
And `assume-unchanged` / `skip-worktree` are bits on an index entry: git honours them by not comparing the file, so a
tracked path the agent rewrote reports clean. Those entries are read separately (`git ls-files -v`) and answered as
paths AND as a withheld `readable`, so a caller refusing on what git listed and one that has to prove the tree empty
both fail closed on them.

One more class sits on the individual call, and for the opposite reason: these ARE settings git reads, but it reads
them from the environment, where a `-c` on the command line does not win. `_git_hardened` therefore takes an
`env_extra` a caller applies over both the process environment and the hardening above, and the added-line
measurement (`git/measurement/additions.py`) is what spends it — `GIT_ATTR_SOURCE` pinned to the candidate commit, so
the `.gitattributes` the count honours are the ones inside what is being measured rather than whatever the agent left
uncommitted in the checkout (an inherited value beats the `attr.tree` config a `-c` could set, and a `* -diff` planted
either way makes a textual candidate measure as zero), plus `GIT_ATTR_NOSYSTEM=1` so the host's own attributes stay
out of a number two hosts have to agree on. On the argv beside them the same reading pins what config CAN answer for:
`core.attributesFile` at `/dev/null`, `core.bigFileThreshold` at git's default, `--diff-algorithm=myers`,
`--no-renames`, `--no-relative`, and `--ignore-submodules=none` — the algorithm because git's algorithms pair a change
with repeated lines in it differently and disagree by whole lines, which would let a `git config` beside the work
retune the size ceiling from below it.

What no pin reaches is refused rather than reported on. `$GIT_DIR/info/attributes` is a file rather than a setting, so
nothing on the command line overrides it and it outranks every attribute source; a diff driver declared in the shared
repository's own config turns a path binary the moment any attribute assigns it. Both are writable from the agent's
worktree, so a measurement that finds either records a typed `diff_unpinnable` failure instead of a count — and it
inspects that path without opening it (`lstat`, no link followed, anything but a regular file refused), since a FIFO
or a `/dev/zero` symlink planted there would otherwise block or exhaust the tick that read it.

## Push path (`git.authentication._push_branch`)

The orchestrator (not the agent) pushes. The push is hardened against the agent-controlled worktree:

- Token delivered via `GIT_ASKPASS` tempfile, never argv.
- Detaches from `~/.gitconfig` and `/etc/gitconfig` (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`).
- Disables `core.hooksPath`, `credential.helper`, `core.fsmonitor`.
- Refuses to push if the config the push resolves — the worktree's local config plus any `include.path` file or
  per-worktree `config.worktree` it pulls in, with global/system detached — carries any `url.*.insteadOf` /
  `pushInsteadOf` rewrite or any `http.*` proxy/TLS setting (e.g. `http.proxy`, `http.sslVerify=false`) that could
  tunnel the token-bearing push through an attacker proxy or disable certificate verification. Env-var proxies
  (`https_proxy`) are operator-set and stay honored — only agent-writable config-file transport is rejected.
- Pushes via an explicit refspec (no upstream stored): `HEAD:refs/heads/<branch>` by default, or
  `<revision>:refs/heads/<branch>` when the caller names the commit it means. A caller that decided to push by
  inspecting a commit — the discussion stage's plan publication — names it, because `HEAD` between the reading and
  the push is not necessarily the commit that was checked.

## Observability

Three independent JSONL sinks plus an optional Postgres aggregation target watch a run without steering it: an opt-in
audit event log, a project-local analytics sink, an opt-in (default-off) trajectory sink that `record_agent_exit`
fills with redacted, head/tail-truncated per-run reasoning trajectories, and the operator-deployed database the
analytics sink is replayed into. Two Streamlit pages read them: the analytics dashboard over that database, and the
file-backed trajectory viewer, which reads its JSONL directly (usage and cost included) and needs no database at all.
None of them feed back into dispatch — workflow correctness keys off the pinned state JSON and the workflow label —
so every surface is observation-only and safe to truncate, rotate, or delete.

They are not all owned in one place. The audit sink belongs to `github/events.py`, since `GitHubClient.emit_event` is
the single chokepoint it is appended from, and the Postgres service is the operator's own `analytics-db/` deployment.
Everything else — the analytics sink and what is downstream of it, the trajectory writers, the usage parser, and both
read models — lives under `orchestrator/observability/`, whose own four owner areas are mapped together with the two
`apps/` pages composed over them in
[`architecture/observability-modules.md`](architecture/observability-modules.md).

[`observability.md`](observability.md) is the map over the surfaces themselves, and each has a reference page behind
it: the two JSONL sinks in [`observability/event-streams.md`](observability/event-streams.md), the trajectory sink
and its viewer in [`observability/trajectories.md`](observability/trajectories.md), the compose layout and sync CLI
in [`observability/analytics-database.md`](observability/analytics-database.md), the read model and dashboard wiring
in [`observability/analytics-dashboard.md`](observability/analytics-dashboard.md), and the usage parser's
cost-precedence rules in [`observability/usage.md`](observability/usage.md).

## Summary of "what runs when"

- **`cli.main` polling loop** — long-lived Python process. Trigger: manual start (or wrapper). Cadence: every
  `POLL_INTERVAL`s.
- **`workflow.tick(gh, spec)`** — function call. Trigger: each loop iteration. Cadence: once per tick per configured
  `RepoSpec`; multi-repo fans out across a `ThreadPoolExecutor`, single-repo stays in-thread.
- **`_refresh_base_and_worktrees(gh, spec)`** — function call. Trigger: start of each `workflow.tick`. Cadence: once
  per tick per repo: one `git fetch <spec.remote_name> <spec.base_branch>`, then per-worktree dispatch — a pre-PR
  worktree rebases locally, and a PR-having one behind base is rebased and pushed in the refresh itself.
- **`_handle_*` per issue** — function call. Trigger: the issue's workflow label. Cadence: once per tick per pollable
  issue; concurrent up to `spec.parallel_limit` per repo and `MAX_PARALLEL_ISSUES_GLOBAL` across all repos. No-agent
  family buckets (`workflow:blocked` / `workflow:umbrella`) are cap-exempt.
- **decomposer agent (`DECOMPOSE_AGENT`)** — subprocess (fresh or resumed). Trigger: `_handle_decomposing` (retry
  budget OK) or HITL resume. Cadence: one shot per tick when needed. The same role spec also backs both conversation
  stages, which pin their own agent and session keys rather than a decomposing one.
- **implementer agent (`DEV_AGENT`)** — subprocess. Trigger: `_handle_implementing` (no commits yet, retry budget OK)
  or HITL resume. Cadence: one shot per tick when needed.
- **reviewer agent (`REVIEW_AGENT`)** — subprocess (fresh session). Trigger: `_handle_validating`, round < max.
  Cadence: one shot per tick.
- **dev-fix agent** — subprocess (resumed dev session). Trigger: a reviewer `CHANGES_REQUESTED` verdict (dispatched
  from `_handle_validating` after the relabel to `workflow:fixing`) or fresh in_review PR feedback (dispatched from
  `_handle_fixing` after the quiet window) — both run with `stage="fixing"` and bounce back to `workflow:validating`
  for re-review. Cadence: one shot per tick.
- **dev-conflict agent** — subprocess (resumed dev session). Trigger: `_handle_resolving_conflict` and `git rebase`
  left conflicts. Cadence: one shot per tick.
- **question / discussion agent (`DECOMPOSE_AGENT` backend)** — subprocess. Trigger: `_handle_question` or
  `_handle_discussion` on the conversation's opening round or on a trusted human reply to a parked one. Cadence: one
  shot per tick when needed. `question` is read-only for the whole conversation; `discussion` is read-only until a
  human confirms the design on the thread, and from there is allowed exactly one commit of
  `plans/issue-<number>.md` for the stage to publish. Neither ever spawns a developer or reviewer.
- **`git push`** — subprocess. Trigger: after dev produces clean commits, or after a discussion round commits the
  confirmed plan and the branch reads as exactly that one file. Cadence: per fix; per discussion, once the humans
  have confirmed the design.
- **self-restart check** — git fetch + diff. Trigger: start of each tick. Cadence: every tick.

## Architecture schema

```
                     ┌──────────────────────────────────────┐
                     │   GitHub repo(s) (REPO or REPOS)     │
                     │   ─ issues (with workflow labels)    │
                     │   ─ pinned state comment per issue   │
                     │   ─ branches / PRs                   │
                     └──────────────┬───────────────────────┘
                                    │ PyGithub (one token per slug)
                                    │
   ┌────────────────────────────────┴─────────────────────────────────────┐
   │  orchestrator process  (python -m orchestrator)                      │
   │  ───────────────────────────────────────────────────                 │
   │   cli.main over orchestrator/runtime/                                │
   │     startup: build per-spec [(spec, GitHubClient), ...] from         │
   │              config.default_repo_specs(); ensure_workflow_labels;    │
   │              build one shared IssueScheduler(global_cap, per_repo)   │
   │     loop every POLL_INTERVAL s:                                      │
   │       1. self-restart check (origin/<ORCHESTRATOR_BASE_BRANCH>       │
   │          moved & touches orchestrator/?)                             │
   │       2. run_tick(state, clients, scheduler):                        │
   │            N == 1 → in-thread workflow.tick(gh, spec, scheduler)     │
   │            N  > 1 → ThreadPoolExecutor fans workflow.tick across     │
   │                     one worker thread per repo                       │
   │       3. scheduler.reap()  (drain completions; surface failures)     │
   │       4. retention.prune_with_retention_logging()                    │
   │     shutdown: scheduler.shutdown(wait=True) drains workers on        │
   │               --once / self-restart; a signal stop first kills       │
   │               in-flight agent+verify groups, and a watchdog          │
   │               hard-exits within SHUTDOWN_GRACE_SECONDS on overrun    │
   │                    │                                                 │
   │                    ▼                                                 │
   │   workflow.tick(gh, spec, scheduler) →                               │
   │     _refresh_base_and_worktrees(gh, spec, scheduler): skip           │
   │       worktrees whose handler is still in flight in scheduler        │
   │     classify each pollable issue and submit to scheduler:            │
   │       family-aware (`workflow:decomposing` / `workflow:blocked` /    │
   │         `workflow:umbrella` / unlabeled) →                           │
   │         ONE bucket submit per repo that drains sequentially          │
   │         (cap-exempt when every family issue is                       │
   │         `workflow:blocked` or `workflow:umbrella`)                   │
   │       fan-out (everything else) →                                    │
   │         one submit per issue, concurrent up to per-repo / global     │
   │         caps                                                         │
   │     scheduler rejects duplicate active / cap hit / family-slot       │
   │       conflict → skipped this tick AND logged with reason            │
   │     accepted workers call gh._for_worker_thread() + refetch the      │
   │       Issue, then run _process_issue → dispatch by label             │
   │                                                                      │
   └─────────┬───────────────────────────────────────┬────────────────────┘
             │ subprocess                            │ subprocess (hardened)
             ▼                                       ▼
   ┌─────────────────────────────┐         ┌─────────────────────────────┐
   │  coding-agent CLI           │         │  git push                   │
   │  (codex or claude,          │         │  ─ GIT_ASKPASS tempfile     │
   │   per-issue worktree)       │         │  ─ no global/system config  │
   │  ─ env: GH tokens stripped  │         │  ─ hooks/helper disabled    │
   │  ─ env: GIT_AUTHOR/COMMITTER│         │  ─ refuses url/http cfg     │
   │     stamped (orchestrator)  │         └──────────────┬──────────────┘
   │  ─ provider auth left alone │                        │
   │  ─ --bypass / --skip perms  │                        │
   │  ─ JSONL → session_id       │                        │
   │  ─ last_message: -o (codex) │                        │
   │     or stream-json (claude) │                        │
   └──────────────┬──────────────┘                        │
                  │ commits to                            │ pushes branch to
                  ▼                                       ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  git worktree:  <WORKTREES_DIR>/<owner>__<name>/issue-<n>           │
   │  branch:        orchestrator/<owner>__<name>/issue-<n>              │
   │  ─ slug subdir + slug-namespaced branch keep two repos sharing a    │
   │    target_root from colliding on the same `orchestrator/issue-<n>`  │
   │  ─ created from <spec.remote_name>/<spec.base_branch>               │
   │    in spec.target_root                                              │
   │    (or reused if has unpushed commits)                              │
   └─────────────────────────────────────────────────────────────────────┘
```

## State transition (label lifecycle)

The compact label-lifecycle diagram for every forward, fix-loop, terminal, and HITL-park transition lives in
[`state-machine/lifecycle.md`](state-machine/lifecycle.md).

[pinned-state]: state-machine/labels-and-state.md#pinned-state
