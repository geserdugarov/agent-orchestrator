# Documentation

This directory is the reference set behind [`../README.md`](../README.md). The README is the operator's end-to-end
guide — install, configure, run, and the labels you drive an issue with. Everything here is the layer under it: why the
system is shaped this way, which module owns what, what every setting does, and what each stage handler reads and
writes. GitHub renders this page when you open the `docs/` directory, so it is also the index you land on from there.

Six areas cover the whole system. Each has a landing page directly under `docs/` and — security aside — a directory of
focused pages beside it.

## Where to start

| If you want to… | Read |
|---|---|
| install, configure, and run it | [`../README.md`](../README.md), then [`configuration.md`](configuration.md) |
| understand the design before changing it | [`architecture.md`](architecture.md) |
| know what a label means, or when it moves | [`state-machine.md`](state-machine.md) |
| know which agent a stage spawns, under what prompt | [`workflow.md`](workflow.md) |
| find a setting, or apply an edited `.env` | [`configuration.md`](configuration.md) |
| see what the orchestrator did, and what it cost | [`observability.md`](observability.md) |
| harden the deployment | [`security.md`](security.md) |
| change the code | [`../AGENTS.md`](../AGENTS.md), then the [`develop` skill](../.agents/skills/develop/SKILL.md) |

## Architecture

[`architecture.md`](architecture.md) — the system overview: the design constraints behind a stateless process, the
top-level module map, the process model, the agent subprocess and the environment boundary around it, the hardened git
and push path, and the schema diagram.

- [`architecture/platform-modules.md`](architecture/platform-modules.md) — the package root, runtime, config, GitHub,
  git, agents, scheduler, and skills owners, the boundaries the suite enforces between them, and the split inside
  `git/`.
- [`architecture/workflow-modules.md`](architecture/workflow-modules.md) — the `workflow` package API, the engine
  owners under it, and the per-stage subpackages.
- [`architecture/observability-modules.md`](architecture/observability-modules.md) — the analytics, usage, dashboard,
  and trajectory-viewer owners and the two `streamlit run` targets over them, mapped at the package boundary.

## Workflow state machine

[`state-machine.md`](state-machine.md) — the label-based machine that drives an issue from pickup to terminal. Label
spellings and pinned-state JSON keys are a compatibility contract, so this area is authoritative for both.

- [`state-machine/labels-and-state.md`](state-machine/labels-and-state.md) — the label set and control labels, the
  typed states and the transition guard, the migration off the pre-namespace spellings, what one tick reads and writes,
  and every pinned-state key.
- [`state-machine/delivery-stages.md`](state-machine/delivery-stages.md) — pickup, drift detection, decomposition, and
  the dev / reviewer / docs loop through `in_review`, `workflow:fixing`, and `workflow:resolving_conflict`.
- [`state-machine/conversation-stages.md`](state-machine/conversation-stages.md) — the operator-applied `question` and
  `discussion` handlers, including the plan PR a confirmed design earns.
- [`state-machine/lifecycle.md`](state-machine/lifecycle.md) — the compact label-lifecycle diagram.

## Workflow roles and command specs

[`workflow.md`](workflow.md) — who runs an issue: which stage invokes which agent role, what that role's prompt grants
and forbids, and how a role's command spec is parsed and pinned.

- [`workflow/roles.md`](workflow/roles.md) — the three roles, the stages that spawn each, the session-reuse rules, the
  late adjudication an oversized committed candidate earns under the decomposer's own role, the owner read that
  adjudication passes before anything acts on it and what each verdict earns past it, the snapshot-first order a
  cleared `split` creates its children in, what a human editing the
  issue or answering under it changes while that candidate is frozen, and the local verify gate that is a stage step
  rather than a role.
- [`workflow/conversations.md`](workflow/conversations.md) — the `question` and `discussion` prompt contracts, what a
  round may leave behind, and the tracked-repository awareness block.
- [`workflow/command-specs.md`](workflow/command-specs.md) — the spec grammar, backend selection, worked examples, and
  the in-flight session lock.

## Configuration

[`configuration.md`](configuration.md) — the setting-by-setting reference: every knob, its default, and what it
changes. [`../.env.example`](../.env.example) holds the basic parameters for a first run and
[`../.env.example.advanced`](../.env.example.advanced) the common advanced overrides; both stay terse and link here.

- [`configuration/observability.md`](configuration/observability.md) — the sink paths and retention windows, the
  analytics database URL, skill-trigger tracking, the dashboard read mode, and the dashboard quickstart.
- [`configuration/operations.md`](configuration/operations.md) — continuous integration, run modes, the systemd user
  service, and what an edited `.env` takes to apply.
- [`configuration/snapshot-capability-check.md`](configuration/snapshot-capability-check.md) — the
  disposable-repository check that proves a production token and its rulesets can create, fetch, verify, and delete
  the late split's snapshot refs, and what each failure means.

## Observability

[`observability.md`](observability.md) — the map over every observation-only surface. None of them is read by the
polling tick, so all are safe to truncate, rotate, or delete.

- [`observability/event-streams.md`](observability/event-streams.md) — the audit event log (`EVENT_LOG_PATH`) and the
  analytics sink (`ANALYTICS_LOG_PATH`): record envelopes, every event kind, and the retention prune.
- [`observability/trajectories.md`](observability/trajectories.md) — the opt-in trajectory sink
  (`TRAJECTORY_LOG_PATH`), the operator workflow that mirrors and prunes it, and the file-backed viewer over it.
- [`observability/analytics-database.md`](observability/analytics-database.md) — the operator-deployed Postgres
  service the sink is replayed into: compose layout, endpoint, schema, and the sync CLI.
- [`observability/analytics-dashboard.md`](observability/analytics-dashboard.md) — the read model over those tables
  and the Streamlit page composed on it, down to every empty and error state.
- [`observability/usage.md`](observability/usage.md) — the decoder for agent CLI JSONL stdout that produces the token
  and cost detail the other surfaces carry.

## Security

[`security.md`](security.md) — the project security checklist mapped to this repo: what the repo files enforce, what
is operator-owned in GitHub or org settings, the comment trust boundary, pinned-state authentication, and the
cross-repo awareness disclosure.

This area is one page on purpose. It is already focused — a checklist plus the controls no file in the repo can set —
so it keeps no directory beside it, and is not split for symmetry with the five areas above.

## Landing pages and deep links

The pages directly under `docs/` are the stable addresses. Write a link — from the README, from `AGENTS.md`, from an
issue, from a commit message — against one of those rather than against a page inside an area directory: a landing
page is where the area's overview lives and stays put, while the pages beside it are split further as the area grows.

A landing page is not a table of contents. Each keeps a section for every part of its area, and where that part has
grown a page of its own the section stays behind as a summary naming the owner. That is what keeps a deep link written
before a split working:
[`state-machine.md#_handle_fixing-label-workflowfixing`](state-machine.md#_handle_fixing-label-workflowfixing) still
resolves, and lands on the summary pointing at
[`state-machine/delivery-stages.md`](state-machine/delivery-stages.md).

Every relative path and `#anchor` on these pages, in [`../README.md`](../README.md), in [`../AGENTS.md`](../AGENTS.md),
and in the skill files is checked by `tests/repository/test_doc_links.py`, so a renamed heading or a moved page fails
the suite instead of rotting quietly. Notes under `plans/` are human working material rather than part of this set,
and are left out of that scan.
