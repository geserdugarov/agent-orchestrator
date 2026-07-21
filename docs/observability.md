# Observability

The orchestrator emits three independent JSONL sinks plus an optional Postgres aggregation target. None are read by the
polling tick — workflow correctness keys off the pinned `<!--orchestrator-state ...-->` JSON comment on the issue (and
the workflow label). Every observability surface here is observation-only and safe to truncate, rotate, or delete at any
time.

- **Audit event log** (`EVENT_LOG_PATH`) — opt-in JSONL audit of workflow events, written through
  `GitHubClient.emit_event`.
- **Analytics sink** (`ANALYTICS_LOG_PATH`) — project-local JSONL of raw metric records, owned by the
  `orchestrator/analytics/` package.
- **Trajectory sink** (`TRAJECTORY_LOG_PATH`) — opt-in, default-off JSONL sink for per-run agent reasoning
  trajectories, a sibling of the analytics sink in the `orchestrator/analytics/` package. `record_agent_exit` is its
  producer: when the sink is on it parses each tracked run's trajectory from the same stdout, redacts and head/tail
  truncates every free-text field, and appends one `agent_trajectory` record — all behind its own fail-open guard. A
  dedicated, file-backed **trajectory viewer** (`orchestrator/trajectory_dashboard.py`) renders it as a separate
  Streamlit page.
- **Analytics database** (`analytics-db/`) — operator-deployed Postgres service that is the aggregation target for the
  analytics sink, with an operator-driven sync CLI and a Streamlit dashboard on top.
- **Usage parser** (`orchestrator/usage.py`) — decoder for the agent CLI JSONL stdout that produces the token / cost
  detail the analytics `agent_exit` record carries.

## Audit event log (`EVENT_LOG_PATH`)

Optional, opt-in JSONL sink. When `config.EVENT_LOG_PATH` is set, `github._write_event_record` appends one JSON object
per audit event to that file inside `GitHubClient.emit_event`; when unset (the default) the helper short-circuits to a
no-op. The fake `GitHubClient` in `tests/fakes.py` calls the same helper.

**Schema.** Every record is built by `github.build_event_record` and carries `ts` (UTC ISO-8601 at second precision),
`repo` (the slug `owner/name`), `issue` (issue number, int), and `event` (the kind). `stage` is included when the
emitter passes one (effectively always today). Extras whose value is `None` are dropped. `json.dumps` is called with
`sort_keys=True` so on-disk order is stable across writers.

**Event kinds.** Every kind is emitted through the single `GitHubClient.emit_event` chokepoint, which also appends to a
capped in-memory tail (`recorded_events`, `_RECORDED_EVENTS_CAP = 500`) for tests and short-window debugging — the
file is the durable record.

- `stage_enter` — `set_workflow_label` (via `_emit_stage_enter`) for every label flip; extras: `stage`.
- `agent_spawn` / `agent_exit` — `workflow._run_agent_tracked` wraps every `run_agent` call (decomposer, implementer,
  reviewer, dev-resume, conflict-resolution dev); extras: `agent` (backend), `agent_role`, `review_round`,
  `retry_count`. `session_id` and `agent_exit`-only fields are described below.
- `skill_triggered` — `workflow._run_agent_tracked` after `agent_exit`, **only when `TRACK_SKILL_TRIGGERS` is on**
  (default off); one event per distinct skill the run triggered; extras: `agent` (backend), `agent_role`,
  `review_round`, `retry_count`, `skill` (the triggered skill name). Reuses the list `record_agent_exit` already parsed;
  off-switch installs emit none.
- `review_verdict` — `_handle_validating` after `_parse_review_verdict` reads the reviewer's last message; extras:
  `verdict` (`approved` / `changes_requested` / `unknown`), `review_round`, `pr_number`, `session_id`.
- `park_awaiting_human` — every `_park_awaiting_human` call site, plus `_on_question`, `_on_dirty_worktree`,
  `_park_verify_failure`, and the question-stage `_park_question` funnel; extras: `stage` (read from the current
  workflow label, not passed in), `reason` (e.g. `agent_timeout`, `push_failed`, `failed_checks`, `agent_question`,
  `agent_session_limit` (a quota-exhausted agent message, parked retryably as `agent_silent`), `dirty_worktree`,
  `reviewer_timeout`, `verify_failed` / `verify_timeout` / `verify_dirty` / `verify_head_changed`, `question_*`, ...).
- `pr_opened` — `_on_commits` after `gh.open_pr` succeeds; extras: `pr_number`, `branch`, `sha`, `retry_count`.
- `pr_merged` — External merge terminal arcs in `_handle_in_review`, `_handle_fixing`, `_handle_resolving_conflict`;
  plus `_finalize_if_pr_merged` from `_handle_implementing` / `_handle_documenting` / `_handle_validating` entry checks
  and from the `_handle_blocked` / `_handle_umbrella` manually-closed child recovery; extras: `pr_number`, `sha`,
  `merge_method="external"`, `review_round`, `conflict_round`, `retry_count`; `stage` reflects the workflow label at
  finalize entry.
- `pr_closed_without_merge` — `_handle_in_review`, `_handle_fixing`, `_handle_resolving_conflict` when the PR is
  closed without merge; plus `_finalize_if_issue_closed` from `_handle_implementing` / `_handle_documenting` /
  `_handle_validating` entry checks (only when the linked PR is also closed; an open PR with a manually-closed issue is
  left alone); extras: `pr_number`, `sha`, `review_round`, `conflict_round`, `retry_count`; `stage` reflects the
  workflow label at finalize entry.
- `merge_attempt` — Every `git rebase origin/<base>` inside `_handle_resolving_conflict`; extras:
  `method="base_rebase"`, `result` (`success` / `failed` / `conflict`), `pr_number`, `sha`, `conflict_round`,
  `review_round`, `retry_count`.
- `conflict_round` — `_route_pr_worktree_to_resolving_conflict` emits `action="entered"` only when the refresh-time
  rebase actually leaves conflicted files (a merely-behind-base clean rebase no longer emits this);
  `_reconcile_parked_fixing` also emits `action="entered"` (with `stage="fixing"`) when a stuck validating-route
  transient `fixing` park is routed to `resolving_conflict` because its worktree is out of sync with the PR head (behind
  base, or an unpushed local rebase); every increment site (`_emit_conflict_round_incremented`) emits
  `action="incremented"` with `outcome`; extras: `pr_number`, `conflict_round`, `review_round`, `retry_count`, `outcome`
  (for increments), `sha`.
- `base_rebased` — `_sync_pr_worktree_to_base` after a clean refresh-time rebase + push that routes the issue from
  `validating` / `documenting` / `in_review` / `fixing` back to `validating`; also `_recover_pending_auto_base_rebase`
  when a crashed prior tick is finalized; extras: `pr_number`, `sha` (new head), `method` ∈ {`auto_clean_rebase`,
  `crash_recovery_pushed`, `crash_recovery_relabel_only`}, `review_round` (post-reset, so 0), `retry_count`; `stage`
  reflects the workflow label at the start of the rebase.

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

**No built-in rotation.** `_write_event_record` reopens the file in append mode for every event after
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
`ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS` and the helpers in `orchestrator/analytics/`.

**Module layout.** The event-recording implementation — sink configuration, the JSONL append primitives, and the stage /
repo-skill / agent-exit recorders — lives in `orchestrator/analytics/_recording.py`; the opt-in trajectory sink's
serialization, redaction / truncation, budgeting, and append helpers live in the sibling
`orchestrator/analytics/_trajectories.py` (which reuses `_recording`'s append core and reads its `_live_settings`); the
by-age retention pruning for both sinks — the JSONL prune, scan models, atomic temp-file rewrite, retention logging, and
the `prune_old_records` / `prune_trajectory_records` / `prune_with_retention_logging` entry points — lives in the
sibling `orchestrator/analytics/_retention.py` (which shares `_recording`'s `_FILE_LOCK`, `_trajectories`'
`_TRAJECTORY_FILE_LOCK`, and both modules' `_live_settings`). `orchestrator/analytics/__init__.py` is a facade that
re-exports all three surfaces and binds the sink knobs as package attributes. The read / sync submodules (`read`,
`read_*`, `sync`, `connection`, `query`, `predicates`, `db_url`) are the separate Postgres-facing surfaces.

**Settings ownership.** `ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`, and `ANALYTICS_DB_URL` (and the sibling
trajectory-sink knobs `TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS`) are parsed at import by
`orchestrator/analytics/_recording.py` and bound as attributes of the `orchestrator/analytics` package — *not* in
`orchestrator/config.py`. They are exposed as package attributes (`analytics.ANALYTICS_LOG_PATH`, etc.) that tests patch
directly via `patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`; the recorders in `_recording` / `_trajectories` and
the prune wrappers in `_retention` read them back off the package facade at call time, so a patch or a package reload
takes effect. The audit event log (`config.EVENT_LOG_PATH`) stays in `config` because `GitHubClient.emit_event` is a
general-purpose audit surface.

**Filesystem only.** No PostgreSQL, Streamlit, or external services — the sink is one JSONL file under the project log
area. Default path is `<LOG_DIR>/analytics.jsonl`, already covered by the `logs/` `.gitignore` rule. Set
`ANALYTICS_LOG_PATH=` (empty) or to `off` / `disabled` / `none` to disable writes entirely; in that mode `append_record`
and `prune_old_records` are silent no-ops and no file is opened.

**Schema.** Every record is built by `analytics.build_record` and carries `ts` (UTC ISO-8601 at second precision),
`repo` (the slug `owner/name`), `issue` (issue number, int), and `event` (the kind). `stage` is included when the caller
passes one; extras whose value is `None` are dropped. `json.dumps` uses `sort_keys=True` so on-disk order is stable. The
JSONL file is the raw foundation layer for the Postgres aggregation step.

**Event kinds written today:**

- `stage_enter` — `GitHubClient._emit_stage_enter` alongside the audit `stage_enter`; one record per workflow label
  transition; carries `stage`.
- `stage_evaluation` — `workflow._process_issue` dispatcher (try/except/finally wrapper); carries `stage`,
  `duration_s` (handler wall-clock), `result` (`"ok"` / `"error"`); omitted for `backlog`- / `paused`-skipped issues
  (no handler runs).
- `agent_exit` — `workflow._run_agent_tracked`; one record per tracked agent invocation; agent context + parsed token
  / model / cost details (see below).
- `repo_skill_catalog` — `orchestrator.skill_catalog._emit_repo_skill_catalog`, driven once per tick per spec by
  `workflow.tick`; repo-level (not issue-scoped, so `issue` is the sentinel `0`); carries `base_branch`, `remote_name`,
  `skills_available` (deduped `SKILL.md` skill names on the base ref), and optional `skill_paths` (name → source
  paths) — see below.

**Append.** `analytics.append_record(record)` reopens the file in append mode for every record after
`path.parent.mkdir(parents=True, exist_ok=True)`. An `OSError` is caught and downgraded to a `log.warning`.

**Retention pruning.** `analytics.prune_old_records(*, now=None)` reads the file and removes records whose `ts` is older
than `ANALYTICS_RETENTION_DAYS`. No-op (returns `0`) when the sink is disabled, retention is non-positive, or the file
does not exist. The rewrite goes through a temp file followed by `os.replace` so a crash mid-prune cannot truncate the
analytics file. Records with a missing / non-string / unparseable `ts` (and any line that is not valid JSON) are
preserved verbatim so the prune step never silently drops data it cannot interpret.

**Append/prune serialization.** Append and prune share a process-local `threading.Lock` inside the analytics module so a
concurrent `append_record` cannot land between the prune's read and its `os.replace`. Under the scheduler-driven
dispatch, `workflow.tick` returns as soon as it has submitted per-issue callables, so scheduler workers may still be
running — and calling `append_record` — when `main._run_tick` invokes `prune_with_retention_logging()`. Without the
lock, an append that opened the old inode after the prune's read but before the replace would be silently lost. The lock
is held only around the filesystem ops; JSON serialization happens outside the critical section.

**Retention cadence.** `main._run_tick` calls `analytics.prune_with_retention_logging()` exactly once per polling
iteration after `workflow.tick` returns for every configured repo, regardless of how many repos are configured — the
sink is process-wide, not per-repo. Right before the prune, `_run_tick` calls `scheduler.reap()` exactly once per
polling pass so worker failure-completion records drain before the next iteration. `_dispatch_via_scheduler`
deliberately does NOT reap. The wrapper catches exceptions and logs the `"removed N record(s)"` message so the call site
in `main` stays a one-liner. Per-tick cost is bounded: the helper reads the file at most once and only rewrites it when
at least one record is older than the retention window.

**Pinned GitHub state is unaffected.** The prune touches only the local file — no issue comment, label, or other
GitHub state is rewritten. The analytics sink is local-filesystem observability and is safe to truncate or delete at any
time.

### `agent_exit` records

`workflow._run_agent_tracked` appends a single `event="agent_exit"` analytics record after every tracked agent run,
distinct from (and in addition to) the audit `agent_spawn` / `agent_exit` events on `EVENT_LOG_PATH`. Each record
carries:

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
  instead discovered out-of-band from the filesystem by `skill_catalog.discover_local_skills(cwd)` — a scan of the repo
  skill roots (`.agents/skills` / `.claude/skills`) under the run's worktree plus the global `$CODEX_HOME/skills` codex
  loads, including the built-in skills under that global root's `.system` container (`imagegen`, `openai-docs`, …). It
  runs only for codex, never overrides the claude stream-parsed set, and is fail-open (a missing root leaves the field
  empty). Each
  field is dropped (its key absent) when empty, so a claude run that was offered skills but triggered none records
  `skills_available` while the triggered / evidence keys drop — the "offered but unused" vs "never available" signal —
  and a run with nothing to report keeps the record shape identical to the switch-off case. Parsed via
  `usage.parse_agent_skills` under its own fail-open guard inside `record_agent_exit`: a skill-parse failure logs and
  still emits the baseline usage / cost record, and reads only the skill *name* — never the `Skill` tool's `args`, the
  surrounding codex command text, or a command's `aggregated_output` (the file's contents). With the switch off the
  extractor never runs and none of the skill keys appear.

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
skill. The skill-trigger-rate dashboard widget (`get_skill_trigger_rates` + the "Skill trigger rates" panel — see the
[read model](#read-model-orchestratoranalyticsreadpy) and [dashboard](#dashboard-orchestratordashboardpy) sections
below) is a pure read-side addition over `extras JSONB` with no schema change.

### `repo_skill_catalog` records

`orchestrator/skill_catalog.py` appends one repo-level `event="repo_skill_catalog"` analytics record per tick per spec,
driven from `workflow.tick` after `_refresh_base_and_worktrees` has fetched `<remote_name>/<base_branch>`. It enumerates
the `SKILL.md` definitions the *target repo* carries on its base ref via `git -C <target_root> ls-tree -r --name-only
<remote_name>/<base_branch> .agents/skills .claude/skills`, keeps only direct `<root>/<name>/SKILL.md` definitions (a
`SKILL.md` nested deeper — e.g. `.claude/skills/.system/<name>/SKILL.md` — is ignored, matching the names-only
trigger anchor in `_usage_skills.py`), and dedupes by skill name across the two roots while preserving every
source path. The catalog is read from the target repo's base ref, never the orchestrator's own working tree, so
dashboard-local skill files are not scanned.

Each record carries `base_branch`, `remote_name`, `skills_available` (the sorted deduped skill names), and the optional
`skill_paths` (name → sorted source paths; dropped when empty). It is **not** issue-scoped, so its `issue` is the
sentinel `0` — the record still satisfies the `ts` / `repo` / `issue` / `event` envelope the sink and the Postgres
`analytics_events` schema require, and the four catalog fields all land in `extras JSONB` with **no DDL change**. The
whole producer is fail-open: a missing clone, an unfetched ref, a git error, or a sink IO failure logs and is swallowed
so catalog collection never disturbs the polling tick. An empty catalog still records `skills_available: []` (the
"scanned, found none" signal).

## Trajectory sink (`TRAJECTORY_LOG_PATH`)

A sibling, opt-in JSONL sink for agent *reasoning trajectories* — the ordered timeline of tool calls / results
interleaved with the assistant / user text turns, plus the final output a run produced — owned by the same
`orchestrator/analytics/` package and parsed at import alongside the analytics knobs. It is kept deliberately
**separate** from the analytics sink so the large free-text trajectory bodies never enter the numeric usage rollup, its
Postgres aggregation, or the dashboard.

**Producer: `record_agent_exit`.** After the baseline `agent_exit` analytics record (and the opt-in skill parse) are
produced, `record_agent_exit` calls `_maybe_record_trajectory`, which — only when `TRAJECTORY_LOG_PATH` is enabled —
parses the run's trajectory from the same stdout (`usage.parse_agent_trajectory`), redacts and truncates it, and appends
one `event="agent_trajectory"` record. `workflow._run_agent_tracked` forwards its orchestrator-built `prompt` so it can
land as the redacted `user_input`; `record_agent_exit` also threads through the `UsageMetrics` it already parsed for the
baseline record so the trajectory can carry a denormalized `run_usage` summary without a re-parse. The whole block rides
its **own** inner fail-open `try/except`: a parser, redactor, or sink failure logs (`log.exception`) and is swallowed,
so it can never drop the baseline `agent_exit` usage / cost record or the `skill_triggered` audit events, all of which
were already produced before it runs. With the sink off (the default) the block is a no-op before any parse work — the
prompt is never read into a record and the `agent_exit` shape is byte-for-byte unchanged. `main._run_tick` does not yet
call `prune_trajectory_records`, so trajectory retention stays operator-driven for now.

**Record shape.** One `agent_trajectory` record per tracked run carries the standard envelope (`ts`, `repo`, `issue`,
`event`, `stage`) plus correlation context (`agent_role`, `backend`, `session_id`, `review_round`, `retry_count`) and
the redacted trajectory: `user_input` (the orchestrator prompt), `system_prompt`, `tools` (the offered-tools set — read
from claude's stream, and for codex backfilled with the best-effort `skill_catalog.discover_codex_tools()` baseline
since its stream carries no offered-tools frame), `skills_triggered` / `skills_available` (names-only — for codex the
`skills_available` set is backfilled from the out-of-band `skill_catalog.discover_local_skills(cwd)` filesystem scan,
since its stream carries no
offered-skills catalog), a `run_usage` summary, a claude-only per-turn `turns`
array, an ordered `steps` array (each `{kind, name, tool_id, content}` plus a `turn` index on the billed steps, where
`kind` is `tool_call` / `tool_result` / `assistant_message` / `user_message` and `content` is the redacted tool input,
tool result, or text turn — `name` / `tool_id` are `null` on the message turns), and the final `output`. `run_usage`
is the denormalized `UsageMetrics` (`models`, `input_tokens`, `output_tokens`, `cached_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `turns` count, `cost_usd`, `cost_source`) minus `backend` (already on the record) — the run
headline, and the codex surface too, since codex has no per-turn detail. Each `turns[]` entry is one claude assistant
turn (`turn` index, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, and an
always-*estimated* `cost_usd` / `cost_source`); each billed `steps[]` entry (`assistant_message` / `tool_call`) carries
the same `turn` index tying it to its turn, while a `tool_result` / `user_message` step is a turn *input* and omits
`turn`. `build_record` drops every empty / `None` field, so an absent prompt, an empty system prompt, a no-trigger skill
set, or codex's empty per-turn array simply leaves its key off.

**Join keys.** The envelope and correlation context double as join keys back to the numeric sinks. `session_id` (the
live `result.session_id`) is the per-run key onto the [`agent_exit`](#agent_exit-records) analytics record and the
`agent_exit` audit event from the same run — both carry that same result id. The shared context `(repo, issue, stage,
agent_role, backend, review_round, retry_count)` lines up field-for-field with the analytics `agent_exit` record (the
audit events carry the same context under their own names, with backend as `agent`). The paired `agent_spawn` audit
event is **not** keyed by this `session_id`: its `session_id` is the *resume* session id, which is omitted entirely on a
fresh spawn and points at the prior session on a resume — so correlate the trajectory to the spawn through that shared
context, not `session_id`. Either way the heavy free-text trajectory body can be correlated back to the usage / cost /
token row for the same run without ever being stored alongside it — the whole point of keeping it in a separate file.

**Redaction and truncation caps.** Every free-text field — `user_input`, `system_prompt`, each step's `content`, and
`output` — is passed through `workflow_messages._redact_secrets` (the same secret-shaped-env-value masker used on
agent stderr) **before** truncation, so a secret straddling an elided boundary cannot survive as two halves. Each field
is then head/tail truncated to its first `_TRAJECTORY_FIELD_HEAD` (`2000`) and last `_TRAJECTORY_FIELD_TAIL` (`2000`)
characters with an `...[N chars elided]...` marker in between — the head keeps the request/intent, the tail the
result/answer. The whole record is additionally bounded: each step is charged its full **serialized** size — the JSON
metadata (`kind` / `name` / `tool_id` / `turn`) plus its truncated content, not just `len(content)`, so even thousands
of empty- or metadata-only steps still consume the budget — and the per-turn `turns` array is charged **and
truncated** against the same budget (turns drawn down first, then steps), so a pathological claude run of thousands of
turns with no tool calls cannot write the whole array in full and blow the budget. Once the running total crosses
`_TRAJECTORY_RECORD_BUDGET` (`200_000`) bytes the remaining turns — then steps — are dropped and a `truncated: true`
flag is set; only the small fixed `run_usage` summary is always kept whole, so one pathological run (thousands of turns
or tool calls) cannot write an unbounded line. Non-string step content (claude tool inputs are dicts; `tool_result`
content a list) is redacted **leaf-by-leaf before** JSON serialization (`_redact_tree`) — serializing first would
escape a multiline secret's newlines into `\n`, leaving the literal `str.replace` in `_redact_secrets` unable to match
the raw env value, so the secret would leak into the serialized content.

**Privacy contract — redaction is not anonymization.** The redactor masks only *secret-shaped* values: env vars whose
name is in the secret-key set or ends in a secret suffix, plus the resolved `GITHUB_TOKEN`, each verbatim occurrence
replaced with `***`. It deliberately does **not** strip issue or repository content. The prompt (`user_input`), the
`system_prompt`, every step's `content` in `steps` (tool inputs / results and the assistant / user text turns), and the
final `output` can — and routinely will — carry issue titles and bodies, quoted source from the worktree, file
paths, diffs, and the agent's own reasoning, all in cleartext after redaction. An enabled trajectory file therefore
carries the same sensitivity as the repositories the orchestrator works on; scope its filesystem permissions (and any
retention) accordingly. This is why the sink is off by default and why it never leaves the local filesystem (next
paragraphs).

**Opt-in, default off.** Unlike `ANALYTICS_LOG_PATH` (which defaults to `<LOG_DIR>/analytics.jsonl`),
`TRAJECTORY_LOG_PATH` defaults *off*: unset, empty, or `off` / `disabled` / `none` (case-insensitive) all disable it;
any other value is the explicit opt-in path. `TRAJECTORY_RETENTION_DAYS` defaults to `90` and mirrors
`ANALYTICS_RETENTION_DAYS` (non-positive keeps trajectories indefinitely).

**Append / prune discipline, dedicated lock.** `append_trajectory_record` reopens the file in append mode per record
after `mkdir(parents=True, exist_ok=True)`, downgrading `OSError` to a `log.warning`; `prune_trajectory_records(*,
now=None)` removes records older than `TRAJECTORY_RETENTION_DAYS` through a temp-file + `os.replace` rewrite, preserves
malformed / unparseable lines verbatim, and no-ops when the sink is disabled, retention is non-positive, or the file is
absent. Both reuse the shared append (`_recording`) and prune (`_retention`) cores but hold a **dedicated**
`threading.Lock`, so the trajectory file serializes its own append-vs-prune race without ever blocking against — or
touching — `ANALYTICS_LOG_PATH`, the analytics Postgres sync, or the dashboard.

**No built-in rotation.** As with the audit and analytics sinks, each append reopens the file after `mkdir`; there is no
size cap, long-lived descriptor, or compression. `prune_trajectory_records` is **not yet wired into the polling loop**,
so beyond the by-age prune (which only an in-process caller drives today) retention and rotation are entirely
operator-managed — pair `TRAJECTORY_LOG_PATH` with `logrotate` (or equivalent). Because every append re-resolves the
path, create/rename or `copytruncate` rotation is safe between writes.

**Local filesystem only.** A trajectory record is never written to `ANALYTICS_LOG_PATH`, never replayed into Postgres by
`analytics.sync` (the sync only reads `ANALYTICS_LOG_PATH`), and never surfaced in the **analytics** dashboard
(`orchestrator/dashboard.py`), which renders only the Postgres rollup. The sink is one JSONL file on local disk; the
only reader is the dedicated trajectory viewer below, which reads that file straight off disk.

**Observation-only, like every surface here.** The polling tick never reads the trajectory file back and no dispatch
decision keys off it; workflow state lives entirely in the pinned `<!--orchestrator-state ...-->` JSON comment and the
workflow label. The file is therefore safe to truncate, rotate, or delete at any time without affecting workflow state
or correctness.

### Trajectory operator workflow

There is no trajectory equivalent of `python -m orchestrator.analytics.sync`: trajectories are deliberately file-backed
only, and the analytics Postgres schema does not ingest their free-text bodies. To browse trajectories on another host,
mirror `TRAJECTORY_LOG_PATH` as a file and run the dedicated viewer on that host with `TRAJECTORY_LOG_PATH` pointing at
the mirrored JSONL. Scope the remote path like source code or issue content: redaction masks secret-shaped values, not
repository text or agent reasoning.

For an unattended deployment, mirror the file with SSH-based tooling such as `rsync`. Use a dedicated receiver account
whose key can only write into the trajectory directory. On an Ubuntu receiver, use a neutral shared directory such as
`/srv/orchestrator` instead of landing the file in the receiver user's home. That keeps `/home/forsync` out of the
dashboard read path and lets the Streamlit user read through a shared group:

```sh
# On the remote VPS.
sudo adduser --system --group --shell /bin/bash --home /home/forsync forsync
sudo groupadd -f orchestrator
sudo usermod -aG orchestrator forsync
sudo usermod -aG orchestrator <dashboard-user>
sudo mkdir -p /srv/orchestrator
sudo chown forsync:orchestrator /srv/orchestrator
sudo chmod 2750 /srv/orchestrator
sudo install -d -m 700 -o forsync -g forsync /home/forsync/.ssh

# Confirm rrsync is available; on current Ubuntu it is shipped by rsync.
command -v rrsync
```

Generate a dedicated cron key on the source host:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/forsync_ed25519 -C "trajectory-sync" -N ""
```

Then install the public key on the remote account as one `authorized_keys` line. Pick the network restriction that
matches the deployment:

- **Private overlay / Tailscale available.** Use the exact source host tailnet IP in `from=` when possible;
  `100.64.0.0/10` is the broader Tailscale CGNAT range. A tailnet ACL that allows only the source device to reach SSH on
  the VPS is stronger, with `from=` as defense-in-depth.
- **Public SSH / no Tailscale.** Use the source host's stable public egress IP or CIDR in `from=` instead. If the source
  IP is not stable, omit `from=` and restrict port 22 at the VPS firewall / cloud security group to the narrowest source
  range you can. Keep the forced `rrsync` command and `restrict` either way.

```text
command="/usr/bin/rrsync -wo -no-del /srv/orchestrator",restrict,from="<source-ip-or-cidr>" ssh-ed25519 AAAA... trajectory-sync
```

Lock the SSH account down further with `/etc/ssh/sshd_config.d/forsync.conf`:

```sshconfig
Match User forsync
    AuthenticationMethods publickey
    PasswordAuthentication no
    PermitTTY no
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
```

Validate and reload SSH:

```sh
sudo sshd -t
sudo systemctl reload ssh
```

A small source-side wrapper is easier to test than a heavily-quoted crontab line, lets cron fail fast when SSH would
otherwise prompt, and uses the same lock name as trajectory maintenance jobs so sync and prune never overlap each other.
With the `rrsync` root above, `DEST=forsync@<host>:trajectories.jsonl` lands at `/srv/orchestrator/trajectories.jsonl`
on the receiver. `rrsync` rejects absolute destination paths; keep the destination relative to its configured root:

```sh
#!/usr/bin/env bash
set -euo pipefail

SRC=/path/to/agent-orchestrator/logs/trajectories.jsonl
DEST=forsync@<host>:trajectories.jsonl
LOCK=/tmp/agent-orchestrator-trajectory.lock
KEY=/home/<local-user>/.ssh/forsync_ed25519

SSH_CMD="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new"

echo "=== $(date -Is) trajectory sync start ==="
if /usr/bin/flock -n -E 75 "$LOCK" \
  /usr/bin/rsync -az --timeout=120 --chmod=F640 \
    -e "$SSH_CMD" \
    "$SRC" "$DEST"; then
  echo "=== $(date -Is) trajectory sync done ==="
else
  rc=$?
  if [ "$rc" -eq 75 ]; then
    echo "=== $(date -Is) trajectory sync skipped: lock held ==="
    exit 0
  fi
  exit "$rc"
fi
```

Install it as executable before adding the cron entry:

```sh
chmod +x /path/to/agent-orchestrator/bin/sync-trajectories.sh
```

```cron
10 * * * * /path/to/agent-orchestrator/bin/sync-trajectories.sh >> /path/to/agent-orchestrator/logs/trajectory-sync.cron.log 2>&1
```

- `rsync` is a file mirror, not an append-only archive. When local retention later rewrites or shrinks the JSONL, a
  later mirror run will make the remote file match.
- The default `rsync` destination update path already writes a temporary file and renames it into place for this
  single-file mirror; avoid `--inplace`.
- Do not use `--append` or `--append-verify` for this mirror: retention pruning can shrink or rewrite the source file,
  and append-mode transfer would leave stale remote tail data or give remote readers partial in-place writes.
- `StrictHostKeyChecking=accept-new` is convenient on a trusted private network because the first cron-run pins the host
  key and later key changes still fail. The stricter alternative is to pre-seed once with `ssh-keyscan -H <host> >>
  ~/.ssh/known_hosts` and drop that option.
- `--chmod=F640` makes the mirrored file readable by the receiver owner and the shared `orchestrator` group. The setgid
  bit on `/srv/orchestrator` (`2750`) keeps replaced files in that group, so the dashboard user can read them after a
  fresh login / restarted service picks up its new group membership.
- If you migrate from an older `/home/forsync/...` landing path, move the existing file once (`sudo mv
  /home/forsync/agent-orchestrator/trajectories.jsonl /srv/orchestrator/`) and then apply `sudo chgrp orchestrator
  /srv/orchestrator/trajectories.jsonl && sudo chmod 640 /srv/orchestrator/trajectories.jsonl`.
- Verify dashboard readability before launching Streamlit: `id` for the dashboard user must show `orchestrator`, and
  `sudo -u <dashboard-user> head /srv/orchestrator/trajectories.jsonl` should print JSONL.
- If the remote host should keep a longer archive than the local machine, mirror to dated snapshots instead of one fixed
  destination path.
- Treat the remote SSH key as sensitive. For a write-only receiver, constrain it in `authorized_keys` with a forced
  `rrsync` command, no PTY / forwarding, and either a `from=` source restriction or network-level SSH allowlist.
- Rotate `trajectory-sync.cron.log`, or send the wrapper output to the journal with `logger`, so the cron log does not
  grow forever.

The mirror cron does not lock the source file against the running orchestrator. A record that is fully appended before
`rsync` reads the file is copied; a record appended during or after the transfer may be absent until the next mirror
run. If `rsync` ever catches a final line mid-write, the remote file may briefly end with a malformed JSON line after
the destination rename; the trajectory reader skips malformed lines, and the next mirror run repairs the fixed
destination because this command mirrors the whole file rather than using `--append`.

Decide whether the remote file is a **mirror** or an **archive** before enabling retention. A fixed destination path is
a mirror: after local retention prunes old records, the next sync shrinks the remote file too. That is correct for a
remote viewer that should show only the retained window, but wrong if the remote host is meant to preserve history
before the local file is pruned. For an archive, use a different strategy, such as dated snapshots, a never-pruned local
archive file, or a custom high-water-mark shipper.

Because `prune_trajectory_records()` is not called by the polling loop, drive trajectory retention explicitly when you
want `TRAJECTORY_RETENTION_DAYS` to affect the file. The value may live in `.env` like the other non-secret knobs; it is
parsed when the prune process imports `orchestrator.analytics`. The cron entry below relies on `.env` for both
`TRAJECTORY_LOG_PATH` and `TRAJECTORY_RETENTION_DAYS`, runs the prune helper, and logs how many records were removed:

```cron
25 0 * * * cd /path/to/agent-orchestrator && /usr/bin/flock -n -E 75 /tmp/agent-orchestrator-trajectory.lock /home/<user>/.local/bin/uv run python -c 'from orchestrator import analytics; print(f"trajectory prune removed {analytics.prune_trajectory_records()} record(s)")' >> /path/to/agent-orchestrator/logs/trajectory-prune.cron.log 2>&1
```

To make the same cron entry use a one-off retention window instead of `.env`, prefix the command with `env
TRAJECTORY_LOG_PATH=/path/to/agent-orchestrator/logs/trajectories.jsonl TRAJECTORY_RETENTION_DAYS=30`.

Only run this prune command while the orchestrator is stopped or otherwise guaranteed not to append trajectories. The
shared `/tmp/agent-orchestrator-trajectory.lock` serializes operator cron jobs with each other, but not with the live
orchestrator process: the append/prune lock in `orchestrator.analytics` is a process-local `threading.Lock`, not an
interprocess file lock. An external prune process can race with the live polling process and lose a record appended to
the old inode between the prune read and `os.replace`. Schedule pruning after at least one mirror run if the remote
fixed-path mirror should receive records before they age out locally. The prune rewrites only the trajectory JSONL
through the same temp-file + `os.replace` path described above; it never touches GitHub workflow state,
`ANALYTICS_LOG_PATH`, Postgres, or the analytics dashboard.

### Trajectory viewer (`orchestrator/trajectory_dashboard.py`)

A deliberately **separate** Streamlit page from the analytics dashboard, launched the same way (`uv run streamlit run
orchestrator/trajectory_dashboard.py`, opt-in `dashboard` group). The two pages stay apart on purpose: the analytics
dashboard reads the numeric usage / cost rollup from Postgres, while the viewer reads the JSONL trajectory file
**directly** — the trajectory bodies are never in Postgres — so an operator can browse trajectories with nothing but
the file on disk (no database, no `analytics.sync`).

**Read model (`orchestrator/trajectory_reader.py`).** A pure, import-light, Streamlit-free reader (the file-backed
analogue of `orchestrator/analytics/read.py`). The record and view dataclasses (`TrajectoryRun` and its
`TrajectoryStepView` / `TimelineEntry` / `TurnUsageView` / `RunUsageView` sub-views), the log-path resolution, and the
defensive JSONL parsing / reading pipeline live in the private `orchestrator/_trajectory_records.py` leaf and are
re-exported from `trajectory_reader` under their original names; `trajectory_reader` itself owns the free-text filtering
and the filter-option / summary aggregation. Together they read `TRAJECTORY_LOG_PATH`, parse each `agent_trajectory`
record into a frozen `TrajectoryRun` (with a normalised `TrajectoryStepView` per step), and expose `read_trajectories`
(newest first by `ts`, file order as the tie-break), `filter_options`, `filter_runs` (repo / backend / agent-role /
stage / issue / free-text-search, every filter conjunctive and an empty multi-value meaning "no constraint", plus an
opt-in `exclude_fixtures`), and `summarize`. Each run exposes a normalised, vintage-agnostic `timeline` — the leading
`user_input` prompt, then the ordered `steps[]`, then the final `output`, as one ordered `TimelineEntry` sequence — so
an old steps-only record (only `tool_call` / `tool_result` steps) and a new record whose steps interleave
`assistant_message` / `user_message` text turns render the same way; `tool_calls` still counts only `tool_call` steps,
so the text turns never inflate the tally. `is_fixture` flags the synthetic test-suite records an inherited file may
carry (the sentinel prompt `ignored`, a `sess-*` session id, or a `Skill`-only run), which
`filter_runs(exclude_fixtures=True)` drops. Each run also exposes the record's usage: a `run_usage` (`RunUsageView`) run
summary and a claude-only per-turn `turns` tuple (`TurnUsageView`), with convenience accessors `model` (first of
`run_usage.models`), `cost_usd` / `cost_source` (the authoritative run figure), `total_tokens`, and an O(1)
`usage_for_turn(idx)` lookup so a `TimelineEntry` (which now carries the producing step's `turn` index) can find its
turn's usage while walking the timeline; `summarize` adds `total_cost_usd`, the summed run cost over runs that recorded
one. A pre-usage record parses with `run_usage=None`, `turns=()`, and every `step.turn=None`, so it renders exactly as
before. The same resilience contract the rest of the codebase honours holds: a missing / disabled path, a malformed
line, a non-`agent_trajectory` record, or a renamed field yields a smaller result, never an exception. The records are
already redacted and truncated by the sink, so the viewer is a read-only window onto an already-sanitised file — it
adds no redaction of its own and must be scoped (filesystem permissions, who can reach the Streamlit port) with the same
care as the trajectory file itself.

**Page (`orchestrator/trajectory_dashboard.py`).** Reuses the analytics dashboard's `orchestrator/dashboard_theme.py`
chrome (CSS variables, fonts, `fmt_*` formatters) so the two pages read as one family, and reuses
`dashboard_state.parse_issue_number` for the issue filter. Streamlit is imported lazily inside `main()` and the
repo-root `sys.path` shim comes from the shared `orchestrator/script_launch.py` helper (`ensure_repo_root_on_path`)
that `orchestrator/dashboard.py` also calls, so importing the module (or the polling tick) never needs the
`dashboard` group — `tests/test_trajectory_dashboard.py` guards both the lazy-import and the script-launch
`sys.path` shape. The layout is intentionally minimal-but-useful: a sidebar of filters (plus a *Hide synthetic fixtures*
toggle that drives the reader's `exclude_fixtures`, off by default), a topbar + five-tile KPI strip (runs / issues /
repos / tool calls / total cost, the last summed from `summarize`'s `total_cost_usd`), a foldable *Recorded runs*
overview table (capped at the 200 most recent; collapse the expander to focus on a single run), three cascading run
pickers (repo → issue → the run's `detail_label` cohort — stage/role · backend · round · timestamp) that
together still reach every match, and a per-run detail card that lists the offered tools and triggered / available
skills, a run-level usage / cost row (model(s), token buckets, turn count, and the authoritative run cost tagged with
its `cost_source` — the codex surface too), then walks the run's normalised `timeline` as one ordered sequence — the
redacted prompt, then the interleaved assistant / user text turns and tool calls / results (each rendered by its
`kind`), then the final output (rendered as markdown; every other entry is shown verbatim in a code block). For a claude
run, a compact per-turn usage strip (model · in / out tokens · cache-read / cache-write · estimated cost, with a
*cache hit* chip when the turn read from cache) is drawn at each assistant-turn boundary in the timeline; the copy
states that per-turn figures are claude-only estimates that need not sum to the authoritative run total, and that
entries without a strip (tool results, user turns) are turn inputs billed on the next turn. A pre-usage record carries
no usage, so the row and strips are absent and it renders exactly as before. The fixtures `is_fixture` flags are tagged
in the overview table and the run-level picker (the `[fixture]` prefix rides the run option; and the detail card carries
a notice) so the operator can tell the inherited test-suite records from real runs even with the toggle off. When the
sink is off it renders the opt-in banner and stops; an empty file or an empty filter set renders an explanatory notice
rather than a blank page. The page's pure inline-HTML builders — the topbar, the five-tile KPI strip, the run cards,
and the per-turn usage strips, each a string builder over plain values or `trajectory_reader` view objects that reuses
the `dashboard_theme` chrome — live in the Streamlit-free `orchestrator/_trajectory_dashboard_html.py` leaf, so
`trajectory_dashboard.py` holds the Streamlit `_render_*` calls, the sidebar / run-picker and page-load helpers, the
page-state dataclasses, and `main()`, but none of the pure HTML string-building; the leaf never imports the page module
back, keeping the dependency one-directional.

## Analytics database (`analytics-db/`)

Local Postgres service that is the aggregation target for the JSONL sink. The service contract and schema are
operator-deployed via Docker compose; the JSONL→Postgres replay is implemented in `orchestrator/analytics/sync.py` as
an operator-driven CLI — NOT wired into the polling tick. Orchestrator correctness must not depend on database
availability.

### Service layout

[`../analytics-db/compose.yml`](../analytics-db/compose.yml) brings up a single `postgres:16` container with the data
directory on a host bind (`./data`, gitignored) and the init directory mounted read-only. The port binding is pinned to
`127.0.0.1` so the database is unreachable off-host regardless of firewall configuration; re-binding to `0.0.0.0` is
intentionally a code change rather than an env-var change. Credentials default to `orchestrator` / `orchestrator` and
are overridable via `analytics-db/.env` (`POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_PORT`).
`docker compose` reads `.env` from the compose-file directory, not the orchestrator root.

```sh
cd analytics-db
docker compose up -d                  # start the local service (data lives in ./data, gitignored)
docker compose down                   # stop the container; data on the ./data bind mount is preserved
docker compose down && rm -rf ./data  # stop and wipe history (the bind is a host directory, so `down -v` does NOT remove it)
```

To apply or re-apply the schema against an already-running compose service:

```sh
cd analytics-db
docker compose exec -T analytics-db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/01-schema.sql'
```

### Endpoint shape

The sync reads a single libpq URL — `ANALYTICS_DB_URL` (default unset, example
`postgresql://orchestrator:orchestrator@127.0.0.1:5432/orchestrator_analytics`) — rather than separate host / port /
user / password variables. Moving the database off-host later (managed Postgres, a different VM, a unix socket) is a
one-line repoint. Empty value and the sentinels `off` / `disabled` / `none` (case-insensitive) disable the sync,
matching `ANALYTICS_LOG_PATH`.

### Schema

[`../analytics-db/init/01-schema.sql`](../analytics-db/init/01-schema.sql) defines:

- **`analytics_events` table.** Columns mirror the JSONL record shape produced by `analytics.build_record`. `ts`,
  `repo`, `issue`, `event` are `NOT NULL`; everything else is nullable so any record across the three event kinds is a
  valid row. An `extras JSONB` column captures any field added to `build_record` before the DDL knows about it — the
  opt-in skill fields (`skills_triggered` / `skills_triggered_count` / `skills_available`, the per-load
  `skills_evidence` tier map, and the `skills_incidental` / `skills_incidental_count` path-only references) are exactly
  such additions, so they need **zero DDL**: an operator-deployed database ingests them the moment
  `TRACK_SKILL_TRIGGERS` is enabled, with no migration and no schema reapply. `source_path` / `source_line` are forensic
  context; the authoritative dedup key is `content_hash` — SHA-256 over the canonical (`sort_keys=True`) JSON form of
  the record.
- **Indexes.** A plain (non-partial) unique index on `content_hash` plus `INSERT ... ON CONFLICT (content_hash) DO
  NOTHING` makes repeated sync runs idempotent. Additional indexes cover the expected query dimensions: `ts`; `(event,
  ts)`; `(repo, issue)`; a partial index on non-null `stage`; per-event-kind partial indexes on `(repo, ts DESC)` for
  `event='agent_exit'` and `event='stage_enter'`; and a composite `(event, repo, stage, ts)` index.
- **`analytics_daily_rollup` materialized view.** Keyed on `(day, repo, issue, event, stage, backend, cost_source)` and
  carrying the aggregates the dashboard's window-bounded widgets need without re-scanning `analytics_events`: token
  totals (`total_input_tokens`, `total_output_tokens`, `total_cached_tokens`, `total_cache_read_tokens`,
  `total_cache_write_tokens`), `total_cost_usd`, `duration_s_sum` + `duration_s_count` (so consumers recover
  `AVG(duration_s)` as `sum / count`), `failed_count` (rows with non-NULL non-zero `exit_code`), `timed_out_count`
  (scoped to `event='agent_exit'` with `timed_out=TRUE`), and `event_count`. `day` is `(ts AT TIME ZONE 'UTC')::date`. A
  unique index on the full key (`NULLS NOT DISTINCT`, Postgres 15+) backs the rollup; a `(day, repo)` supporting index
  keeps `WHERE day BETWEEN x AND y` predicates on a range scan.
- **`analytics_agent_runs` view.** `CREATE OR REPLACE VIEW` over `event = 'agent_exit'` rows that promotes derivations:
  `model` from `COALESCE(models->>0, 'unknown')`, `total_tokens` = `input + output`, `total_cache_tokens` = `cached +
  cache_read + cache_write`, a categorical `review_round_bucket` (`0`, `1`, `2`, `3-5`, `6+`), `failed = exit_code <> 0`
  (NULL preserved), and `has_cost = cost_usd IS NOT NULL` (true for `cost_source` in {`reported`, `estimated`}). Raw
  nullable columns pass through alongside derived ones; `cost_source` passes through verbatim.

The init script runs once when the data volume is empty. `IF NOT EXISTS` guards plus trailing `ALTER TABLE ... ADD
COLUMN IF NOT EXISTS` / `CREATE UNIQUE INDEX IF NOT EXISTS` for `content_hash` keep it idempotent for the
operator-driven case (`psql -f` against an existing instance) and migrate a pre-`content_hash` data volume without
dropping data. MV column changes require `DROP MATERIALIZED VIEW analytics_daily_rollup` followed by a reapply; the
sync's refresh hook does NOT recover from a column mismatch.

### Sync CLI (`orchestrator/analytics/sync.py`)

Run on demand:

```sh
uv run python -m orchestrator.analytics.sync                                                # uses configured env vars
uv run python -m orchestrator.analytics.sync --log-path /path/to/rotated.jsonl --db-url postgresql://other/db
```

**Batched inserts.** Reads `ANALYTICS_LOG_PATH` line by line, accumulates validated row tuples into a
`_BATCH_SIZE`-sized buffer (default 500), and flushes each full batch via `cur.executemany("INSERT ... ON CONFLICT
(content_hash) DO NOTHING", batch)`. A multi-thousand-record replay pays one Postgres round-trip per batch instead of
one per row. A final partial batch is flushed at EOF so the tail still lands.

**Pre-check dedup.** Before opening the input file the sync issues a single `SELECT content_hash FROM analytics_events
WHERE content_hash IS NOT NULL` and pulls the result into a Python set, so already-present rows are filtered out before
they enter the batch. Newly queued hashes are added to the same set as the loop iterates, so two identical records
inside one JSONL file are deduped against each other before reaching `executemany`. The pre-check reads from the unique
`analytics_events_content_hash_idx`. The server-side `ON CONFLICT (content_hash) DO NOTHING` arbiter stays the
authoritative dedup backstop for racing concurrent writers.

**Counters.** Per-batch `cur.rowcount` drives the cumulative `inserted` / `skipped_duplicate` totals. Duplicates =
`len(batch) - rowcount` for wire-side skips, plus the pre-skip counter for in-Python skips.

**Malformed-line tolerance.** Blank lines are silently skipped; lines that are not valid JSON, JSON that is not an
object, records missing one of the required (`ts` / `repo` / `issue` / `event`) keys, or carrying an unparseable `ts`
are counted as skipped and logged but never enter the batch buffer. The JSONL file is treated as read-only — the sync
never rewrites or truncates it, even when it sees malformed lines. Naive timestamps are interpreted as UTC.

**Transaction shape.** A `psycopg` driver-level error inside a batch flush rolls the transaction back and propagates so
the CLI exits non-zero rather than reporting "success" on a half-inserted run. After the insert transaction commits, the
sync issues `REFRESH MATERIALIZED VIEW analytics_daily_rollup` (non-concurrent) and commits again so the rollup-backed
dashboard widgets catch up. The refresh fires unconditionally on every successful commit — including all-duplicates
and all-malformed runs — so rerunning the sync is the documented recovery path for a stale rollup. A refresh exception
(MV not migrated yet, transient Postgres error, lock-wait timeout) is logged via `log.exception` and swallowed; the
committed inserts are durable, and the next sync's refresh recovers the rollup.

**No-op modes.** `sync_jsonl_to_postgres` is a no-op (no connection attempt, no row insertion, no error) when
`ANALYTICS_DB_URL` is unset or disabled, when `ANALYTICS_LOG_PATH` is explicitly disabled (note that the env var
defaults to `LOG_DIR/analytics.jsonl`, so only the empty value or `off` / `disabled` / `none` turns it off), or when the
JSONL file is absent. The CLI is safe to schedule before the operator deploys Postgres. The driver is `psycopg[binary]`;
the import is lazy inside the connect helper so the module load path remains driver-free for callers that only need
`SyncResult`.

**Row mapping.** The pure record → DB-row mapping — the promoted-column / JSONB / required-key schema, the
canonical-JSON `content_hash` dedup key, and the per-record validation that promotes known columns, routes the rest to
`extras`, and turns a validated record into the positional INSERT tuple — lives in the driver-free (stdlib + typing, no
psycopg) `orchestrator/analytics/_sync_rows.py` leaf; `sync.py` owns the connection lifecycle, batching, rollup refresh,
and CLI and imports the mapping layer one-directionally.

### Operator feedback

The sync surfaces feedback through the module logger and the stdout summary:

- Every log line is timestamped (UTC, with an explicit `UTC` suffix) via `_configure_cli_logging`'s `%(asctime)s`
  formatter and `formatter.converter = time.gmtime`.
- A `connecting to <redacted-url>` / `connection established` pair brackets the connect call so a remote-Postgres
  reachability problem surfaces immediately.
- A `progress lines=N inserted=… duplicate=… malformed=… elapsed=…s` record drops after each batched
  `executemany` flush (`_BATCH_SIZE` and `_PROGRESS_INTERVAL` are both 500, so each flush carries one progress line).
- A final `completed in %.3fs (…)` line carries the wall-clock total.
- The CLI prints a UTC-stamped stdout summary at the end carrying `inserted=` / `duplicate=` / `malformed=` /
  `total_lines=` / `duration_s=`.
- `ANALYTICS_DB_URL` credentials are stripped before logging — both the `user:password@` netloc form and the libpq
  query-string form (`?user=`, `?password=`, `?sslpassword=`, `?passfile=`, case-insensitive per libpq parameter-name
  rules) collapse to `***`.

### Operator workflow

Run `uv run python -m orchestrator.analytics.sync` on whatever cadence you prefer; `--log-path` and `--db-url` override
the env values for one-off replays of archived JSONL files. The default cadence is operator-chosen because the JSONL
sink is already the authoritative analytics surface on disk — the database is for aggregation and reporting, not
durability.

For an unattended deployment, drive the sync from `cron`. A typical entry runs hourly, guards against overlap with
`flock`, and captures output:

```cron
00 * * * * cd /path/to/agent-orchestrator && /usr/bin/flock -n /tmp/agent-orchestrator-analytics-sync.lock /home/<user>/.local/bin/uv run python -m orchestrator.analytics.sync --log-path /path/to/agent-orchestrator/logs/analytics.jsonl --db-url 'postgresql://<user>:<password>@<host>:<port>/<database>' >> /path/to/agent-orchestrator/logs/analytics-sync.cron.log 2>&1
```

- `cd /path/to/agent-orchestrator` so `uv run` finds the project's `pyproject.toml`.
- Absolute `/home/<user>/.local/bin/uv` because cron's `PATH` does not include `~/.local/bin`.
- `flock -n` makes the run a no-op when a previous invocation is still holding the lock, so a long replay never overlaps
  with the next tick.
- `--log-path` and `--db-url` are explicit CLI overrides, so the cron entry does not depend on `.env` being loadable
  from cron's environment.
- `>> ...analytics-sync.cron.log 2>&1` keeps stdout and stderr in the project log area instead of routing failures to
  local `mail`.

### Read model (`orchestrator/analytics/read.py`)

Thin, testable data-access layer over `analytics_events`, the `analytics_agent_runs` view, and the
`analytics_daily_rollup` materialized view. The dashboard's window-bounded aggregates read from the rollup; per-row
drill-downs and widgets the rollup cannot reconstruct exactly stay on the base table or the agent-run view. The module
is Streamlit-free so the read path can be wired into any UI.

`read.py` is the public facade: it owns no query helpers of its own and re-exports everything below so the
`orchestrator.analytics.read` import surface every caller already depends on is unchanged. The reader functions are
split across three focused sibling modules by data source / shape — `read_raw.py` (foundational readers over
`analytics_events` / the agent-run view: `get_filter_options`, `get_data_extent`, `get_event_breakdown`,
`get_recent_agent_exits`, `get_issues`, `get_issue_events`), `read_rollup.py` (the `analytics_daily_rollup`-backed
aggregates: `get_summary`, `get_kpi_prev`, `get_time_series`, `get_stage_breakdown`, `get_backend_efficiency`,
`get_repo_breakdown`, `get_throughput_breakdown`), and `read_dashboard.py` (the redesigned-dashboard chart breakdowns
the rollup cannot reconstruct: `get_review_round_breakdown`, `get_skill_trigger_rates`, `get_skill_trigger_matrix`,
`get_skill_adoption`, `get_cost_coverage`, `get_backend_daily_tokens`, `get_hourly_heatmap`). The supporting plumbing is
split into further
sibling modules — `read_models.py` (the frozen read-model dataclasses), `connection.py` (`AnalyticsReadError`, the
deferred-psycopg connect factories, and the thread-local connection cache behind `analytics_connection` /
`close_thread_local_connection`), `db_url.py` (`_resolve_db_url`), `query.py` (`_query`), and `predicates.py` (the
window / filter `WHERE`-clause builders).

- `get_summary` (rollup) — date-bounded totals + per-event / per-stage breakdowns + token / cost sums, plus
  `total_agent_runs` / `failed_agent_runs` / `timed_out_agent_runs` scoped to `event='agent_exit'`. `distinct_issues` is
  `COUNT(DISTINCT (repo, issue))`. Single round-trip via `WITH win AS (...)` CTE with three `UNION ALL` branches tagged
  by a `kind` discriminator.
- `get_kpi_prev` (rollup) — stripped variant of `get_summary` returning only the cost / token / agent-run scalars the
  dashboard reads off `prev_summary` for KPI deltas. Skips the `COUNT(DISTINCT)`s and `GROUP BY` follow-ups; ~one
  aggregate scan instead of three.
- `get_time_series` (rollup) — daily `(day, event, count)` rollups with per-cell cost / input / output / cache_read /
  cache_write token aggregates.
- `get_stage_breakdown` (rollup) — per-stage counts + weighted `AVG(duration_s)` recovered as `SUM(duration_s_sum) /
  NULLIF(SUM(duration_s_count), 0)`, rolled-up cost / token totals, and a `runs` agent-exit subset count. The total cost
  is further split into cache vs no-cache (`cache_cost_usd` + `no_cache_cost_usd`); each rollup row's `total_cost_usd`
  is weighted by `(total_cached_tokens + total_cache_read_tokens + total_cache_write_tokens) / (total_input_tokens +
  total_output_tokens + total_cache_read_tokens + total_cache_write_tokens)` into the cache stack and the complement
  into no-cache. `total_cached_tokens` is the Codex "portion of input served from cache" counter and is already inside
  `total_input_tokens`, so it stays out of the denominator to avoid double-counting. Token-less rollup rows attribute
  their full cost to no-cache.
- `get_repo_breakdown` (rollup) — per-`repo` rollup of issues / events / agent-exits / cost.
- `get_backend_efficiency` (rollup) — per-backend runs / failed / avg duration / cost / token totals with NULL
  backends surfaced as `"unknown"`. `event = 'agent_exit'` is pinned in the WHERE clause.
- `get_throughput_breakdown` (rollup) — daily resolved / rejected counts over `stage_enter` rows whose `stage` is
  `done` or `rejected`. Short-circuits when the events multiselect excludes `stage_enter` or the stages selection
  excludes both terminals.
- `get_filter_options` (base table) — distinct repos / events / stages / backends / agent_roles for dropdowns. All
  five columns pulled in a single `UNION`'d round-trip with rows tagged by their column.
- `get_data_extent` (base table) — min / max `ts` so the sidebar date picker defaults to a window that contains rows.
- `get_event_breakdown` (base table) — per-event counts (the rollup pre-aggregates more finely than `event` alone, so
  the base-table read is cheaper here).
- `get_recent_agent_exits` (base table) — newest rows filtered to `event='agent_exit'`.
- `get_skill_trigger_rates` (base table) — per-`(agent_role, backend)` skill-trigger aggregate: `runs`, `skill_runs`
  (rows whose `extras` carries a `skills_triggered` key), and `total_triggers` (`SUM` of `skills_triggered_count`), with
  a derived `rate` property. Reads the base table because the skill fields live in `extras JSONB`, which the rollup does
  not carry — no DDL. `event = 'agent_exit'` is pinned and the agent-exit event-filter short-circuit applies. NULL
  `agent_role` / `backend` bucket under `"unknown"`. A `0` rate is a real "no trigger observed" signal but cannot tell a
  tracked-but-quiet run from one whose `TRACK_SKILL_TRIGGERS` was off.
- `get_skill_trigger_matrix` (base table) — per-skill × `(repo, agent_role, backend)` trigger-run matrix. Two
  base-table reads combined in Python: the `repo_skill_catalog` records (the `skills_available` universe a repo offers;
  date/repo-filtered only, since those records are repo-level with `issue = 0` / NULL stage) and the filtered
  `agent_exit` rows (each run's `skills_triggered` list). Each cell carries `skill_runs` (runs *containing* the skill,
  one per run per distinct name — not total invocations) and `runs` (the total agent-exit runs in the cell's cohort,
  so a low/zero trigger count reads against the cohort size). Every catalog skill is zero-padded across the cohorts
  observed for its repo so the matrix carries explicit "offered but never triggered" cells (e.g. `developer / claude /
  review`, `skill_runs = 0`); with the catalog missing it degrades to just the observed-trigger cells. decomposer /
  question cohorts get the same catalog-backed zero rows as developer / reviewer whenever they have agent-exit runs.
  Rows are ordered by `skill_runs` DESC, then cohort `runs` DESC, then a stable `(repo, agent_role, backend, skill)`
  tiebreak, and the list is capped at `limit` rows (default `SKILL_MATRIX_ROW_LIMIT` = 100; a non-positive `limit`
  disables the cap). The agent-exit event-filter short-circuit applies (no catalog read either). NULL `agent_role` /
  `backend` bucket under `"unknown"`. Same `extras JSONB` / no-DDL and `TRACK_SKILL_TRIGGERS`-off caveats as
  `get_skill_trigger_rates`.
- `get_skill_adoption` (base table) — per-skill × `(repo, agent_role, backend)` adoption aggregated by **logical**
  agent session rather than by raw agent run, so a resume chain that pulled `develop` across several ticks counts as one
  adopting session, not several. Two `agent_exit` base-table scans combine in Python. The first applies the full
  reporting-window filters and selects the *active* sessions plus the window-scoped diagnostics; the second reads each
  active session's evidence from every `agent_exit` row *before the window end*, deliberately dropping the window start
  and the stage filter (`_WindowFilters.historical_scope`) so a load from a prior stage or from before the window stays
  visible, while the retained `end` bound stops a later load from leaking backward. A session is keyed by
  `resume_session_id`, then `session_id`, then the row's primary key (an ID-less row is its own session, never merged
  into one anonymous bucket). `sessions` is the denominator — sessions in the cohort with the skill available (its
  `skills_available` listed it, or a legacy load with the `skills_available` key absent implied it — an explicit empty
  set does not) — and `adopted` counts the sessions that loaded it, once per session, with a derived `adoption_rate`.
  `invocations` is the cohort's window `agent_exit` run count (every run, so a low `load_rows` reads against it);
  `load_rows` counts the window runs that loaded the skill and `incidental` the window runs that referenced it without
  loading. All three are window-scoped, so a pre-window load counts toward `adopted` but not toward them. Rows are
  ordered by `sessions` DESC, then `adopted` DESC, then
  `invocations` DESC, then a stable `(repo, agent_role, backend, skill)` tiebreak, and the list is capped at `limit`
  (default `SKILL_ADOPTION_ROW_LIMIT` = 100; a non-positive `limit` disables the cap). The agent-exit event-filter
  short-circuit (no scans at all), NULL `"unknown"` bucketing, and `extras JSONB` / no-DDL / `TRACK_SKILL_TRIGGERS`-off
  caveats match `get_skill_trigger_matrix`.
- `get_issues` (base table) — date / repo-bounded one-row-per-`(repo, issue)` overview: event count, first / last
  activity, latest non-null stage, agent-exit count, cost / token totals, `max_review_round`, `failed_agent_runs`,
  `max_retry_count`. Bounded by `limit` and ordered by `sort_by` (`"last_seen"` default, `"cost"` orders by
  `SUM(cost_usd) DESC NULLS LAST`; unknown `sort_by` raises `ValueError`).
- `get_issue_events` (base table) — full event trace for a single `(repo, issue)` pair, oldest first.
- `get_hourly_heatmap` (base table) — 7×24 weekday/hour activity cells from `EXTRACT(DOW)` / `EXTRACT(HOUR)` over
  `(ts AT TIME ZONE 'UTC') + tz_offset_hours * INTERVAL '1 hour'` (normalizing first guards against a non-UTC session
  timezone re-shifting the buckets) with per-cell event count + `input + output + cache_read + cache_write` token total.
  `tz_offset_hours` (default `0`, parameter binding only — never spliced) lets the dashboard bucket in a non-UTC zone.
- `get_review_round_breakdown` (agent-run view) — per `review_round_bucket` runs / failed counts + `total_cost_usd`,
  plus per-role (`developer_*` / `reviewer_*`) run counts and cost, each role's cost further split into cache vs
  no-cache (`*_cache_cost_usd` + `*_no_cache_cost_usd`). The split is proportional: each run's cost is weighted by
  `(cached_tokens + cache_read_tokens + cache_write_tokens) / (input_tokens + output_tokens + cache_read_tokens +
  cache_write_tokens)` into the cache stack and the complement into no-cache. `cached_tokens` is the Codex "portion of
  input served from cache" counter and is already inside `input_tokens`, so it stays out of the denominator to avoid
  double-counting. Token-less rows attribute their full cost to no-cache. NULL buckets surface as `"unknown"`.
- `get_backend_daily_tokens` (agent-run view) — per `(day, backend)` token totals feeding the hero chart's "By
  backend" stacked-area toggle.
- `get_cost_coverage` (agent-run view) — per `cost_source` rollups carrying both runs and `total_tokens`. The
  `unknown-price` cohort is exposed verbatim (never collapsed into a generic "unknown") because it is the maintenance
  signal for the pricing table in `orchestrator.usage`. NULL `cost_source` buckets under `"unknown"`.

**Filter contract.** The agent-run view has no `event` column (its WHERE `event = 'agent_exit'` is baked in), so
view-backed functions cannot push an `event IN (...)` clause down. They honor the dashboard's event-filter contract by
short-circuiting to empty when the operator's events selection excludes `agent_exit` (or is cleared). Rollup readers
preserve the same contract through `_build_rollup_window_where`, which emits a tautologically-false predicate on a
cleared multiselect and a parameterised `IN (...)` on a non-empty one.

The rollup window helper translates the dashboard's midnight-aligned UTC `[start, end)` datetimes to `day >=
start.date() AND day < end.date()` predicates so the `(day, repo)` index drives a date-range scan. Sub-day-aligned
bounds collapse to day granularity (the rollup carries no finer resolution), but the dashboard never passes those.

**Connection model.** Each function returns a frozen dataclass or list of dataclasses. `ANALYTICS_DB_URL` unset
short-circuits every function to an empty / zero-valued result with no connection attempt, mirroring the sync's no-op
contract. Connection or query failures (driver-level psycopg errors, schema mismatches, network unreachable) are wrapped
in a single `AnalyticsReadError` whose `__cause__` preserves the underlying exception. The psycopg import is deferred to
call time inside `_default_connect`; tests inject a fake `connect(db_url) -> connection` factory.

Every public reader accepts an optional `conn=` so a caller (typically the dashboard, inside an `analytics_connection`
scope) can run many reads on a single shared connection instead of paying the ~1 s psycopg handshake per call; absent
`conn=`, the open-per-call / close-in-`finally` path runs unchanged. A caller-supplied `conn=` always wins over the URL
short-circuit.

`analytics_connection(*, db_url=None, connect=None)` is a context manager that maintains a single thread-local
persistent connection. The first `with` block opens the socket (real psycopg connections open with `autocommit=True`);
subsequent `with` blocks on the same thread reuse it; a broken-connection error (`OperationalError` / `InterfaceError`)
inside the scope close-and-replaces the cached socket before re-raise. `close_thread_local_connection()` drains it
explicitly for shutdown hooks or test teardown. The thread-local cache is keyed on the resolved URL: a later `with`
block on the same thread requesting a different `db_url=` closes the stale socket first. The connection is not part of
any Streamlit cache key (a raw `psycopg.Connection` is not hashable). Close-time exceptions are logged and swallowed.

The read model is deliberately separate from `analytics/sync.py`: the sync owns the JSONL → Postgres write path, while
reads have a different error story and injection shape.

### Dashboard (`orchestrator/dashboard.py`)

Streamlit app over the read model. Opt-in via the `dashboard` dependency group so the default `uv sync --locked` keeps
installing only the polling runtime plus `pytest` / `ruff`. Streamlit (and its transitive pandas), `plotly`, the Plotly
figure builders in `orchestrator/dashboard_charts.py`, and the plotly-free theme tokens in
`orchestrator/dashboard_theme.py` are imported lazily inside `main()` — importing `orchestrator.dashboard` from a test
or non-dashboard caller does not require the group to be installed. A regression-guard test in `tests/test_dashboard.py`
asserts that loading `orchestrator.dashboard` keeps `streamlit`, `pandas`, `plotly`, and `orchestrator.dashboard_charts`
out of `sys.modules`.

**Module layout.** `orchestrator/dashboard.py` keeps page startup, the sidebar / date-range controls, and the
compatibility re-exports — `main()` is a thin orchestrator that delegates the static-metadata read
(`_read_static_metadata`) and, once the controls resolve the filters and staged read plan (`_prepare_dashboard_page`),
hands the page to `orchestrator/dashboard_widgets.py` for the two-wave render. The widget-rendering pipeline — the
two-wave data load (`_load_dashboard_data` → `_run_read_waves`, which owns the staged
`_dispatch_reads` fan-out, the between-wave short-circuit, and the `_log_dashboard_load` timing line), the empty /
no-data states (`_render_no_data`, `_render_empty_window`), every filter / widget section (the `_render_*` helpers) plus
the per-issue drill-down renderer, and the immutable page-state dataclasses the pipeline threads — live in
`orchestrator/dashboard_widgets.py`, which builds the KPI strip by handing a `_KpiInputs` to the
`orchestrator/dashboard_kpi_strip.py` aggregations through the facade. The historical `orchestrator.dashboard.*`
entry points that `dashboard_state` / `dashboard_kpis` / `dashboard_html` / `dashboard_cards` / `dashboard_kpi_strip` /
`dashboard_reads` own are each re-exported through the facade under their original names; from `dashboard_skill_matrix`
only its two public entry points (`_skill_matrix_html` / `parse_skill_matrix_sort`) are re-exported, its internal sort /
header / row helpers staying private. The `dashboard_widgets` widget / page-state members and the `dashboard_kpi_strip`
KPI-strip entry points (`_KpiInputs` / `_build_kpi_strip_data`) the pipeline and the dashboard tests reach through
`dashboard.<name>` are re-exported (and listed in
`__all__`) too, while the leaf-private internal helpers stay private to their modules — the `dashboard_cards`
`_safe_ratio` / `_backend_efficiency_metrics` math, the `dashboard_kpi_strip` `_kpi_totals` aggregations, the
`dashboard_html` sparkline / table internals, and the `dashboard_widgets` token / layout math helpers. So
`streamlit run orchestrator/dashboard.py` and the historical `orchestrator.dashboard.*` import surface (and its test
patch points) are unchanged.
The repo-root `sys.path` shim that lets `streamlit run` resolve the absolute `orchestrator.*` imports is factored
into the shared import-light `orchestrator/script_launch.py` helper (`ensure_repo_root_on_path`), which
`orchestrator/trajectory_dashboard.py` also calls.
The extracted helpers live in eight import-light modules (stdlib plus `orchestrator.analytics`, so they hold
the lazy-import invariant): `orchestrator/dashboard_state.py` (date / window math, preset and timezone vocabulary,
stage-filter / cache-key resolution, the issue-number parser, the DB-config banner check, and the read fan-out switch),
`orchestrator/dashboard_kpis.py` (KPI delta math, the computed insight banners, the reliability-tile triples, the
top-cost issue ordering, and the rework-share aggregation), `orchestrator/dashboard_html.py` (the topbar / filter-meta /
KPI-strip / sparkline / issues- and skill-trigger-table inline-HTML builders below),
`orchestrator/dashboard_cards.py` (the insight / backend-efficiency / cost-coverage / reliability-tile inline-HTML card
family), `orchestrator/dashboard_kpi_strip.py` (the KPI-strip aggregations: the token / throughput / rework helpers that
feed the four KPI tiles), `orchestrator/dashboard_skill_matrix.py` (the per-skill trigger matrix: its `mtx_sort` /
`mtx_dir` sort-param parser and the sortable inline-HTML table), `orchestrator/dashboard_reads.py` (the read
orchestration: the filter-to-query adapters, the cached data-extent / filter-option and per-filter widget readers, the
two-wave reader registries, the staged parallel dispatch, the static-metadata load, the two-wave data load, and the
load-timing log — where the cache keys / TTLs, read
ordering, parallel-read toggle, and one-banner-and-stop read-error behavior live), and
`orchestrator/dashboard_widgets.py` (the widget-rendering pipeline: the two-wave render
passes, the empty / no-data states, the per-issue drill-down renderer, the page footer, and the page-state dataclasses).
Streamlit is never imported in any of them — `st` (with the chart / theme / pandas handles) is always passed in as a
parameter.

```sh
uv sync --group dashboard                                  # install streamlit + plotly alongside the runtime + dev deps
uv run streamlit run orchestrator/dashboard.py             # launches a local browser tab
```

**Page chrome.** A sticky topbar carries the page title with the data extent / repo / event summary on the left and the
in-range spend pill on the right. A sticky filter bar exposes `3D` / `7D` / `All` inline presets (anchored at the data
extent's max timestamp and clamped to its min) plus two date inputs for arbitrary windows within the extent. The sidebar
surfaces a `Custom` preset fallback, a repo selector, event / stage multi-selects, and a `#123` / `123` issue-number
input.

**Caching.** Every per-filter read is wrapped in `st.cache_data` keyed by `(start, end, repo, events, stages, issue)`,
so a filter change invalidates every cached query in lockstep. `get_data_extent` and `get_filter_options` carry no
filter inputs and live in argument-less wrappers under the longer `STATIC_METADATA_TTL_SECONDS = 300` (5 min) TTL so the
sidebar / topbar only re-hit Postgres when `analytics.sync` ingests new events.

**Two-wave loading.** The 15 widget reads are staged into two waves:

- **First wave (6 reads).** `summary`, `prev_summary`, `ts_points`, `review_round_rows`, `throughput_rows`,
  `cost_coverage_rows` — feeds the topbar, filter meta, insight banners, and KPI strip.
- **Second wave (9 reads).** `stage_rows`, `agent_exits`, `issues_rows`, `backend_rows`, `repo_rows`, `heatmap_rows`,
  `backend_daily_rows`, `skill_rows`, `skill_matrix_rows` — feeds the rest of the body.

`main()` renders the above-the-fold chrome between waves on the main thread (worker threads only return data through
futures, so every `st.*` write runs on the main thread). The second wave is skipped on an empty window. A single inline
`st.spinner("Loading analytics…")` brackets both waves; a read error from either wave surfaces as one `st.error` +
`st.stop`.

**Body layout, top to bottom:**

1. Computed insight banners (failure rate ≥ 10 %, unpriced cost coverage ≥ 10 %).
2. Four-tile KPI strip — total spend, total tokens (`input + output + cache_read + cache_write`), cost / resolved
   issue, rework share — each with an inline-SVG sparkline and previous-window delta where applicable.
3. Hero `usage_over_time` stacked-area + cost-line chart with a "By token type / By backend" toggle.
4. Side-by-side `cost_by_stage` and `cost_by_review_round` cards; the stage card stacks each stage bar into no-cache +
   cache cost, and the review-round card groups development and review cost bars per round with each role's bar further
   stacked into no-cache + cache cost — so the operator can see how much per-stage and per-round spend still bypasses
   prompt caching.
5. 7/5 split: top-cost issues table (Issue with in-row cost bar, Cost, Runs, Review rds, Retries, status pill) +
   backend-efficiency cards (`$ / 1M tok`, `% cache hit`, `$ / run`) above the cost-source coverage bar (sized by token
   share).
6. Another 7/5 split: `cost_by_repo` bars + six-tile reliability panel (agent runs / success rate / resolved / rejected
   / failures / timeouts — all sourced from the same `Summary` window-wide aggregate) above the
   issues-resolved-per-day bar chart with explicit zero days backfilled.
7. 7 × 24 weekday × hour activity heatmap rendering token volume, with an in-card `UTC` offset selectbox (range `-12
   … +14`, default `UTC+7`) that controls both the heatmap bucketing and the wall-clock conversion of the `ts` column
   in the recent agent-runs table below. The widget binds to `st.session_state["tz_offset_hours"]`; the offset is read
   before the second-wave fan-out so the heatmap query buckets in the chosen zone, and the card subtitle / x-axis title
   render the matching `UTC±N` label.
8. "Skill trigger rates" panel — an aggregate table plus a fold-out matrix. The aggregate table (one row per
   `(agent_role, backend)` group) over `get_skill_trigger_rates` shows runs, skill runs, a trigger-rate bar, and the
   total trigger count. Below it, the **per-skill trigger matrix** (`_skill_matrix_html` over
   `get_skill_trigger_matrix`) sits inside a collapsed `st.expander` (mirroring the "Recent agent runs" block) so it
   does not dominate the card until opened; it renders one row per `(repo, agent_role, backend, skill)` cell with
   columns Repo / Role / Backend / Skill / Runs / Runs with skill / Trigger rate, where `Runs` is the cohort's total
   agent-exit runs, `Runs with skill` the subset that fired the skill, and `Trigger rate` the share of the two
   (`skill_runs / runs`). It folds each repo's `repo_skill_catalog` into the observed triggers so a skill the repo
   offers but no cohort fired surfaces as an explicit (muted) `0` "Runs with skill" cell (and a matching muted `0%`
   trigger rate) rather than a missing row (the cohort `Runs` total is never muted). The read model caps the list at
   100 rows (selected by Runs-with-skill DESC then Runs DESC), so the expander never floods the page; by default those
   rows display sorted by Repo ascending, then Trigger rate descending. Each column header is a clickable sort control:
   it is an anchor that writes `mtx_sort` / `mtx_dir` query params (parsed back by `parse_skill_matrix_sort`), so
   clicking a column re-sorts the matrix on it and clicking the active column flips the direction (a ▲ / ▼ indicator
   marks the current sort); an unknown / absent param falls back to that default Repo-ascending, Trigger-rate-descending
   order. Both tables are
   opt-in: they only carry signal when `TRACK_SKILL_TRIGGERS` is on. A window whose aggregate groups all show a `0%`
   rate renders a caption naming the switch, an empty window renders the aggregate no-rows notice, and the matrix shows
   a clear fallback notice in place of the table when no catalog-backed matrix can be built (no catalog records matched
   and no run fired a skill).
9. Recent agent-runs table as a collapsible expander; the `ts` column is shifted to the wall-clock of the selected UTC
   offset via `shift_ts`.
10. Per-issue drill-down when a number is entered.

**Filter contract.** `_build_window_where` distinguishes three cases for the event / stage selections: `None` is "no
filter on this column", a non-empty sequence emits a parameterised `IN (...)`, and an empty sequence emits a
tautologically-false predicate (`FALSE`). The event multiselect maps straight through (`event` is `NOT NULL` in the
schema). The stage multiselect routes through `resolve_stage_filter(selected, available)` because `options.stages` only
lists non-null stages: the all-selected default collapses to `None` so NULL-stage rows are included; an explicitly
cleared selection still emits `[]`; a proper subset passes through verbatim. Without this asymmetry the default
dashboard would silently exclude `stage_evaluation` rows on issues with no workflow label. The issue number acts as a
SQL-level filter when a specific repo is selected AND triggers the drill-down section; with the repo filter on "All" it
stays inert (GitHub issue numbers are not unique across repos).

**Parallel read fan-out.** Setting `DASHBOARD_PARALLEL_READS=on` (or `1` / `true` / `yes`, case-insensitive) flips the
15 widget reads from sequential to a `ThreadPoolExecutor` capped at eight workers. Each worker opens its own
thread-local psycopg connection via `analytics.read.analytics_connection()` — `psycopg.Connection` is not thread-safe,
so sharing one socket across workers would corrupt the wire protocol. The fan-out emits a single INFO log line on every
dashboard load — `dashboard.load: total=X.Xs reads=15 parallel=true|false` on a full render, or `reads=6` when the
empty-window short-circuit skips the second wave — so the two paths can be A/B'd with `grep dashboard.load
streamlit.log`. An `AnalyticsReadError` raised by any worker propagates verbatim from the first failing future.

**Chart builders.** `orchestrator/dashboard_charts.py` exposes pure Plotly figure builders: `usage_over_time`
(stacked-area + cost-line overlay with `mode="type"` / `mode="backend"` switch), `cost_horizontal_bars` (shared
primitive), `cost_by_repo` (thin adapter over `cost_horizontal_bars`), `cost_by_stage` (per-stage horizontal bars with
each bar stacked into no-cache + cache cost under `barmode="stack"`; the cache segment uses a translucent shade of the
stage's base color so the pair stays visibly tied to the stage, and only the outer cache segment carries the per-stage
dollar text), `cost_by_review_round` (grouped development/review bars per round, each role's bar further stacked into
no-cache + cache cost via `offsetgroup` + `barmode="relative"`; the cache segment uses a translucent shade of the role's
base color so the pair stays visibly tied to the role), `hour_weekday_heatmap` (faint-to-saturated accent gradient over
per-cell token totals, Sunday-first, with a `tz_label` parameter that annotates the x-axis — the caller passes the
matching offset to `get_hourly_heatmap` so cells already reflect that zone), and `done_per_day_bars` (resolved-per-day
bars with explicit `window_start` / `window_end` for zero-day backfill). `orchestrator/dashboard_charts.py` is a pure
re-export hub: each chart family lives in a focused leaf -- `usage_over_time` / `backend_per_day` in
`orchestrator/dashboard_charts_usage.py`, the cost-bar family (`cost_horizontal_bars` / `cost_by_repo` / `cost_by_stage`
/ `cost_by_review_round`) in `orchestrator/dashboard_charts_cost.py`, `hour_weekday_heatmap` in
`orchestrator/dashboard_charts_heatmap.py`, and `done_per_day_bars` in `orchestrator/dashboard_charts_throughput.py` --
and the hub re-imports each public builder under its original name. The shared low-level chart primitives
(`_empty_figure`, the money / mono-textfont / two-line-tick and panel-height / legend helpers) live in
`orchestrator/dashboard_charts_base.py`, which the usage / cost / throughput leaves import from (the heatmap leaf
inlines its own empty-state and imports none) -- so the dependency runs one way and a direct import of any chart module
is cycle-free. The topbar, filter meta, KPI strip,
sparkline / delta pill, most-expensive-issues table, and skill-trigger-rates aggregate table are built by inline-HTML
helpers in `orchestrator/dashboard_html.py`; the insight banners, per-card header, backend-efficiency cards,
cost-source coverage bar, and reliability-tile strip live in `orchestrator/dashboard_cards.py`; the per-skill trigger
matrix (its `mtx_sort` / `mtx_dir` sort-param parser and the sortable table) lives in
`orchestrator/dashboard_skill_matrix.py` (all re-exported through `dashboard.py`).

**Theme.** `orchestrator/dashboard_theme.py` is a plotly-free token module: palette (cool gray `#f4f5f8` page, white
cards, indigo accent, muted ink tints), spacing tokens, the `1480px` content max-width, per-token-type / per-backend /
per-agent-role / per-review-round / per-stage / per-`cost_source` palettes, a shared `base_layout(title=...)` Plotly
dict, the `PAGE_CSS` string the dashboard injects through `st.markdown(unsafe_allow_html=True)`, and the `fmt_money` /
`fmt_money_exact` / `fmt_tokens` / `fmt_num` formatters. `.streamlit/config.toml` mirrors the palette into Streamlit's
`[theme]` and disables the `[browser] gatherUsageStats` POST so the launch stays local-observability-only.

**Independence.** The dashboard process is independent of the polling tick: it does not open a GitHub session, does not
write to Postgres, and can be deployed off-host by repointing `ANALYTICS_DB_URL` at a managed Postgres endpoint without
changing the orchestrator's deployment.

### Empty and error states

The dashboard never raises an unhandled exception at the user — every missing-data or misconfiguration case surfaces
as a labeled banner.

- `` `ANALYTICS_DB_URL` is not configured. … `` (top-level `st.warning`, app stops) — *env* — `ANALYTICS_DB_URL`
  is unset, empty, or set to `off` / `disabled` / `none`. Set it in `.env` and **relaunch** `streamlit run
  orchestrator/dashboard.py` (the dashboard reads the URL from the imported analytics module at startup, so a browser
  reload alone will not pick up the new value).
- `Could not load analytics filter options: …` (top-level `st.error`, app stops) — *DB connectivity* — The
  dashboard could not reach Postgres at startup. Confirm `docker compose ps` shows `analytics-db` healthy, that the host
  / port / credentials in `ANALYTICS_DB_URL` match `analytics-db/.env`, and that the user can connect with `psql`.
- `Analytics query failed: …` (top-level `st.error`, app stops) — *DB schema / I/O* — A read query raised
  mid-render. Most commonly the `analytics_events` table is missing — either the volume is fresh and the init script
  has not been applied (`docker compose down && docker compose up -d`) or a manual schema reapply is needed (see
  [Service layout](#service-layout)).
- `No analytics events have been recorded yet. …` (top-level `st.info`, app stops) — *data* — The
  `analytics_events` table holds zero rows. Confirm the JSONL sink is on (`ANALYTICS_LOG_PATH`), that recent workflow
  activity produced records, and run `python -m orchestrator.analytics.sync` to populate Postgres.
- `No analytics events match the current filters.` (page banner) — *data* — The data extent is non-empty but every
  row was filtered out. Widen the window preset, pick `All` for the repo, blank the issue-number input, and confirm the
  event / stage multi-selects still have **every option selected** (an empty multi-select is the documented "show
  nothing" signal).
- `No stage data matches the current filters.` (chart annotation) — *data* — Scoped to the stage breakdown chart.
  Also empty when the only matching rows have a NULL stage (`stage_evaluation` records on issues with no workflow
  label).
- `` No `agent_exit` rows match the current filters. `` — *data* — The window contains `stage_enter` /
  `stage_evaluation` rows but no agent invocations — surfaces in the review-round chart, backend cards, cost coverage
  bar, and recent-runs expander.
- `No agent runs with recorded cost in this window.` — *data* — The top-cost issues table fell back to its empty
  state — no `(repo, issue)` pair in the window has any priced agent runs.
- `No repos match the current filters.` — *data* — The per-repo activity chart is empty for this filter combination.
- `Pick a specific repo in the sidebar before drilling into an issue number …` — *UI guard* — The issue-number
  input is inert with the repo filter on `All` because GitHub issue numbers are not unique across repos.
- ``No analytics events recorded for `<repo>#<n>` under the current filters.`` — *data / filter* — The drill-down
  query returned nothing. Either the issue number is wrong for that repo, the orchestrator has not processed it yet, or
  the event / stage multi-selects exclude every row for that issue.
- `Issue drill-down failed: …` — *DB I/O* — The drill-down query raised but the headline metrics rendered first.
  Same fixes as `Analytics query failed: …`.

If a sidebar multi-select is **explicitly cleared** (no items selected), every dependent widget falls back to "no data"
— that is the documented "show nothing for this dimension" signal. Re-select the items (or hit the `↺` reset chip
Streamlit renders on the widget) to restore the default unfiltered shape.

If `python -m orchestrator.analytics.sync` runs cleanly (non-zero `inserted=`) but the dashboard still shows zero rows,
double-check the `ANALYTICS_DB_URL` the sync used — passing `--db-url postgresql://other/db` (or a different shell
environment) populates a different database than the one the dashboard is reading.

## Usage parser (`orchestrator/usage.py`)

Pure-Python helpers that decode the JSONL stdout `agents.AgentResult` carries into a `UsageMetrics` dataclass —
backend, distinct model(s), turn count, input / output / cached / cache-read / cache-write token totals, `cost_usd`, and
a `cost_source` tag of `reported` / `estimated` / `unknown-price` / `no-usage`. No external dependency: the parser is
jq-free.

**Module layout.** The usage-metric parsing — the `UsageMetrics` dataclass and the claude / codex token, model, turn,
pricing, and cost parsing reached through `parse_agent_usage` (`parse_claude_usage` / `parse_codex_usage`) — lives in
the private `orchestrator/_usage_metrics.py`; the skill-trigger parsing — the `SkillTriggers` dataclass and the
`parse_claude_skills` / `parse_codex_skills` / `parse_agent_skills` trio — lives in the private
`orchestrator/_usage_skills.py`; and the trajectory parsing — the `TrajectoryStep` / `TurnUsage` / `AgentTrajectory`
dataclasses and the `parse_claude_trajectory` / `parse_codex_trajectory` / `parse_agent_trajectory` classifier — lives
in the private `orchestrator/_usage_trajectory.py`. `orchestrator.usage` re-exports exactly those three public surfaces
so it stays the stable import site for callers (`agents`, `workflow`, `analytics`). The trajectory classifier reuses the
metric module's shared event iterator, token decoders, and price path and the skill module's offered-set init-frame
helpers, so the resilience contract and cost precedence stay defined once.

**Two parsers, one dispatcher.** `parse_claude_usage(stdout)` consumes claude `--output-format stream-json` events,
groups assistant frames by `message.id` so the final-frame usage wins (claude streams partial counts on intermediate
frames), and sums per-model. `parse_codex_usage(stdout, fallback_model=None)` consumes codex `--json` events and treats
usage as cumulative across the session: the *last* non-zero usage record is the authoritative total.
`parse_agent_usage(backend, stdout, fallback_model=None)` dispatches by backend string the same way `agents.run_agent`
does.

**Cost precedence.** A `total_cost_usd` reported by the CLI itself always wins (`cost_source="reported"`); otherwise the
parser walks first-party Anthropic / OpenAI price tables baked into the module and produces an estimate (`"estimated"`).
When usage is present but the model SKU does not match any priced family, the parser returns
`cost_source="unknown-price"` and `cost_usd=None` rather than guess at zero or bill cached tokens at the input rate. An
empty stream — or one with no usage frames at all — yields `"no-usage"`.

**Resilience.** Malformed JSON lines (banner text, truncated frames, partial flushes) are silently skipped so a single
bad line never invalidates the rest of the stream. `workflow._run_agent_tracked` calls `parse_agent_usage` after every
tracked agent run and appends the parsed counts to the [analytics sink](#analytics-sink-analytics_log_path) under
`event="agent_exit"`; a parser exception is caught and downgraded to a `log.exception`.

**Terminal verdict surface.** Beyond the analytics sink, `workflow._accumulate_issue_usage` folds each run's
`UsageMetrics` into per-issue counters on the pinned state (`issue_agent_runs` / `issue_total_tokens` /
`issue_total_cost_usd` / `issue_cost_sources`; see [state-machine.md](state-machine.md#pinned-state)). When an
issue reaches a terminal, `workflow._format_issue_usage_verdict` renders those counters into one visible receipt line
posted on the issue thread — `:receipt: this issue: N agent runs · T tokens · $X.XX`, with `(est.)` appended when any
run's cost was `estimated` and the figure collapsed to `unknown` when an `unknown-price` run leaves the total
incomplete. The PR merged / rejected finalizers and the closed-`question` terminal post it as a standalone tracked
comment; the `umbrella` close comment appends it. It is a read-only summary — nothing gates on the figure — and it is
skipped when no run was ever counted.

**Skill-trigger extractor (opt-in).** A sibling trio mirrors the usage parsers' two-parsers-one-dispatcher shape and
resilience contract to record which agent *skills* a run loaded, gated behind `TRACK_SKILL_TRIGGERS` (default off;
see [`agent_exit` records](#agent_exit-records)). The result is a names-only evidence model on `SkillTriggers`:
`triggered` / `trigger_counts` are the loaded skills, `evidence` maps each to its tier (`confirmed` / `inferred`), and
`incidental` / `incidental_counts` are path-only references that never become loads. The two buckets are independent —
a skill both read and inspected is recorded in both — the only exclusion is structural: an incidental reference never
enters `triggered` / `trigger_counts` or the `skill_triggered` audit events. `parse_claude_skills(stdout)` reads the
firm **confirmed** signal — `tool_use`
content blocks named `Skill` in the `assistant` stream — and returns `input.skill` in first-seen order, de-duplicating
per invocation by the block `id`. (A captured real stream showed that under `--include-partial-messages` each completed
content block lands in its own `assistant` frame — the content array is partitioned across frames, not a cumulative
snapshot that repeats earlier blocks the way the `usage` sub-object does — so the parser walks every frame and de-dups
by `id` rather than keeping the last frame per message id.) Claude has no dedicated file mechanism, so it produces no
incidental references. `parse_codex_skills(stdout)` recognizes the codex shape: codex has no dedicated `Skill` tool, so
its file-based skill mechanism surfaces only as a `command_execution` item whose shell `command` opens a skill's
`skills/<name>/SKILL.md` file (codex's own "open its SKILL.md" instruction). A captured reviewer run pinned this — codex
both registered-under-`$CODEX_HOME/skills/` and project-local `.agents/skills/` reads match. The command is first
unwrapped (peeling the `bash -lc "…"` shell) and split into sub-commands on **unquoted, unescaped** operators —
quote- and backslash-aware, so a metacharacter inside a quoted argument (`rg 'foo|bar' path`) or a backslash-escaped one
(`rg foo\|cat path`) does not fabricate a spurious segment — then each `SKILL.md` match is routed by its sub-command's
leading verb (skipping any `NAME=value` env prefix). Only a verb
established as a **direct reader** (`cat` / `sed` / `head` / …) makes the reference an **inferred** load; every other
verb — an inspection / search (`git diff` / `git status` / `rg`), an env-prefixed inspection (`GIT_PAGER=cat git
diff …`), or a generic path-only command (`echo …`) — makes it an **incidental** reference. Even a reader verb is
demoted to incidental when the `SKILL.md` is *written* rather than read: an output-redirect target
(`cat t > .agents/skills/x/SKILL.md`) or a non-reading mode (`sed -i` / `--in-place`) is an incidental reference, so a
skill file a run only writes is never miscounted as a load. So a read chained after an inspection still counts, and a
bystander `git diff` over a changed SKILL.md does not fabricate a load. Started/completed
echo the same command, so the parser dedups by the shared `item.id` (last-frame-wins, as for usage) — for inferred
loads and incidental references alike. It reads only the `<name>` path segment and the routing verb — never the command
text or its `aggregated_output` (the file's contents), both of which can echo user content (names-only Privacy). The
inferred signal stays **heuristic** within the reader allowlist: a reader that opens a SKILL.md for an unrelated reason
would still register as an inferred load, while defaulting non-readers to incidental keeps an unrecognized command from
fabricating a trigger. `parse_agent_skills(backend, stdout)` dispatches by backend exactly as `parse_agent_usage` does.
The offered-skills set (`SkillTriggers.available`) is
**confirmed on claude** — read from the dedicated `skills` array in the `system`/`init` frame, captured against a real
stream — and
stays **empty on codex** at the parser layer: a captured `codex exec --json` stream (v0.142.5) carries no offered-skills
frame at all, so `record_agent_exit` backfills the codex offered set out-of-band from the filesystem via
`skill_catalog.discover_local_skills(cwd)` instead. The *triggered* set does not
depend on it either way. As with the usage parsers, malformed JSONL lines are skipped and a missing / renamed field
yields an empty result rather than an exception. Only the skill *name* is ever read — never the `Skill` tool's `args`
(Privacy).

**Trajectory classifier.** A third sibling trio mirrors the same two-parsers-one-dispatcher shape and resilience
contract to reconstruct a run's *trajectory* — the ordered timeline of tool calls / results interleaved with the
assistant / user text turns — into an `AgentTrajectory` dataclass: `backend`, a best-effort `system_prompt` / `tools`,
the names-only `skills` (`SkillTriggers`), an ordered `steps` tuple of `TrajectoryStep` (`kind` is `"tool_call"` /
`"tool_result"` / `"assistant_message"` / `"user_message"`, with `name` / `tool_id` / raw `content` — `name` /
`tool_id` empty on the text turns — plus a `turn` index tying each billed step back to the assistant turn that
produced it), `final_output`, and a best-effort `turns` tuple of per-turn token usage (`TurnUsage`, parallel to `tools`
/ `skills` and claude-only today). `parse_claude_trajectory(stdout)` reads the offered tools from the `system`/`init`
frame's `tools` array, then the ordered timeline: assistant `text` blocks become `assistant_message` turns and
`tool_use` blocks `tool_call` steps; user `text` blocks become `user_message` turns and `tool_result` blocks
`tool_result` steps — calls / results joined by `tool_use_id` and de-duplicated per id the same way
`parse_claude_skills` is, while id-less text blocks ride claude's per-completed-block framing — and the final answer
from the `result` frame's `result` string. It also groups those assistant frames by `message.id` in first-seen order to
assign each a 0-based `turn` index — stamped onto the `assistant_message` / `tool_call` steps that frame produced (a
`tool_result` / `user_message` step is a turn *input*, not billed output, so its `turn` stays `None`) — and emits one
`TurnUsage` per turn: `model`, `input_tokens` / `output_tokens`, `cache_read_tokens` / `cache_write_tokens` (the 5m + 1h
cache-creation buckets summed), and an always-*estimated* `cost_usd` / `cost_source` (`"estimated"`, or
`"unknown-price"` with `cost_usd=None` for an unpriced SKU — a reported `total_cost_usd` is a run-level figure and
never reaches a turn). The per-turn estimate reuses the same `_claude_estimate_cost` price path as the run aggregate, so
factoring it out left `parse_claude_usage`'s run totals unchanged. `parse_codex_trajectory(stdout)` treats each
`command_execution` item as one call (its `command`) plus one result (its `aggregated_output`) and each `agent_message`
item as one `assistant_message` turn (its `text`), collapsing each item's started/completed pair by the shared
`item.id`, and reads the final answer from the last `agent_message` `text`; it leaves `turns` empty with every
`step.turn` `None`, since codex usage frames are cumulative across the session rather than per-turn. Both reuse the
matching skill extractor for the `skills` field. `parse_agent_trajectory(backend, stdout)` dispatches by backend exactly
as the usage / skill dispatchers do. `system_prompt` stays `None` and `tools` stays empty in the classifier whenever a
backend's stream does not expose them (codex exposes neither); the analytics writer backfills codex `tools` out-of-band
from `skill_catalog.discover_codex_tools()`. Malformed JSONL lines are skipped and a missing / renamed field
yields an empty section rather than an exception. Unlike the skill extractor, this classifier records the **raw** stream
payload — tool inputs, tool outputs, and the final text — verbatim: it deliberately does **not** redact, truncate,
or write any file. Those concerns belong to its downstream writer, `analytics._trajectories._maybe_record_trajectory`
(called from `record_agent_exit`), which redacts every free-text field, applies the head/tail and total-record
truncation caps, and appends the `agent_trajectory` record to the
[trajectory sink](#trajectory-sink-trajectory_log_path) — only when `TRAJECTORY_LOG_PATH` is enabled and always behind
its own fail-open guard.

## Summary of "what runs when"

- `analytics.prune_with_retention_logging` (function call) — trigger: end of each `main._run_tick` after every
  configured repo drains; cadence: once per tick (process-wide, not per-repo); no-op when the sink is disabled or
  `ANALYTICS_RETENTION_DAYS <= 0`.
- `scheduler.reap` (method call) — trigger: end of each `main._run_tick` after every configured repo drains,
  immediately before the analytics prune; cadence: exactly once per polling pass regardless of repo count; nonblocking
  drain of any worker completions since the last poll. `_dispatch_via_scheduler` deliberately does NOT call `reap`.
