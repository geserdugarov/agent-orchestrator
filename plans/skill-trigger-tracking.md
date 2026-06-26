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

One narrow defect was addressed during the post-landing review and then
**superseded** by the Step-0 capture (described below). The original fix
assumed one message emits *cumulative* partial snapshots under
`--include-partial-messages` — so a `Skill` block would persist across
frames and be over-counted — and grouped by `message.id` keeping the final
snapshot per id (a last-frame-wins discipline borrowed from
`parse_claude_usage`). The capture **disproved** that assumption for
content: each completed content block lands in its own *non-cumulative*
`assistant` frame, so a `tool_use` block appears exactly once and
last-frame-wins could itself drop a real trigger (a `Skill` block followed
by a later block of the same message). `parse_claude_skills` now walks
every frame and de-duplicates by the `tool_use` block `id` instead — see
the claude-capture write-up two paragraphs down.

Steps 2 and 3 **have both since shipped**. Step 2 (the audit
`skill_triggered` event): `_run_agent_tracked` now emits one event per
distinct triggered skill, gated on the same `TRACK_SKILL_TRIGGERS` switch
and reusing the list `record_agent_exit` parses (see [Audit event
log](#3-audit-event-log)). Step 3 (the dashboard widget):
`analytics.read.get_skill_trigger_rates` plus the "Skill trigger rates"
panel in `orchestrator/dashboard.py`, a pure read-side addition over the
`extras JSONB` fields with no DDL. The codex *triggered* event shape has
since been **pinned** by a captured reviewer run (issue #513); the
remaining design sections describe the shipped behavior.

The **claude offered-skills capture has since landed** (the secondary
Step-0 task): a real `claude --output-format stream-json
--include-partial-messages` run confirmed the `system/init` frame carries
a dedicated top-level `skills` array, so `parse_claude_skills` now
populates `SkillTriggers.available` from it and `record_agent_exit` writes
`skills_available` for tracked claude runs (resolving the claude offered-
skills Open question). The same capture **disproved** the assumption
behind the claude *triggered* de-dup: under `--include-partial-messages`
the `assistant` content array is partitioned one completed block per
frame (a text block in its own frame, the following `Skill` block in the
next), **not** a cumulative snapshot that repeats earlier blocks the way
the `usage` sub-object does. Each `tool_use` block appears in exactly one
frame and carries a unique `id`, so the parser now walks every frame and
de-dups by that `id` rather than keeping the last frame per `message.id`
— last-frame-wins would have silently dropped a `Skill` block followed by
a later block of the same message. The **codex offered-set** remains a
best-effort capture task (Open questions); the codex *triggered* shape is
pinned (above).

### Live data after the switch was turned on

The operator has flipped `TRACK_SKILL_TRIGGERS=on` in production, so the
sink has now *incidentally* captured real skill-bearing records (these are
live `agent_exit` rows, **not** Step-0 test fixtures — no captured raw
`*.jsonl` files are committed to the tree). The **claude half of
Sequencing step 0 has since landed**: a real `claude` stream was captured
and inspected, resolving the offered-set and partial-frame questions, with
its findings encoded as inline synthetic test fixtures plus this write-up
rather than a committed raw stream (sanitized, names-only — see the
Privacy constraint). The **codex *triggered* capture has also landed**
(issue #513 pinned the file-open `SKILL.md` shape); only the **codex
offered-set** is still uncaptured. The signal so far, all from 2026-06-23:

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
- The **codex extractor captured nothing** across 5 real reviewer runs and
  2 decomposer runs — and issue #513's capture has since explained why: the
  old extractor matched a shape codex never emits. Codex *does* trigger the
  `review` skill (it opens the skill's `SKILL.md`); the parser now matches
  that file-open shape (see the resolved codex Open question).
- `skills_available` had **never appeared in a single record** at the
  time this table was gathered — the offered set was uncaptured live, not
  just unconfirmed in theory. That gap is now **closed on claude**: the
  Step-0 capture (below) pinned the `system/init.skills` source, so once a
  tracked claude run streams through the updated parser, `skills_available`
  is populated. The codex offered set is still uncaptured.
- The "partial-frame double-count" premise was **disproved by the
  capture, not just unreproduced.** All 3 live claude records were
  captured on `main` *before* the original fix and already read count 1,
  so block duplication was never observed live. The Step-0 capture shows
  why: claude emits one `assistant` frame **per completed content block**
  (the content array is *partitioned* across frames, not cumulative), so a
  `tool_use` block appears in exactly one frame — it does **not** repeat
  the way the `usage` sub-object does. The original last-frame-wins fix
  was therefore solving a non-problem and could itself drop a real trigger
  (a `Skill` block followed by a later text block in the same message);
  the parser now walks every frame and de-dups by the `tool_use` `id`,
  which counts each invocation once under the real partitioned framing and
  stays correct even if a future stream *does* repeat a block.

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

The *offered* set was the weaker signal — and a **capture has now
resolved it on claude.** An earlier draft asserted a session-init frame
(`type: "system"`, `subtype: "init"`) enumerates the offered skills; a
later revision walked that back because the headless docs
(<https://code.claude.com/docs/en/headless>) framed `system/init` as
model / tools / MCP / plugins, **not** a skills list. A real captured
`claude --output-format stream-json --include-partial-messages` run
(claude_code_version `2.1.191`, with this repo's `develop` / `review`
skills on offer) settles it: the `system/init` frame **does** carry a
dedicated top-level **`skills` array** — a flat list of the offered skill
names, repo-local and built-in alike — alongside the documented
`tools` / `mcp_servers` / `plugins` / `agents` keys. So
`parse_claude_skills` now reads `system/init.skills` into
`SkillTriggers.available` on claude (see [Status](#status) and the
resolved Open question). It stays defensive — a missing / renamed field
or non-string entry yields an empty set, never an error — and the
*triggered* set does not depend on it. On **codex** the offered set
remains best-effort/empty until a codex capture confirms its field.

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
consumes; the codex skill extractor parses that same stream. A captured
reviewer run (issue #513) has since **pinned** the codex shape: codex has
no dedicated `Skill` tool — it discovers a skill (under `$CODEX_HOME/skills/`
*or* the project-local `.agents/skills/`) and triggers it by opening that
skill's `SKILL.md`, which surfaces only as a `command_execution` item whose
shell command reads a `skills/<name>/SKILL.md` path. The extractor matches
that file-open shape (see [Codex skill event shape](#open-questions)).

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
- *Confirmed `skills_available` source (claude resolved; codex still a
  non-goal).* This design's *original* commitments treated the offered set
  as best-effort on both backends, pending a capture. The claude source has
  since been **confirmed and implemented** — `parse_claude_skills` reads
  `system/init.skills`, so `skills_available` is populated for claude (see
  [Status](#status) and Open questions). On **codex** the offered set stays
  best-effort/uncaptured and out of this design's commitments. The
  *triggered* set was, and remains, the firm cross-backend deliverable.

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

- `parse_claude_skills(stdout) -> SkillTriggers` walks **every**
  `assistant` frame, collecting `tool_use` blocks whose `name == "Skill"`
  and reading `input.skill` (the firm *triggered* signal), de-duplicating
  per invocation by the block `id`. It also reads the offered set from the
  `system`/`init` frame's `skills` array into `available` — now that a
  real capture has confirmed that source (see [What "triggered"
  means](#what-triggered-means-on-the-wire) and Open questions). Both
  reads are defensive: a missing / renamed field or non-string entry
  yields an empty result, never an exception.
- `parse_codex_skills(stdout) -> SkillTriggers` walks the same
  `codex exec --json` event stream `parse_codex_usage` consumes. Issue #513
  captured a real reviewer run and pinned the shape: codex has no `Skill`
  tool, so a trigger surfaces only as a `command_execution` item whose
  shell command opens a `skills/<name>/SKILL.md` file. The extractor reads
  only that `<name>` path segment (never the command text or its output),
  and dedups the started/completed pair codex emits per command by the
  shared `item.id` so one read counts once. The signal is heuristic — a
  SKILL.md opened for an unrelated reason would also register — but it is no
  longer a guess at an unobserved shape. Codex is **not** short-circuited to
  empty — the reviewer runs here by default.
- `parse_agent_skills(backend, stdout) -> SkillTriggers` dispatches by
  backend exactly as `parse_agent_usage` does (`claude` →
  `parse_claude_skills`, `codex` → `parse_codex_skills`).
- `SkillTriggers` is a small frozen dataclass:
  `triggered` (skill names, first-seen order, de-duplicated),
  `trigger_counts` (name → count, for repeated invocations), and
  `available` (offered-skills set; read from `system/init.skills` on
  claude, best-effort/empty on codex; empty when the frame/field is absent
  — forward-compatible with stream schema drift).
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
key. This is deliberate: the three fields vary independently, so a run
that was offered skills but triggered none records `skills_available=[...]`
while `skills_triggered` / `_count` drop out — that asymmetry is what gives
the "offered but not used" vs "never available" signal the
[What "triggered" means](#what-triggered-means-on-the-wire) section
describes. On claude that asymmetry is now **live** (where it was a future
prospect in the original design): the offered set is read from
`system/init.skills`, so an implementing run offered `develop` but
triggering nothing records `skills_available=[...]` while
`skills_triggered` / `_count` drop out. Where the source is absent — codex
today, or a claude stream missing the field — `skills_available` stays
`None` and the distinction degrades gracefully to "triggered / not
triggered" without breaking the record shape. Added fields:

- `skills_triggered` — list of skill names, first-seen order; `None`
  (and thus dropped) when nothing fired. The firm, well-grounded field.
- `skills_triggered_count` — total trigger count (sum over
  `trigger_counts`), so a run that invokes `develop` three times is
  distinguishable from one clean trigger; `None` when nothing fired.
- `skills_available` — offered-skills list; on claude read from
  `system/init.skills` (confirmed by capture), best-effort on codex.
  Recorded only when the extractor positively captured an offered set, and
  `None` otherwise — when the source is absent / uncaptured (codex today),
  the backend exposes no offered set, or the switch is off.

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
  triggers — the most common case, not a corner. Codex is covered — its
  `review`-trigger shape pinned by issue #513, its offered-set gap still
  tracked — never silently dropped.
- **Asserting a confident `system/init` offered-skills list (originally
  rejected — since reversed on claude by capture).** This was rejected as
  unverified while the headless docs described the frame as
  model / tools / MCP / plugins and no captured sample confirmed a skills
  enumeration. A real capture (claude_code_version `2.1.191`) has since
  shown the `system/init` frame *does* carry a dedicated `skills` array, so
  the parser now reads it on claude. The rejection stands only for **codex**
  (its offered-set field is still uncaptured), and the read stays
  defensive — an absent field yields empty, never an error.
- **Capturing `input.args`.** Adds a user-content exfiltration surface
  for no analytics value (see Privacy).

## Open questions

Status update (see [Status](#status)): **both original capture tasks are
now resolved.** The claude offered-skills source landed via a real
`claude` stream capture (`system/init.skills`), and the codex *triggered*
event shape landed via issue #513's reviewer capture (the file-open
`SKILL.md` shape). The switch-default was **revisited under issue #515**
once codex coverage landed and **stays off** (see the [Switch
default](#open-questions) item), the audit `skill_triggered` event has
**shipped** (step 2), and the dashboard has **shipped** (step 3). The
[live data](#live-data-after-the-switch-was-turned-on) gathered since the
operator turned the switch on confirmed both gaps empirically (codex
captured nothing; `skills_available` had never appeared) and drove the two
captures that closed them. The only residual best-effort piece is the
**codex offered-set**, still uncaptured.

- **Claude offered-skills source (capture task — RESOLVED).** A real
  `claude --output-format stream-json --include-partial-messages` run
  (claude_code_version `2.1.191`, this repo's `develop` / `review` skills
  on offer) confirmed the `system/init` frame carries a dedicated top-level
  **`skills` array** — a flat list of the offered skill names, repo-local
  and built-in alike — distinct from its documented `tools` / `mcp_servers`
  / `plugins` / `agents` keys. (The earlier doubt came from the headless
  docs, which enumerate the frame as model / tools / MCP / plugins and omit
  `skills`; the field is present in 2.1.x regardless.) `parse_claude_skills`
  now reads `system/init.skills` into `SkillTriggers.available`, so
  `record_agent_exit` writes `skills_available` for tracked claude runs,
  independently of the triggered set. The read is defensive — a missing /
  renamed field or non-string entry yields empty, never an error. The same
  capture also resolved the secondary "do `tool_use` blocks duplicate
  across partial snapshots?" question: they do **not** (the content array
  is partitioned one completed block per `assistant` frame, not cumulative
  like `usage`), so the claude *triggered* de-dup switched from
  last-frame-wins to `tool_use`-`id` de-dup (see [Status](#status) and the
  [What "triggered" means](#what-triggered-means-on-the-wire) section).
- **Codex skill event shape (capture task — RESOLVED, issue #513).** This
  was the headline gap: `REVIEW_AGENT=codex` makes the reviewer the most
  common skill case, yet **0** of the 5 codex reviewer runs (plus 2
  decomposer runs) after the switch went on captured a trigger. A real
  `codex exec --json` capture of a reviewer run resolved the ambiguity:
  codex **does** trigger the `review` skill — it discovers the skill (both
  the registered `$CODEX_HOME/skills/` root *and* the project-local
  `.agents/skills/` were observed) and opens its `SKILL.md` — but the old
  `parse_codex_skills` looked for the wrong shape (a `Skill`-named
  function/tool call or a `*skill*`-typed event, neither of which codex
  emits). The production 0/5 was therefore the parser, not the reviewer.
  The capture confirmed codex has no dedicated `Skill` tool: its file-based
  skill mechanism surfaces only as a `command_execution` item whose shell
  command reads a `skills/<name>/SKILL.md` path. `parse_codex_skills` now
  matches that shape, reads only the `<name>` segment (names-only Privacy),
  and dedups the started/completed pair codex emits per command by the
  shared `item.id` so one read counts once. The signal is heuristic — a
  SKILL.md opened for an unrelated reason (e.g. reviewing a PR that edits
  one) would also register — but it is observed, not guessed.
- **Switch default (revisited under #515 — keep off).** Shipped **off** so
  default installs are unchanged. Issue #515 performed the revisit the
  earlier "keep off until codex coverage exists" note deferred: it reran the
  decision once the codex stream-shape gap closed (issue #513 pinned the
  file-open shape, so the parser now covers both backends — the precondition
  #515 required before reconsidering). The revisit decision is to **keep it
  off**. Live data is reassuring on noise — the operator has run with the
  switch on across ~2160 `agent_exit` records with zero crashes and clean
  output, exactly the "low-noise in practice" evidence a flip would want —
  but that coverage has **not yet accumulated its own live data**: the ~2160
  records were captured against the *old* codex parser (which matched
  nothing, 0/5 reviewer runs), so the new file-open path's real-world noise —
  including the heuristic false-positive of a SKILL.md opened for an unrelated
  reason, on the default `REVIEW_AGENT=codex` reviewer that is the most common
  skill case — is still unmeasured. The codex offered-set behind
  `skills_available` also remains uncaptured (claude's is now pinned to
  `system/init.skills`). Flipping now would turn the default on **solely**
  because the claude path works and the codex parser exists in code, not
  because codex coverage has been shown low-noise in production — the exact
  outcome #515's acceptance criteria rule out. Keep it off until the pinned
  codex path proves low-noise on production data; revisit then.
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

0. **Stream capture (prerequisite — claude AND codex captures landed).**
   Capture real `claude` and `codex` stream samples (skill-bearing and
   skill-free) to pin the trigger event shape per backend and resolve the
   capture-task open questions above. Step 1 shipped *ahead* of this,
   grounded on synthetic frames; both captures have since landed. The
   **claude capture**: a real `claude --output-format stream-json
   --include-partial-messages` run with `develop` / `review` on offer
   confirmed the `system/init.skills` offered set (so `skills_available` is
   now populated for claude) and disproved the partial-snapshot
   block-duplication premise (so the claude *triggered* de-dup is now keyed
   on the `tool_use` `id`). The **codex reviewer capture** (issue #513)
   pinned the file-open shape (`command_execution` reading
   `skills/<name>/SKILL.md`) `parse_codex_skills` now matches and showed
   codex *does* trigger `review` — the 0/5 was the parser, not the reviewer.
   The only piece still uncaptured is the **codex offered-set**.
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
