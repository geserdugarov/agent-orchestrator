# Skill-Trigger Tracking — Design

## Context

The orchestrator drives `claude` (and `codex`) CLI agents in per-issue
worktrees. Claude Code agents can pull in *skills*. The skills relevant
here are the repo-local agent skills this project already ships — the
canonical source files are `.agents/skills/<name>/SKILL.md` (`develop`
for implementer/fixing runs, `review` for the reviewer), with
`.claude/skills/<name>/SKILL.md` as symlinks to them so both the
`.agents/` and `.claude/` conventions resolve to the same content (the
same `AGENTS.md` ↔ [`../CLAUDE.md`](../CLAUDE.md) mirroring this repo
already uses). These are distinct from Claude Code's *internal* skill
catalog (built-in capabilities a `claude` session may also offer); this
design tracks whatever skill name the agent triggers, repo-local or
built-in, and does not assume the two sets are disjoint. A repo-local
skill is prompt-injected project guidance: the implementer is told to run
the pre-push checklist from `develop`, the reviewer to apply the `review`
defect list. Their value is entirely conditional on the agent actually
*triggering* them.

Today nothing records whether that happened. The orchestrator decodes the
claude stream for tokens and cost
([`../orchestrator/usage.py`](../orchestrator/usage.py)) and emits one
`agent_exit` analytics record per run
([`../docs/observability.md`](../docs/observability.md)), but the
skill-invocation signal in that same stream is discarded. An operator
debugging "why did the implementer ignore the commit convention" or
"is the `review` skill ever firing" has no observable answer, and no way
to correlate skill use with review-round count or cost.

This document designs a narrow, observation-only system to track skill
triggering. The design issue that picked this up landed the design doc
only; **Sequencing step 1 (the extractor + `agent_exit` fields + opt-in
switch) and step 3 (the dashboard widget) have since shipped** — see
[Status](#status) for what is now in the tree and what remains a
follow-up or a capture task.

## Status

Sequencing step 1 landed across three commits: `parse_claude_skills` /
`parse_codex_skills` / `parse_agent_skills` plus the `SkillTriggers`
dataclass in `orchestrator/usage.py` (`a3e2c0a`); the
`record_agent_exit` wiring, the `TRACK_SKILL_TRIGGERS` opt-in switch, and
the `agent_exit` fields in `orchestrator/analytics/__init__.py`
(`ec6f1df`); and the `docs/configuration.md` / `docs/observability.md`
updates (`ec6f1df`, `1a7d607`). Unit tests cover both backends over
synthetic skill-bearing and skill-free streams.

One narrow defect was fixed during the post-landing review: the claude
*triggered* extractor counted every `assistant` frame, so a `Skill` block
that persists across the cumulative partial snapshots one message emits
under `--include-partial-messages` was over-counted in `trigger_counts`.
`parse_claude_skills` now groups by `message.id` and keeps the final
snapshot per id — the same last-frame-wins discipline `parse_claude_usage`
already applies for exactly this reason — so a single trigger counts once.

Steps 2 and 3 **have both since shipped**. Step 2 (the audit
`skill_triggered` event): `_run_agent_tracked` now emits one event per
distinct triggered skill, gated on the same `TRACK_SKILL_TRIGGERS` switch
and reusing the list `record_agent_exit` parses (see [Audit event
log](#3-audit-event-log)). Step 3 (the dashboard widget):
`analytics.read.get_skill_trigger_rates` plus the "Skill trigger rates"
panel in `orchestrator/dashboard.py`, a pure read-side addition over the
`extras JSONB` fields with no DDL. The offered-skills set and the codex
event shape remain best-effort capture tasks (Open questions). The
remaining design sections describe the shipped behavior.

### Live data after the switch was turned on

The operator has flipped `TRACK_SKILL_TRIGGERS=on` in production, so the
sink has now *incidentally* captured real skill-bearing records (these are
live `agent_exit` rows, **not** Step-0 test fixtures — no captured
`*.jsonl` fixtures exist in the tree, so Sequencing step 0 is still
outstanding). The signal so far, all from 2026-06-23:

| backend | role / stage             | runs (switch on) | skill-bearing            |
| ------- | ------------------------ | ---------------- | ------------------------ |
| claude  | developer / implementing | 9                | 3 → `['develop']`, count 1 |
| codex   | reviewer / validating    | 5                | 0                        |
| codex   | decomposer               | 2                | 0                        |

What this confirms empirically:

- The **claude triggered path is validated on real streams** (3 genuine
  `develop` hits). Across ~2160 `agent_exit` records the extractor logged
  zero crashes and clean output — the low-noise evidence the switch
  default wants before any flip.
- The **codex extractor has captured nothing** across 5 real reviewer
  runs and 2 decomposer runs — the default reviewer's `review` skill is
  going unobserved in production today (see the codex Open question).
- `skills_available` has **never appeared in a single record** — the
  offered set is confirmed uncaptured live, not just unconfirmed in
  theory (see the claude offered-skills Open question).
- The partial-frame fix has **no live reproduction**: all 3 claude
  records were captured on `main` *before* the fix and already read
  count 1, so `tool_use`-block duplication across partial snapshots has
  not been observed directly — only the usage sub-object is known to
  repeat. The fix is still correct (defensive, and consistent with
  `parse_claude_usage`, whose docstring confirms claude emits multiple
  `type:"assistant"` frames per `message.id`), and the synthetic test is
  its direct evidence; a Step-0 capture would confirm whether `tool_use`
  blocks duplicate in the same way the usage block does.

## What "triggered" means on the wire

The orchestrator already runs claude with
`--output-format stream-json --include-partial-messages`
([`../orchestrator/agents.py`](../orchestrator/agents.py)), and
`usage.py` already iterates that stream's events. A skill invocation
surfaces in it as a tool-use content block inside an `assistant`
message:

```json
{"type": "assistant", "message": {"id": "msg_…", "content": [
  {"type": "tool_use", "name": "Skill", "input": {"skill": "develop", "args": "…"}}
]}}
```

The same `assistant` frames `parse_claude_usage` groups by `message.id`
carry these blocks; the parser walks past them today because it only
reads the `usage` sub-object. This `tool_use`-block path is the
well-grounded, confident signal for the *triggered* set on the claude
backend.

The *offered* set is weaker. An earlier draft asserted a session-init
frame (`type: "system"`, `subtype: "init"`) enumerates the skills
offered to the agent, but Claude Code's headless docs
(<https://code.claude.com/docs/en/headless>) describe the `system/init`
metadata as model / tools / MCP / plugins — **not** a dedicated
offered-skills list. No captured sample in this repo confirms a clean
enumeration. So `skills_available` is **best-effort**: skills may surface
indirectly (e.g. a `Skill` entry in the init `tools` array, or under
plugin metadata) or not at all, and the exact field/path must be
confirmed against a real captured stream before it is relied on (Open
questions). The *triggered* set does not depend on it.

**Both backends are in scope, not just claude.** `REVIEW_AGENT` defaults
to `codex` ([`../orchestrator/config.py`](../orchestrator/config.py),
[`../docs/workflow.md`](../docs/workflow.md)), so the reviewer — the role
that triggers the `review` skill — runs on codex in a default install.
The repo-local skills are agent-agnostic by design: their canonical home
is `.agents/skills/<name>/`, readable by codex as well as claude. Scoping
codex out would therefore leave the single most common skill-trigger case
(the default reviewer) permanently unobserved. Codex's CLI in this
workspace exposes skill-related features, and codex emits a
`codex exec --json` event stream that `parse_codex_usage` already
consumes; the codex skill extractor parses that same stream. The exact
codex event shape for a skill invocation is not yet captured here, so the
codex side ships **best-effort** with the residual gap called out
explicitly (Open questions) rather than asserted to be empty.

## Goal / non-goals

**Goal.** Record, per tracked agent run, which skills the agent
triggered (ordered, de-duplicated, with a count), wired into the
existing analytics `agent_exit` record. The signal is gated behind a
dedicated opt-in switch that **defaults off**, so a default install's
`agent_exit` records are byte-for-byte identical to today's — note this
is *not* automatic from the analytics sink alone, which is itself
default-on (`ANALYTICS_LOG_PATH` defaults to `LOG_DIR/analytics.jsonl`),
so the guarantee comes from the switch default, not from the sink being
off (see [Configuration](#5-configuration--opt-in-switch)).

**Non-goals.**

- *Enforcement.* This system observes; it never blocks, retries, or
  re-prompts a run for failing to trigger a skill. Required-skill gating
  is policy, not observability — out of scope (see Rejected).
- *Prompt changes.* We do not change how agents are told to use skills.
- *Skill argument capture.* Only the skill *name* is recorded (see
  Privacy).
- *Confirmed `skills_available` source.* The offered-skills field is
  best-effort, not a guarantee; pinning its exact stream source (claude
  and codex) is a capture task, not part of this design's commitments
  (see Open questions). The *triggered* set is the firm deliverable.

## Gap, precisely

1. `usage.py` decodes the claude stream for tokens/cost but drops every
   `tool_use` block, so the skill signal is parsed-over, not absent.
2. The `agent_exit` analytics record built in
   `analytics.record_agent_exit`
   ([`../orchestrator/analytics/__init__.py`](../orchestrator/analytics/__init__.py))
   has fields for backend, model, tokens, cost, duration, and
   review/retry context — but no skill field.
3. Nothing in the audit event log
   ([`EVENT_LOG_PATH`](../docs/observability.md#audit-event-log-event_log_path))
   marks skill use either.

So the work is one extractor plus one record field plus one wiring line,
not a new subsystem.

## Design

### 1. Skill extractor (`orchestrator/usage.py` sibling)

Add a pure-Python extractor next to the usage parsers, mirroring their
two-parser-plus-dispatcher shape and resilience contract:

- `parse_claude_skills(stdout) -> SkillTriggers` walks `assistant`
  frames, collecting `tool_use` blocks whose `name == "Skill"` and
  reading `input.skill` (the firm *triggered* signal). As shipped it
  reads **only** that triggered set and leaves `available` empty — it does
  not inspect the `system`/`init` frame, because no captured stream has
  confirmed an offered-skills field there yet (Open questions). Pinning
  that source stays a pure capture task; the parser never raises on its
  absence.
- `parse_codex_skills(stdout) -> SkillTriggers` walks the same
  `codex exec --json` event stream `parse_codex_usage` consumes and
  collects skill-invocation events (best-effort: the precise codex event
  shape is a capture task, so v1 may extract nothing and that gap is
  documented, not hidden). Codex is **not** short-circuited to empty —
  the reviewer runs here by default.
- `parse_agent_skills(backend, stdout) -> SkillTriggers` dispatches by
  backend exactly as `parse_agent_usage` does (`claude` →
  `parse_claude_skills`, `codex` → `parse_codex_skills`).
- `SkillTriggers` is a small frozen dataclass:
  `triggered` (skill names, first-seen order, de-duplicated),
  `trigger_counts` (name → count, for repeated invocations), and
  `available` (best-effort offered-skills set; empty when unconfirmed or
  absent — forward-compatible with stream schema drift).
- **Resilience parity.** Malformed JSONL lines are skipped silently, the
  same contract `usage.py` already documents; a missing/renamed field
  yields an empty result, never an exception.

Keeping this in `usage.py` (rather than a new module) reuses the
existing `_iter_events` line decoder and keeps the stream-schema
knowledge — the one thing most likely to drift — in a single file.

### 2. Record schema

Fold the signal into the existing `agent_exit` analytics record rather
than minting a new event kind. **Drop rule.** `analytics.build_record`
drops only extras whose value is exactly `None` — it does *not* drop an
empty list, empty dict, or `0`
([`../orchestrator/analytics/__init__.py`](../orchestrator/analytics/__init__.py)).
The rule is therefore applied *per field, on that field's own value* —
never "drop all skill fields whenever nothing triggered."
`record_agent_exit` passes `None` (never `[]` / `{}` / `0`) for any
individual field that is empty/absent, so build_record drops exactly that
key. This is deliberate: the three fields vary independently, so *when*
the best-effort offered set is captured, a run that was offered skills but
triggered none records `skills_available=[...]` while `skills_triggered` /
`_count` drop out — that asymmetry is what gives the "offered but not
used" vs "never available" signal the
[What "triggered" means](#what-triggered-means-on-the-wire) section
describes. Until the offered-set source is confirmed,
`skills_available` is simply always `None` and the distinction degrades
gracefully to "triggered / not triggered" without breaking the record
shape. Added fields:

- `skills_triggered` — list of skill names, first-seen order; `None`
  (and thus dropped) when nothing fired. The firm, well-grounded field.
- `skills_triggered_count` — total trigger count (sum over
  `trigger_counts`), so a run that invokes `develop` three times is
  distinguishable from one clean trigger; `None` when nothing fired.
- `skills_available` — **best-effort** offered-skills list; recorded only
  when the extractor positively captured an offered set (see Open
  questions for the unconfirmed source), and `None` otherwise — when the
  source is absent / uncaptured, the backend exposes no offered set, or
  the switch is off.

A fully shape-stable record (all three fields dropped, no new keys) is
thus the switch-off case and any run — codex or claude — whose stream
surfaced no skill triggers and no capturable offered set; it is *not*
every run that merely triggered nothing while skills were on offer.

The Postgres path needs **zero DDL**: `analytics_events.extras JSONB`
([`../docs/observability.md`](../docs/observability.md#schema)) already
absorbs fields the schema does not name explicitly, so an
operator-deployed database ingests the new fields the day the parser
ships.

### 3. Audit event log

The audit log (`EVENT_LOG_PATH`) carries the per-skill granularity
surface for per-trigger forensics: **as shipped** (step 2),
`_run_agent_tracked` emits one `skill_triggered` audit event per distinct
skill through `GitHubClient.emit_event`, carrying `agent`, `agent_role`,
`review_round`, `retry_count`, and `skill`. The wiring wrinkle the first
cut deferred — the audit `agent_exit` event is emitted from
`_run_agent_tracked` (the `emit_event` call site), while the skill parse
lands one call deeper in `record_agent_exit` — is resolved by the first of
the two options this section floated: `record_agent_exit` **returns** the
de-duplicated first-seen triggered list it already parsed, and
`_run_agent_tracked` loops over it to emit. That reuses the single parse
(no second pass over stdout), keeps the analytics layer free of any
`GitHubClient` dependency, and inherits the gate for free — the switch off
yields an empty/`None` list, so no events fire. The emission rides its own
fail-open `try/except` (log-and-continue), mirroring the skill parse's
guard, so an opt-in bug can never disturb the baseline `agent_spawn` /
`agent_exit` events that already fired. The `agent_exit` analytics rollup
still ships alongside it: one record per run answers the headline
questions and avoids leaning on the per-event stream for aggregate counts;
the audit events add the per-invocation ordering on top.

### 4. Integration point

`analytics.record_agent_exit`
([`../orchestrator/analytics/__init__.py`](../orchestrator/analytics/__init__.py))
is the analytics boundary that already calls `parse_agent_usage` after
every tracked run and appends the `agent_exit` record under a single
try/except. `workflow._run_agent_tracked` only *delegates* to it
([`../orchestrator/workflow.py`](../orchestrator/workflow.py)) — it
passes the `AgentResult` through and does no parsing — so the skill
extraction belongs at the same boundary as the usage parse, not in the
workflow facade. Add a sibling `parse_agent_skills(backend, result.stdout)`
call inside `record_agent_exit`, gate it on the opt-in switch (below),
and merge its fields into the same record dict. No new chokepoint, no new
call site, no second pass over a fresh subprocess — `result.stdout` is
already in memory and already parsed once there.

**Fail-open is mandatory — do NOT reuse the existing guard.** The
current `record_agent_exit` wraps `parse_agent_usage` in a try/except
that `return`s on failure, i.e. it *skips the entire record*. The skill
parse must therefore **not** ride inside that same guard: an opt-in
skill-parser bug would otherwise drop the baseline usage/cost
`agent_exit` record that ships today — a regression on existing behavior
for an opt-in feature. Wrap `parse_agent_skills` and the field merge in
their *own inner* try/except that, on any exception, logs via
`log.exception` and falls through with the skill fields left unset
(`None`, so build_record drops them). The baseline
`append_record(build_record(... usage / cost ...))` then runs
unconditionally afterward. A skill-parse failure thus degrades to "an
`agent_exit` record without skill fields," never a missing record, and
can no more stall the per-issue tick than the usage parse can.

### 5. Configuration / opt-in switch

Add one boolean switch — `TRACK_SKILL_TRIGGERS` (**default off**), parsed
in the analytics package alongside its siblings (`ANALYTICS_LOG_PATH`
etc.). The extractor is skipped entirely when the switch is off, so a
default install pays zero extra parse cost and its `agent_exit` records
gain no new fields. The switch defaults off *because* the analytics sink
is itself default-on (`ANALYTICS_LOG_PATH` defaults to
`LOG_DIR/analytics.jsonl`, see
[`../docs/configuration.md`](../docs/configuration.md)): if the switch
defaulted on, every default install would silently start emitting skill
fields, breaking the "absent opt-in → today's records" guarantee. An
operator turns it on to start collecting; turning it back off restores
the prior record shape immediately. Flip the default to on only after the
field has proven low-noise in practice (Open
questions). Document it in
[`../docs/configuration.md`](../docs/configuration.md) and
[`../docs/observability.md`](../docs/observability.md) in the same PR
that ships the parser.

## Privacy / trust

- **Names only.** `tool_use.input` for the `Skill` tool can carry an
  `args` string that may echo issue or user content. The extractor reads
  only `input.skill` (the name) and never `input.args`. This matches the
  established stance that the analytics sink stores usage/cost, never
  prompts, stdout, or worktree contents.
- **Non-secret surface.** Repo-local skill names live in committed
  `.agents/skills/*/SKILL.md` frontmatter (surfaced under `.claude/` via
  symlink); recording them discloses nothing an operator cannot already
  read in the repo. A built-in Claude Code skill name is likewise just a
  capability label, not secret.

## Cost

- **Zero new dependencies.** stdlib `json` only; the extractor reuses the
  `usage.py` event iterator.
- **One extra walk** over an stdout string already held in memory after
  the run — negligible next to the agent subprocess wall-clock, and it
  runs for both backends (codex included, since the reviewer is codex by
  default).
- **Inert paths stay inert.** Analytics-disabled hosts and
  `TRACK_SKILL_TRIGGERS=off` add zero work and zero record bytes. (Codex
  is *not* an inert path — it carries the default reviewer's skills.)

## Considered but rejected

- **A dedicated skill-trigger JSONL sink.** Redundant with the analytics
  sink, which already runs per agent run through one chokepoint and feeds
  the Postgres/dashboard pipeline. A second file would duplicate the
  rotation, retention, and dedup machinery for one field group.
- **A `skill_triggered` audit event per invocation as the v1 surface.**
  Multi-skill runs would multiply event volume for a signal the
  `agent_exit` rollup already carries, so it was kept out of the first cut
  and documented as a follow-up. That follow-up (step 2) has since shipped
  *alongside* the rollup, not in place of it, and stays gated off by
  default — so the volume concern only applies to operators who opt in.
- **Required-skill enforcement / gating.** Blocking or re-prompting a run
  that skips `develop` is workflow policy, not observability, and would
  couple a correctness decision to a best-effort stream parse. The
  observation-only invariant ([the tick never reads these
  sinks](../docs/observability.md)) is load-bearing; keep it.
- **Scoping codex out entirely.** Rejected: `REVIEW_AGENT` defaults to
  `codex`, so codex carries the default reviewer's `review`-skill
  triggers — the most common case, not a corner. Codex is covered
  best-effort with its capture gap tracked, never silently dropped.
- **Asserting a confident `system/init` offered-skills list.** Rejected
  as unverified: the headless docs describe that frame as
  model / tools / MCP / plugins, and no captured sample confirms a clean
  skills enumeration. `skills_available` is best-effort instead.
- **Capturing `input.args`.** Adds a user-content exfiltration surface
  for no analytics value (see Privacy).

## Open questions

Status as of the step-1 landing (see [Status](#status)): the two capture
tasks are **still open** — step 1 shipped grounded on synthetic streams,
not captured ones, so the parsers extract from plausible-but-unconfirmed
shapes — while the switch-default is **settled for v1**, the audit
`skill_triggered` event has since **shipped** (step 2), and the dashboard
remains an **explicitly deferred** follow-up. The
[live data](#live-data-after-the-switch-was-turned-on) gathered since the
operator turned the switch on now confirms both capture-task gaps
empirically (codex captured nothing; `skills_available` never appeared)
and makes the **codex capture the priority** — that is where the default
reviewer's signal is lost in production today.

- **Claude offered-skills source (capture task — still open).** The exact
  stream-json location of the offered set is unconfirmed — the headless
  docs frame `system/init` as model / tools / MCP / plugins, not a skills
  list. As shipped, `parse_claude_skills` returns an empty `available`
  set and `record_agent_exit` therefore drops `skills_available`
  entirely; the *triggered* set does not depend on it. Live data backs
  keeping it dropped: `skills_available` has appeared in **zero** records
  since the switch went on, so the empty-best-effort handling is right and
  `skills_triggered` standing alone is the correct call today. This is
  capturable now — the orchestrator already runs claude in `implementing`
  with `develop` on offer, so the only blocker is that raw stdout is not
  persisted; a one-off manual capture of a real
  `claude --output-format stream-json` run resolves it and pins whether
  the offered set is derivable (e.g. a `Skill` entry in the init `tools`
  array) before relying on `skills_available`. If it is not cleanly
  derivable, `skills_triggered` stands alone as it does today.
- **Codex skill event shape (capture task — still open; the HEADLINE
  gap).** This is the single most important open question, not a residual
  one: `REVIEW_AGENT=codex` makes the reviewer the most common skill case,
  and live data shows it is effectively non-functional. Across the **5
  codex reviewer runs** (plus 2 decomposer runs) after the switch went on,
  **0 captured a skill trigger** — the default reviewer's `review` skill
  is going unobserved in production today. The shipped `parse_codex_skills`
  is best-effort: it scans for a `Skill`-named tool/function call or a
  `*skill*`-typed event and returns empty on a real stream whose shape
  differs. A raw `codex exec --json` capture of a reviewer run is needed to
  tell whether the reviewer simply does not trigger `review` or the guessed
  event shape does not match the real one. When that capture lands, point
  the parser at the confirmed field and guard against the same
  per-invocation double-count the claude path was fixed for — codex emits
  started/completed events for one call. This is the next step to
  prioritize: it is where the default reviewer's signal is lost today.
- **Switch default (settled for v1; keep off).** Shipped **off** so
  default installs are unchanged. Live data is reassuring on noise — the
  operator has run with the switch on across ~2160 `agent_exit` records
  with zero crashes and clean output, exactly the "low-noise in practice"
  evidence a flip would want. But do **not** flip to default-on yet: the
  noise is low partly *because codex emits nothing*, so promoting the
  default now would advertise a feature that silently covers only the
  claude backend. Keep it off until codex coverage exists (the headline
  gap above); revisit then.
- **Audit-event follow-up (implemented — step 2).** The per-invocation
  audit `skill_triggered` event has shipped, reusing the list
  `record_agent_exit` parses and gated on the same switch. Live data still
  says the *signal* it adds is thin — all 3 real triggers so far are single
  (`count 1`, one skill), so per-invocation ordering is moot in today's
  data — but the wiring is cheap, fail-open, and inert when the switch is
  off, so it ships now rather than waiting for a multi-skill run to prove
  the need.
- **Dashboard surface (implemented — step 3).** The
  skill-trigger-rate-per-role widget landed as a pure read-side change
  over the `extras JSONB` fields: `analytics.read.get_skill_trigger_rates`
  aggregates per `(agent_role, backend)` — `runs`, `skill_runs` (rows
  carrying a `skills_triggered` key), and `total_triggers` — and
  `orchestrator/dashboard.py` renders the "Skill trigger rates" panel as
  an HTML table (no Plotly builder, no DDL). Because the field is only
  written when tracking is on and a skill fired, a `0%` rate reads as
  "no trigger observed" and the panel captions the `TRACK_SKILL_TRIGGERS`
  switch when nothing has fired in the window.

## Sequencing

0. **Stream capture (prerequisite — not done).** Capture real `claude`
   and `codex` stream samples (skill-bearing and skill-free) to pin the
   trigger event shape per backend and resolve the two capture-task open
   questions above. Step 1 shipped *ahead* of this, grounded on synthetic
   frames; the captures remain outstanding and keep `skills_available` and
   the codex extractor best-effort. Prioritize the **codex reviewer
   capture** — live data (above) shows 0/5 codex reviewer runs captured a
   trigger, so that is where the default reviewer's signal is lost today;
   the claude offered-set capture is the secondary piece and is doable as
   a one-off manual run since `develop` is already on offer in
   `implementing`.
1. **Extractor + `agent_exit` fields + opt-in switch — LANDED**
   (`a3e2c0a`, `ec6f1df`, `1a7d607`): `parse_claude_skills` /
   `parse_codex_skills` / `parse_agent_skills` in `usage.py`, the
   `analytics.record_agent_exit` wiring (gated on the switch, fail-open
   inner try/except, passing `None` for empty skill fields), the
   `TRACK_SKILL_TRIGGERS` knob, unit tests over synthetic skill-bearing
   claude *and* codex streams plus a skill-free stream per backend, and
   the `docs/observability.md` / `docs/configuration.md` updates. No DDL —
   `extras JSONB` ingests the fields immediately.
2. **Audit `skill_triggered` event — LANDED.** `record_agent_exit`
   returns the de-duplicated triggered list it parses, and
   `_run_agent_tracked` emits one `skill_triggered` audit event per
   distinct skill (`agent`, `agent_role`, `review_round`, `retry_count`,
   `skill`) under its own fail-open guard, gated on `TRACK_SKILL_TRIGGERS`.
   Focused tests cover the on / off / multi-skill / parse-failure paths;
   `docs/observability.md` documents the event.
3. **Dashboard widget — LANDED.** `get_skill_trigger_rates` in
   `analytics/read.py` plus the "Skill trigger rates" panel in
   `orchestrator/dashboard.py`, wired into the second read fan-out wave
   and covered by `tests/test_analytics_read.py` and
   `tests/test_dashboard.py`. A pure read-side addition over the
   `extras JSONB` fields — no DDL.

Steps 1, 2, and 3 have all landed; the feature is fully reversible by
clearing the switch.
