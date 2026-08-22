# Event streams

The two JSONL sinks the polling tick writes as it drives issues: the **audit event log** (`EVENT_LOG_PATH`), an
opt-in record of workflow events, and the **analytics sink** (`ANALYTICS_LOG_PATH`), the raw metric records the
Postgres aggregation and the analytics dashboard over it are built from. Both are observation-only — no dispatch
decision reads either back, and both are safe to truncate, rotate, or delete at any time. The third sink, the opt-in
trajectory recorder, is documented in [`trajectories.md`](trajectories.md).

The record shapes below are a compatibility contract: an operator's `jq` filter, a `logrotate` rule, and the Postgres
`analytics_events` columns all key off them. Where the write path sits in the package tree is *not* repeated here —
[`architecture/observability-modules.md`](../architecture/observability-modules.md) maps it at the package boundary.
For the knobs themselves see [`configuration/observability.md`](../configuration/observability.md). The
parser the analytics `agent_exit` counts come from is on [`usage.md`](usage.md), and the database, read model, and
dashboard that sink is aggregated into afterwards are on [`analytics-database.md`](analytics-database.md) and
[`analytics-dashboard.md`](analytics-dashboard.md).

## Audit event log (`EVENT_LOG_PATH`)

Optional, opt-in JSONL sink. When `config.EVENT_LOG_PATH` is set, `github.events.write_event_record` appends one JSON
object per audit event to that file inside `GitHubClient.emit_event`; when unset (the default) the helper
short-circuits to a no-op. The fake `GitHubClient` in `tests/support/github/` calls the same helper.

**Schema.** Every record is built by `github.events.build_event_record` and carries `ts` (UTC ISO-8601 at second
precision), `repo` (the slug `owner/name`), `issue` (issue number, int), and `event` (the kind). `stage` is included
when the emitter passes one (effectively always today). Extras whose value is `None` are dropped. `json.dumps` is
called with `sort_keys=True` so on-disk order is stable across writers.

`stage` always carries the **bare stage tag** — `implementing`, `fixing`, `resolving_conflict` — and never the
`workflow:`-prefixed GitHub label the issue wears. Three shapes feed it here, and all three land on the tag:

- **The tracked agent spawn** takes it as a literal from the stage that calls `_run_agent_tracked` (`stage="validating"`
  in the reviewer owner, `stage="fixing"` in the fixing one), so `agent_spawn` / `agent_exit` read no label at all.
- **`stage_enter`** is handed the label `set_workflow_label` is applying, normalized through
  `workflow.state.stage_name` — the one emitter whose tag names where the issue is going rather than where it is.
- **The events that describe an issue where it already sits** — the park funnel, the terminals, the base-sync writers
  — normalize the label it currently carries the same way.

The namespace therefore stops at the GitHub boundary, so a grep or a dashboard filter matches on the tag. See
[`state-machine/labels-and-state.md#workflow-labels`](../state-machine/labels-and-state.md#workflow-labels) for the
two spellings and which labels have only one.

**Event kinds.** Every kind is emitted through the single `GitHubClient.emit_event` chokepoint, which also appends to a
capped in-memory tail (`recorded_events`, `_RECORDED_EVENTS_CAP = 500`) for tests and short-window debugging — the
file is the durable record.

- `stage_enter` — `set_workflow_label` (via `_emit_stage_enter`) for every label flip; extras: `stage`.
- `agent_spawn` / `agent_exit` — `_run_agent_tracked` (in `workflow/engine/usage.py`) wraps every `run_agent` call
  (decomposer, implementer, reviewer, dev-resume, conflict-resolution dev); extras: `agent` (backend), `agent_role`,
  `review_round`, `retry_count`. `session_id` and `agent_exit`-only fields are described below.
- `skill_triggered` — `_run_agent_tracked` after `agent_exit`, **only when `TRACK_SKILL_TRIGGERS` is on**
  (default off); one event per distinct skill the run triggered; extras: `agent` (backend), `agent_role`,
  `review_round`, `retry_count`, `skill` (the triggered skill name). Reuses the list `record_agent_exit` already parsed;
  off-switch installs emit none.
- `review_verdict` — `_handle_validating` after `_parse_review_verdict` reads the reviewer's last message; extras:
  `verdict` (`approved` / `changes_requested` / `unknown`), `review_round`, `pr_number`, `session_id`.
- `park_awaiting_human` — every `_park_awaiting_human` (in `workflow/engine/guards.py`) call site, plus
  `_on_question`, `_on_dirty_worktree`,
  `_park_verify_failure`, and the question- and discussion-stage `_park_question` / `_park_discussion` funnels;
  extras: `stage` (read from the current
  workflow label, not passed in), `reason` (e.g. `agent_timeout`, `push_failed`, `failed_checks`, `agent_question`,
  `agent_session_limit` (a quota-exhausted agent message, parked retryably as `agent_silent`), `dirty_worktree`,
  `reviewer_timeout`, `verify_failed` / `verify_timeout` / `verify_dirty` / `verify_head_changed`, `question_*`,
  `discussion_*`, ...).
- `pr_opened` — `_on_commits` after `gh.open_pr` succeeds; extras: `pr_number`, `branch`, `sha`, `retry_count`. The
  `discussion` stage's plan publication emits the same event with `stage="discussion"` when it opens (never when it
  reuses) a plan PR; it carries no `retry_count`, having no retry budget of its own.
- `pr_merged` — External merge terminal arcs in `_handle_in_review`, `_handle_fixing`, `_handle_resolving_conflict`;
  plus `_finalize_if_pr_merged` (in `workflow/engine/terminals.py`, which also owns those arcs) from
  `_handle_implementing` / `_handle_documenting` / `_handle_validating` entry checks
  and from the `_handle_blocked` / `_handle_umbrella` manually-closed child recovery; plus the `discussion` stage's
  plan-PR terminal (`workflow/stages/discussion/terminal.py`), which polls the recorded plan PR at handler entry and
  drains the same `_finalize_merged_pr` arc when the humans merged it; extras: `pr_number`, `sha`,
  `merge_method="external"`, `review_round`, `conflict_round`, `retry_count` — a plan PR carries none of those three
  counters, so its record reports `review_round: 0` and drops the other two with the rest of the null extras;
  `stage` names the stage the issue was in at finalize entry — spelled literally as `discussion` on that path, since
  the stage attributes its own runs rather than re-reading the label.
- `pr_closed_without_merge` — `_handle_in_review`, `_handle_fixing`, `_handle_resolving_conflict` when the PR is
  closed without merge; plus `_finalize_if_issue_closed` from `_handle_implementing` / `_handle_documenting` /
  `_handle_validating` entry checks (only when the linked PR is also closed; an open PR with a manually-closed issue is
  left alone); plus the same `discussion` plan-PR terminal with `stage="discussion"` when the humans closed the plan PR
  unmerged. Two discussion endings deliberately emit NOTHING: a manually closed issue whose plan PR is still open (the
  stage holds its terminal and keeps the label so the closed-issue sweep goes on yielding it), and a close before any
  plan PR exists (finalized `rejected`, with no pull request for the payload to name); extras: `pr_number`, `sha`,
  `review_round`, `conflict_round`, `retry_count`; `stage` names the stage the issue was in at finalize entry.
- `merge_attempt` — Every `git rebase origin/<base>` inside `_handle_resolving_conflict`; extras:
  `method="base_rebase"`, `result` (`success` / `failed` / `conflict`), `pr_number`, `sha`, `conflict_round`,
  `review_round`, `retry_count`.
- `conflict_round` — `_route_pr_worktree_to_resolving_conflict` emits `action="entered"` only when the refresh-time
  rebase actually leaves conflicted files (a merely-behind-base clean rebase no longer emits this);
  `_reconcile_parked_fixing` also emits `action="entered"` (with `stage="fixing"`) when a stuck validating-route
  transient `workflow:fixing` park is routed to `workflow:resolving_conflict` because its worktree is out of sync with
  the PR head (behind base, or an unpushed local rebase); every increment site (`_emit_conflict_round_incremented`)
  emits `action="incremented"` with `outcome`; extras: `pr_number`, `conflict_round`, `review_round`, `retry_count`,
  `outcome` (for increments), `sha`.
- `base_rebased` — `_sync_pr_worktree_to_base` after a clean refresh-time rebase + push that routes the issue from
  `workflow:validating` / `workflow:documenting` / `in_review` / `workflow:fixing` back to `workflow:validating`; also
  `_recover_pending_auto_base_rebase` when a crashed prior tick is finalized; extras: `pr_number`, `sha` (new head),
  `method` ∈ {`auto_clean_rebase`, `crash_recovery_pushed`, `crash_recovery_relabel_only`}, `review_round`
  (post-reset, so 0), `retry_count`; `stage` names the stage the issue was in when the rebase started.
- `late_measurement` / `late_verdict` / `late_failure` / `late_snapshot` / `late_cleanup` / `late_cancellation` /
  `late_restart` — the late size gate's seven families, each emitted by `workflow/late_split/telemetry.py` together
  with an identical analytics record; extras: the bounded correlation payload in
  [Late-split records](#late-split-records-both-sinks).

**`agent_spawn` / `agent_exit` extras.** On top of the shared fields:

- On `agent_spawn`, `session_id` is the resume session id and is OMITTED for fresh spawns (`resume_session_id=None` is
  dropped by `build_event_record`).
- On `agent_exit`, `session_id` is the result id from `AgentResult`. `agent_exit` additionally carries `duration_s`,
  `exit_code`, and `timed_out`.

**`skill_triggered` events (opt-in).** Gated behind `TRACK_SKILL_TRIGGERS` (default off; the same switch that adds the
[`agent_exit` analytics skill fields](#agent_exit-records)). After the `agent_exit` audit event fires,
`_run_agent_tracked` emits one `skill_triggered` event per distinct skill the run triggered, reusing the de-duplicated
first-seen list `record_agent_exit` parsed from the same stdout rather than re-reading it. Each event carries `agent`
(backend), `agent_role`, `review_round`, `retry_count`, and the `skill` name — and never the `Skill` tool's `args`
(Privacy, same names-only contract as the analytics fields). A run that triggered nothing, or any install with the
switch off, emits none, so the default audit log is unchanged. The emission rides its own fail-open guard: a bug here
logs and is swallowed, never disturbing the baseline `agent_spawn` / `agent_exit` events. This is the per-invocation
granularity surface; the rolled-up counts live in the `agent_exit` analytics record below.

**No built-in rotation.** `write_event_record` reopens the file in append mode for every event after
`path.parent.mkdir(parents=True, exist_ok=True)`; there is no long-lived file descriptor, no size cap, no rename, and no
compression. External rotation is operator-managed — pair `EVENT_LOG_PATH` with `logrotate` (or equivalent). Because
each append re-resolves the path, create/rename-style rotation is as safe as `copytruncate`: the next event picks up the
new inode without any `SIGHUP` or restart.

An `OSError` during the append is caught and downgraded to a `log.warning` so a misconfigured path (read-only mount,
disk full, permission failure) cannot stop the per-issue tick from making progress; the missing record is silently
dropped and the pinned state on GitHub remains correct.

**Pinned state is authoritative.** The event log is append-only and observation-only. The orchestrator never reads it
back; every dispatch decision keys off the pinned `<!--orchestrator-state ...-->` JSON comment on the issue (and the
issue's workflow label). If the two disagree, trust pinned state. The append-only log is safe to truncate or delete at
any time without affecting workflow correctness.

## Analytics sink (`ANALYTICS_LOG_PATH`)

Project-local JSONL sink for raw metric records, separate from `EVENT_LOG_PATH`. Opts in or out independently via
`ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS`.

**Ownership.** The recorders that append this sink, the by-age prune beside them, and the read and sync paths over
the Postgres target all live under `orchestrator/observability/analytics/`. Every knob named on this page —
`ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`, `ANALYTICS_DB_URL`, the sibling trajectory pair
`TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS`, and `TRACK_SKILL_TRIGGERS` — is parsed by
`analytics/config.py` rather than by `orchestrator/config/`, which keeps only the audit log's own
`EVENT_LOG_PATH` because `GitHubClient.emit_event` is a general-purpose audit surface. What each package along that
write path is responsible for is in
[`architecture/observability-modules.md`](../architecture/observability-modules.md).

**Filesystem only.** No PostgreSQL, Streamlit, or external services — the sink is one JSONL file under the project log
area. Default path is `<LOG_DIR>/analytics.jsonl`, already covered by the `logs/` `.gitignore` rule. Set
`ANALYTICS_LOG_PATH=` (empty) or to `off` / `disabled` / `none` to disable writes entirely; in that mode `append_record`
and `prune_old_records` are silent no-ops and no file is opened.

**Schema.** Every record is built by `recording.build_record` and carries `ts` (UTC ISO-8601 at second precision),
`repo` (the slug `owner/name`), `issue` (issue number, int), and `event` (the kind). `stage` is included when the caller
passes one, and carries the same bare stage tag the audit sink records rather than the `workflow:`-prefixed label — so
`WHERE stage = 'validating'` is the form that matches, here and in the Postgres column the sync loads it into. The
recorders with an audit twin are handed the tag their emitter already resolved; `stage_evaluation` has no twin, and
the per-issue dispatcher that writes it alone normalizes the label it dispatched on the same way. Extras whose value
is `None` are dropped. `json.dumps` uses `sort_keys=True` so on-disk order is stable. The JSONL file is the raw
foundation layer for the Postgres aggregation step.

**Event kinds written today:**

- `stage_enter` — `GitHubClient._emit_stage_enter` alongside the audit `stage_enter`; one record per workflow label
  transition; carries `stage`.
- `stage_evaluation` — the `_process_issue` dispatcher (in `workflow/engine/dispatch.py`); written by its
  try/except/finally wrapper; carries `stage`,
  `duration_s` (handler wall-clock), `result` (`"ok"` / `"error"`); omitted for `backlog`- / `paused`-skipped issues
  (no handler runs).
- `agent_exit` — `_run_agent_tracked` (in `workflow/engine/usage.py`); one record per tracked agent invocation; agent
  context + parsed token / model / cost details (see below).
- `repo_skill_catalog` — `orchestrator.skills.catalog._emit_repo_skill_catalog`, driven once per tick per spec by the
  tick owner (in `workflow/engine/tick.py`, entered through `workflow.tick`); repo-level (not issue-scoped, so
  `issue` is
  the sentinel `0`); carries `base_branch`, `remote_name`, `skills_available` (deduped `SKILL.md` skill names on the
  base ref), and optional `skill_paths` (name → source paths) — see below.
- `late_measurement` / `late_verdict` / `late_failure` / `late_snapshot` / `late_cleanup` / `late_cancellation` /
  `late_restart` — `workflow/late_split/telemetry.py`, one record per late event beside the audit event of the same
  kind; carries `stage` plus the same bounded payload — see
  [Late-split records](#late-split-records-both-sinks).

**Append.** `recording.append_record(record)` reopens the file in append mode for every record after
`path.parent.mkdir(parents=True, exist_ok=True)`. An `OSError` is caught and downgraded to a `log.warning`.

**Retention pruning.** `retention.prune_old_records(*, now=None)` reads the file and removes records whose `ts` is older
than `ANALYTICS_RETENTION_DAYS`. No-op (returns `0`) when the sink is disabled, retention is non-positive, or the file
does not exist. The rewrite goes through a temp file in the sink's own directory followed by `os.replace` so a crash
mid-prune cannot truncate the analytics file. Records with a missing / non-string / unparseable `ts` (and any line that
is not valid JSON) are preserved verbatim so the prune step never silently drops data it cannot interpret.

**Append/prune serialization.** Append and prune share one process-local `threading.Lock`, minted on
`observability/analytics/sink.py` — one mint per process, so every reference to `append_record` takes the object the
prune takes — so a concurrent `append_record` cannot land between the prune's read and its `os.replace`. Under the
scheduler-driven dispatch, `workflow.tick` returns as soon as it has submitted per-issue callables, so scheduler
workers may still be running — and calling `append_record` — when `runtime.ticks.run_tick` invokes
`prune_with_retention_logging()`. Without the lock, an append that opened the old inode after the prune's read but
before the replace would be silently lost. The lock is held only around the filesystem ops; JSON serialization happens
outside the critical section.

**Retention cadence.** `runtime.ticks.run_tick` calls `retention.prune_with_retention_logging()` exactly once per
polling iteration after `workflow.tick` returns for every configured repo, regardless of how many repos are
configured — the sink is process-wide, not per-repo. It names the owner inside the call, so the tick's own import never
pays for the prune graph. Right before the prune, `run_tick` calls `scheduler.reap()` exactly once per polling pass so
worker failure-completion records drain before the next iteration. `_dispatch_via_scheduler` deliberately does NOT
reap. The wrapper catches exceptions and logs the `"removed N record(s)"` message so the call site in the tick stays a
one-liner, and it dispatches `prune_old_records` on its own module so
`patch.object(retention, "prune_old_records", ...)` still intercepts. Per-tick cost is bounded: the helper reads the
file at most once and only rewrites it when at least one record is older than the retention window.

**Pinned GitHub state is unaffected.** The prune touches only the local file — no issue comment, label, or other
GitHub state is rewritten. The analytics sink is local-filesystem observability and is safe to truncate or delete at any
time.

### `agent_exit` records

`_run_agent_tracked` (in `workflow/engine/usage.py`) appends a single `event="agent_exit"` analytics record after
every tracked agent run, distinct from (and in addition to) the audit `agent_spawn` / `agent_exit` events on
`EVENT_LOG_PATH`. Each record carries:

- **Context** — `repo`, `issue`, `stage`, `agent_role`, `backend`, `review_round`, `retry_count`, `duration_s`,
  `exit_code`, `timed_out`.
- **Spec / session** — the configured `agent_spec` (the role's full `*_AGENT_SPEC` string, e.g. `claude --model
  claude-opus-4-7`), both the `resume_session_id` passed into the spawn and the live `session_id` from the result.
- **Usage parser output** — `input_tokens`, `output_tokens`, `cached_tokens`, `cache_read_tokens`,
  `cache_write_tokens`, the distinct `models` observed in the stream, `turns`, `cost_usd`, and `cost_source`.
- **Skill triggers (opt-in)** — only when `TRACK_SKILL_TRIGGERS` is on (default off): `skills_triggered` (distinct
  skill names, first-seen order), `skills_triggered_count` (total trigger count, so three `develop` pulls read `3` while
  the list carries `develop` once), `skills_evidence` (name → the per-load evidence tier: `confirmed` for a claude
  `Skill` tool call, `inferred` for a codex command that directly reads the skill's `SKILL.md` with a reader verb such
  as `cat` / `sed`), the incidental pair `skills_incidental` / `skills_incidental_count` (path-only references a codex
  run made to a `SKILL.md` without reading it — a `git diff` / `git status` / `rg`, an env-prefixed inspection, a write
  to the file (`>` redirect or `sed -i`), or any other non-reader command — kept out of `skills_triggered`, its count,
  and the `skill_triggered` audit events so a bystander
  mention is never miscounted as a load, but recorded independently: a skill both read *and* inspected appears in both
  buckets), and `skills_available` (the offered-skills set). On **claude** the offered set is
  read from the dedicated `skills` array in the `system`/`init` stream frame — confirmed against a real captured
  `--output-format stream-json` run — so `skills_available` is populated for tracked claude runs independently of what
  they triggered. Codex's `codex exec --json` stream carries no such offered-skills catalog, so for **codex** the set is
  instead discovered out-of-band from the filesystem by `skills.discovery.discover_local_skill_sources(cwd)` — a scan of
  the repo skill roots (`.agents/skills` / `.claude/skills`) under the run's worktree plus the global
  `$CODEX_HOME/skills` codex
  loads, including the built-in skills under that global root's `.system` container (`imagegen`, `openai-docs`, …). It
  runs only for codex, never overrides the claude stream-parsed set, and is fail-open (a missing root leaves the field
  empty). That scan also reports the *source level* each name was defined at, recorded beside the array as
  `skill_levels` (name → `project` for a definition under one of the worktree's own skill roots, `user` for one
  installed directly under the global `$CODEX_HOME/skills`, `harness` for a built-in under that root's `.system`
  container). The map rides *beside* `skills_available` instead of reshaping it, so the array keeps the shape a reader
  already knows and the addition is another `extras JSONB` key with **no DDL change**. Provenance is reported only
  where the enumeration knows it: claude's `system`/`init` frame names no source directory for the skills it lists, so
  a tracked claude run records `skills_available` with the `skill_levels` key dropped rather than a guessed level. Each
  field is dropped (its key absent) when empty, so a claude run that was offered skills but triggered none records
  `skills_available` while the triggered / evidence keys drop — the "offered but unused" vs "never available" signal —
  and a run with nothing to report keeps the record shape identical to the switch-off case. Parsed via
  `observability/usage/skills.py`'s `parse_agent_skills` under its own fail-open guard inside `record_agent_exit`: a
  skill-parse failure logs and still emits the baseline usage / cost record, and reads only the skill *name* — never
  the `Skill` tool's `args`, the surrounding codex command text, or a command's `aggregated_output` (the file's
  contents). With the switch off the extractor never runs and none of the skill keys appear.

The configured model is pulled out of the role's `extra_args` (via `_configured_model`; recognises `-m <model>` /
`-m=<model>` for codex and `--model <model>` / `--model=<model>` for claude) and forwarded as the parser's
`fallback_model` so a codex run whose stdout includes usage frames but omits the model still records the configured
model and — when it matches a priced family — an estimated `cost_usd`. A stream-reported model always wins over the
fallback.

Prompts, raw stdout / stderr, secrets, and worktree contents are deliberately NOT stored — the sink is a usage / cost
surface, not a debugging mirror. A parser exception or sink IO failure is swallowed so an analytics misconfiguration
cannot stop the per-issue tick.

**Skill-trigger surfaces (shipped).** Both skill-trigger follow-ups (the audit event and the dashboard widget) have now
landed. The per-invocation `skill_triggered` audit event on [`EVENT_LOG_PATH`](#audit-event-log-event_log_path) (see the
[audit event-kinds list](#audit-event-log-event_log_path)) is gated on the same `TRACK_SKILL_TRIGGERS` switch and
reuses the list `record_agent_exit` already parsed — `_run_agent_tracked` emits one event per distinct triggered
skill. The dashboard's primary skill metric is per-session **adoption** (`get_skill_adoption` + the "Skill adoption"
panel), which counts, for each `(repo, role, backend, skill, level)` cell, how many logical agent sessions had the
skill available and how many loaded it — an incidental `SKILL.md` reference stays a separate diagnostic column and never
raises the rate. The invocation-level views (`get_skill_trigger_rates` and `get_skill_trigger_matrix`) open the card
above it as a clearly named invocation-level diagnostic, and adoption itself is folded into one collapsed section per
source level under them — see the
[read model](analytics-dashboard.md#read-model-orchestratorobservabilityanalyticsquery) and
[dashboard](analytics-dashboard.md#dashboard-orchestratorappsanalytics_dashboardpy) sections
on that page. All are pure read-side additions over `extras JSONB` with no schema change. See
[Session-aware skill adoption](#session-aware-skill-adoption) for the four evidence forms and the per-session adoption
semantics that sit on top of these fields.

### Session-aware skill adoption

The dashboard's **primary** skill metric is per-session *adoption* — for each
`(repo, agent_role, backend, skill, level)` cell, what share of the logical agent sessions that had the skill
available actually loaded it. It is computed by
`observability/analytics/query/skill_reads.py`'s `get_skill_adoption` and rendered by
`observability/dashboard/skill_panel.py`'s "Skill adoption" card, in a collapsed section per source level; the
older per-run trigger views
(`get_skill_trigger_rates` / `get_skill_trigger_matrix`) open that card above them as a clearly named
invocation-level diagnostic.
The per-session adoption metric reads the opt-in `agent_exit` skill fields above, so it only carries signal once
`TRACK_SKILL_TRIGGERS` has recorded a session's available and loaded skills. The invocation-level views degrade more
gently with the switch off: the trigger-rate table still counts every `agent_exit` run (a `0` trigger rate), and the
matrix still renders each repo's catalog-backed skills as explicit zero rows, because the `runs` denominator and the
`repo_skill_catalog` records do not depend on the switch. Records written while it was on stay queryable after it is
turned off.

**Four evidence forms.** A skill observation is classified into one of four forms. The first three are emitted on the
`agent_exit` record; the fourth is a read-model inference and is never written to disk:

- **confirmed** *(load)* — a claude `Skill` tool-use block, the firm signal. Recorded in `skills_triggered`, with tier
  `confirmed` in `skills_evidence`.
- **inferred** *(load)* — a codex `command_execution` whose leading verb is an established direct reader
  (`cat` / `sed` / `head` / …) opening a `skills/<name>/SKILL.md`. A heuristic file-open signal. Recorded in
  `skills_triggered`, with tier `inferred` in `skills_evidence`. A single run is homogeneous — claude only confirms,
  codex only infers — so every `skills_evidence` entry on one record shares its tier.
- **incidental** *(not a load)* — a codex *path-only* reference to a `SKILL.md`: a non-reader inspection / search
  (`git diff` / `git status` / `rg`), an env-prefixed inspection (`GIT_PAGER=cat git diff …`), or a write to the file
  (`>` redirect / `sed -i`). Recorded independently in `skills_incidental` / `skills_incidental_count`, deliberately
  kept out of `skills_triggered`, `skills_triggered_count`, `skills_evidence`, and the `skill_triggered` audit events,
  so a bystander mention is never miscounted as a load. A skill both read *and* inspected lands in both buckets.
- **legacy** *(implied availability)* — not an emitted field. Inside `get_skill_adoption`, a load whose logical
  session never reported any `skills_available` metadata (no row carried the `skills_available` *key*) is treated as
  implied availability: the load itself implies the skill was offered, so it still counts in that session's
  availability denominator. An explicit empty `skills_available: []` is metadata ("scanned, found none") and
  **blocks** this fallback, so a load against a session that reported no offered skills does not fabricate availability.

**Logical-session fallback.** Adoption counts by *logical agent session*, not by raw run, so a resume chain that pulled
`develop` across several ticks counts as one adopting session, not several. A session is keyed by `resume_session_id`,
then `session_id`, then the row's primary key — an ID-less row is its own session, never merged into one anonymous
bucket, and the primary-key fallback is stable across both scans below.

**Active window vs. historical lookback.** `get_skill_adoption` runs two `agent_exit` scans and combines them in Python:

- The **active-window** scan applies the full reporting-window filters (date `[start, end)` / repo / stage / issue). It
  selects the *active* sessions (those with a run in the window) and computes the window-scoped invocation diagnostics.
- The **historical-lookback** scan (`WindowFilters.historical_scope`) gathers each active session's availability + load
  evidence from every `agent_exit` row *before the window end*, deliberately dropping the window `start` bound and the
  stage / events filters while keeping `end` / repo / issue — so a load or an availability report from a prior
  stage, or from before the reporting window, still counts toward that session's denominator and `adopted`. History
  rows for sessions that were not active in the window are ignored, so their evidence never leaks into the aggregate.

The retained `end` bound is the **future-evidence cutoff**: evidence recorded *after* the window end never leaks
backward into an earlier window's aggregate, so a later load cannot retroactively raise a past window's adoption.

**A cell is keyed by source level too.** Both per-skill read models file their counts under the skill's `skill_levels`
level as well as its name, so a repository's own `develop` and a same-named global one stay two cells rather than one
blended average. The level is read off the same row that named the skill, so an offer and a load are matched by
provenance as well as by name. A name no level map covers reads `unknown` — a record written before levels existed, or
a claude run whose stream names no source directory — which is one spelling an operator can look up rather than a
scattering of blanks; because both an offer and a load read it, a legacy session's load still counts as adoption of
what it was offered. The one exception is the trigger matrix's catalog padding: a `repo_skill_catalog` name the record
left unclassified pads at `project`, since that scan enumerates a repository's own checked-in definitions. Both tables
render the level as its own sortable `Level` column, so two rows whose Repo / Role / Backend / Skill read the same are
legible as the two definitions their differing counts come from.

**Per-session availability denominator.** `sessions` (the denominator) is how many logical sessions in the cohort had
the skill available — its reported `skills_available` union listed it, or the *legacy* fallback above implied it.
`adopted` (the numerator) is how many of those sessions loaded it, counted once per session no matter how many runs
reached for it; `adoption_rate` is `adopted / sessions`. A zero-session cell has an undefined rate that renders as a
muted `—`, never a misleading `0%`.

**Primary adoption vs. invocation-level diagnostics.** The read model carries three **window-scoped invocation** fields
(raw `agent_exit` rows, not sessions, and not the historical evidence): `invocations` is every run in the cohort's
window, `load_rows` the window runs that loaded the skill, and `incidental` the window runs that made a path-only
(incidental) reference to its `SKILL.md`. The load and incidental buckets are independent — a single run can appear
in both — so `incidental` is a parallel count, not a "did-not-load" complement, and it can never raise the adoption
rate. Of these three the adoption table renders only `Invocation loads` (`load_rows`) and `Incidental references`
(`incidental`) as its two trailing columns; `invocations` (the cohort's total window run count) is a read-model field
used for ordering and context, not a displayed column. A pre-window load counts toward `adopted` but toward none of
the three, since all three are window-scoped. The collapsed invocation-level diagnostic above the adoption sections
(`get_skill_trigger_rates` / `get_skill_trigger_matrix`) reports the same per-run granularity across roles / backends
and per-skill cohorts. See the
[read model](analytics-dashboard.md#read-model-orchestratorobservabilityanalyticsquery) for the exact query shapes and
the [dashboard](analytics-dashboard.md#dashboard-orchestratorappsanalytics_dashboardpy) for the rendered columns.

### `repo_skill_catalog` records

`orchestrator/skills/catalog.py` appends one repo-level `event="repo_skill_catalog"` analytics record per tick per spec,
driven from `workflow/engine/tick.py` after `_refresh_base_and_worktrees` has fetched `<remote_name>/<base_branch>`,
before the scheduler / in-tick split so it fires once per tick on either dispatch path. It enumerates
the `SKILL.md` definitions the *target repo* carries on its base ref via `git -C <target_root> ls-tree -r --name-only
<remote_name>/<base_branch> .agents/skills .claude/skills`, keeps only direct `<root>/<name>/SKILL.md` definitions (a
`SKILL.md` nested deeper — e.g. `.claude/skills/.system/<name>/SKILL.md` — is ignored, matching the names-only
trigger anchor in `observability/usage/skill_commands.py`), and dedupes by skill name across the two roots while
preserving every source path. The catalog is read from the target repo's base ref, never the orchestrator's own
working tree, so dashboard-local skill files are not scanned.

Each record carries `base_branch`, `remote_name`, `skills_available` (the sorted deduped skill names), and two
optional per-name maps: `skill_paths` (name → sorted source paths) and `skill_levels` (name → source level), both
dropped when empty. Everything this scan enumerates is a definition checked into the target repo itself, so every name
is classified `project` — the same level `skills.discovery` stamps a worktree skill root with, read back from that
owner so the two enumerations cannot disagree about what a repository definition is. It is **not** issue-scoped, so its
`issue` is the sentinel `0` — the record still satisfies the `ts` / `repo` / `issue` / `event` envelope the sink and the
Postgres `analytics_events` schema require, and the five catalog fields all land in `extras JSONB` with **no DDL
change**. The whole producer is fail-open: a missing clone, an unfetched ref, a git error, or a sink IO failure logs and
is swallowed so catalog collection never disturbs the polling tick. An empty catalog still records `skills_available:
[]` (the "scanned, found none" signal) with both maps dropped.

## Late-split records (both sinks)

The late size gate writes to **both** streams, deliberately: the audit copy has to answer offline what the database
answers, so an operator with only the JSONL file can tell whether depth 3 is being approached, which repositories keep
producing artifact-dominated `single` verdicts, and whether the configured threshold creates more adjudication than it
prevents. One call on `workflow/late_split/telemetry.py` emits both — `GitHubClient.emit_event` for the audit record
and the recording package's `build_record` / `append_record` pair for the analytics record — so the two carry the same
fields under their own envelopes.

**Families.** `late_measurement` (a clean committed candidate was measured), `late_verdict` (an adjudication decided),
`late_failure` (a typed step could not be completed), `late_snapshot` and `late_cleanup` (what happened to one
external resource), `late_cancellation` (the owner was observed closed), and `late_restart` (a restart after a
completed cancellation). The kind is the family; `stage` is the bare stage tag the issue sat in, spelled by the
emitter like every other event on this page.

**What a stream carries today.** The gate is not wired into publication yet, so the only producer that exists is the
late adjudication under `workflow:decomposing`
([`../workflow/roles.md`](../workflow/roles.md#what-a-late-adjudication-is-asked-and-what-it-may-answer)): it writes
one `late_verdict` per completed adjudication, one `late_measurement` per candidate a developer revision re-froze and
re-measured
([`../workflow/roles.md`](../workflow/roles.md#what-the-humans-can-still-change-while-a-candidate-is-frozen)), one
`late_cancellation` per cycle whose owner the post-agent guard found closed after ANY completed run — a question and
a timeout included ([`../workflow/roles.md`](../workflow/roles.md#the-owner-read-a-finished-run-has-to-pass)) — and a
`late_failure` carrying `plan_pr_hold_failed` when the plan-PR hold cannot be reconciled or released on a still-open
plan PR (a notice a human removed from one included, which starts no new agent under it), `measurement_failed` when a
revised candidate could not be measured, `owner_read_failed` on every read that guard could not take, or
`pr_reconcile_failed` when the pull request an accepted candidate would be handed on against could not be
established. Two more arrive with the split transaction
([`../workflow/roles.md`](../workflow/roles.md#what-a-cleared-split-actually-does)): one `late_snapshot` per
snapshot ref established (`retained`) or refused (`failed`), and one `late_cleanup` per superseded branch the
transaction reconciled (`reconciled`) or could not (`failed`) — the latter beside a `late_failure` carrying
`snapshot_failed` or `branch_cleanup_failed`, and `child_create_failed` or `supersession_failed` where those steps
park instead. What the transaction could not reclaim is retried at the umbrella's terminal gate, which emits the
same pair under `stage: umbrella` on every tick that finds every child resolved and something still owed — the
branch unconditionally, and the snapshot ref once every recorded direct consumer is terminal, carrying
`snapshot_delete_failed` where the remote refuses one. So a repeated `late_cleanup` with `outcome: failed` on one
issue is exactly the shape of an obligation nobody can settle, and the umbrella stays open while it repeats.
`late_restart` is still the contract the restart step will emit under, and no record of it can appear in either
stream until that step lands.

**Family-typed events.** A record is built from a `LateEvent` on `workflow/late_split/events.py`, and each family
declares which detail fields it requires and which it may carry (`_FAMILY_FIELDS`). Anything else raises
`InvalidLateValue` where the event is constructed, so a measurement claiming a verdict, a verdict with no verdict on
it, or a cleanup with no resource cannot reach either sink at all:

| Family | Requires | May also carry |
| --- | --- | --- |
| `late_measurement`, `late_cancellation` | — | — |
| `late_verdict` | `verdict`; `category` too when `question` | `category` on any verdict, `child_count` with `split` |
| `late_failure` | `failure` | — |
| `late_snapshot`, `late_cleanup` | `resource` | — |
| `late_restart` | `restart_step` | — |

A category is allowed on **every** verdict and required only of a `question`: a `single` verdict that explains itself
as `generated_artifacts` is exactly the artifact-dominated signal this page promises, so the schema has to be able to
carry it. A child count is a split's and nobody else's, in both directions.

**Types are enforced, not annotated.** A `StrEnum` member and the string that spells it compare equal, so a raw
`verdict="question"` would satisfy every comparison the schema makes and be written verbatim — and so would a
`category` carrying an agent's rationale. Each detail is therefore checked for being an actual member (a resource
through to its own `kind`, `state`, and bounded target), and a count for being a real non-negative integer. The same
`check` runs again inside the payload builder, so an event that reached it without passing through the constructor is
refused there.

**Payload.** Assembled by `workflow/late_split/records.build_late_payload` from the frozen `LateGeneration` and that
typed event, then filtered through the declared `LATE_PAYLOAD_FIELDS`, so widening a record is an edit to that tuple
rather than a keyword somebody passed:

- **Correlation** — `cycle_id`, `generation`, `root_issue`, `lineage_depth`, `phase`. A lineage depth the pinned state
  could not read is absent rather than reported as the root's 0.
- **The commits** — `source_sha` (the frozen candidate) and `base_sha` (the exact remote base it was measured against).
- **The measurement** — `threshold` and `additions`.
- **Family fields** — `verdict` (`single` / `split` / `question`), `category`, `child_count`, `failure` (the typed
  reason), `resource` (`snapshot_ref` / `branch` / `plan_pr` / `child`) with `resource_id` and `outcome` (`pending` /
  `retained` / `reconciled` / `failed`), `restart_step` (`pending` / `reconciled`), and `restart_target` +
  `predecessor_cycle_id`.

Extras whose value is `None` are dropped by both envelope builders, so each family carries only what applies to it.

**What is deliberately absent.** No file paths, no diff content, no prompt, no agent rationale, and no agent output.
The payload has no argument any of those could arrive through — it is built from a frozen record and a family-typed
event — and every field that could otherwise smuggle text through is closed at the boundary:

- **A verdict `category`** is a member of `LateVerdictCategory` (`generated_artifacts`, `scope_ambiguous`,
  `unsafe_split`, `lineage_bound`, `unknown`), not a label an agent writes. `events.verdict_category` maps a parsed
  answer onto it and answers `unknown` for everything it does not recognize, so an adjudication's rationale — the
  sentences, the file names in them — has no path into a record. Widening the vocabulary is an edit here, in review.
- **A resource's own name** — a ref, a branch, an issue number — is not recorded. What identifies it is `resource_id`,
  the bounded 12-character fingerprint `identity.resource_fingerprint` takes over the entry's kind and target. It is
  stable across retries of one resource and distinct between two of the same kind, which is what lets a consumer tell
  two children's cleanups apart without being told which children they were.
- **The generation's own fields** are checked by `workflow/late_split/validation.py` before anything is built, because
  `candidate_sha`, `base_sha`, `phase`, and `restart_target` are typed `str` and would otherwise be written verbatim.
  A commit field must be a whole git object id — 40 or 64 hex, since nothing here records an abbreviation — a phase
  must be a member, a restart target must be one of the two labels a restart may apply, and every count must be a
  real non-negative integer. The four correlating identities — `cycle_id`, `generation`, `root_issue`,
  `current_issue` — are **required**: a record nothing can be joined to is not one this domain writes. A refusal
  names the field and the type it arrived as, never the value, so a field rejected for carrying prose does not put
  that prose in the log line instead of the sink.
- **`stage`** is resolved against the workflow label vocabulary rather than passed through. A caller may name either
  spelling — `workflow:decomposing` or the bare `decomposing` — and what reaches both sinks is always the tag, which
  is what every other emitter on this page records. Anything outside that closed set, prose included, is refused with
  the record it came with: it is the one envelope field this domain supplies, and the sinks would carry whatever they
  were handed.

**Self-contained by family.** Beyond the shared fields, `late_measurement` and `late_verdict` must carry
`source_sha`, `base_sha`, `threshold`, `additions`, and `phase` — the commits that were frozen and the measurement
taken against them. A record of either reporting only an identity would be a row no threshold study could use, so it
is not written. The other five families describe reconciliation rather than size, and a restart's fresh cycle has
deliberately let its commits go, so none of them is held to it.

**A refused record is a non-emission, not an exception.** `telemetry.emit_late_event` runs the build inside the same
fail-open guard as the two writes: the refusal is logged on the `orchestrator.workflow` channel, the call returns an
empty payload, neither sink is touched, and the tick that asked carries on. The log line is held to the same boundary
the record is — an issue number that is not one and a family that is not a member are reported as `?` rather than as
they arrived, since a log is the same surface one step over from the sinks it was protecting. That extends to why the
record was refused: this domain's own refusals are built from field names, vocabularies, and type names, so their
message is repeated, while an exception raised anywhere below is named by its type alone. Nothing renders the
exception itself — `log.exception` would append its text and traceback, and only a refusal this domain built is
guaranteed safe to repeat.

**Duplicates.** Records are emitted before the step they describe is durable, so a crash can produce the same record
twice. Consumers deduplicate on `records.CORRELATION_FIELDS`, which is **the whole record apart from `ts`**: the four
envelope fields (`repo`, `issue`, `event`, `stage`) plus every field in `LATE_PAYLOAD_FIELDS`. A retried step writes
every field again identically, so the timestamp is the only thing that can differ between one step's two emissions,
and any other difference is a different step by construction — one candidate split into two children and into seven,
two questions asked under different categories, two cleanups of two children, two restarts aimed at different states,
two measurements against different bases. Naming the distinguishing fields one at a time instead is what let pairs
like those collide, because the list has to be remembered every time the payload grows. Nothing about workflow
disposition may depend on delivery, which is the other half of the same rule.

**Fail-open, twice.** Both sinks already swallow a filesystem refusal, and each emission additionally rides its own
guard on the `orchestrator.workflow` channel, so a failing sink costs the record and nothing else — and a failure on
one side does not skip the other. A late generation is reconciled from its pinned state
([`state-machine/labels-and-state.md#late-generation-state`](../state-machine/labels-and-state.md#late-generation-state)),
never from what a sink accepted.
