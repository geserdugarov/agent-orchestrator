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
  `review_round`, `retry_count`. `session_id` and `agent_exit`-only fields are described below. A launch the
  agent-run circuit refuses emits **neither**: no process was invoked, so there is no run to bookend — what that
  refusal records instead is an `agent_run_limit` event where a spent allowance was the reason, and an
  `agent_run_budget` record carrying the `exhausted` phase. The charge the launch paid before the spawn is on that
  same budget stream, one record per durable phase.
- `skill_triggered` — `_run_agent_tracked` after `agent_exit`, **only when `TRACK_SKILL_TRIGGERS` is on**
  (default off); one event per distinct skill the run triggered; extras: `agent` (backend), `agent_role`,
  `review_round`, `retry_count`, `skill` (the triggered skill name). Reuses the list `record_agent_exit` already parsed;
  off-switch installs emit none.
- `review_verdict` — `_handle_validating` after `_parse_review_verdict` reads the reviewer's last message; extras:
  `verdict` (`approved` / `changes_requested` / `unknown`), `review_round`, `pr_number`, `session_id`.
- `park_awaiting_human` — every `_park_awaiting_human` (in `workflow/engine/guards.py`) call site, plus
  `_on_question`, `_on_dirty_worktree`, `_on_unreadable_worktree`,
  `_park_verify_failure`, and the question- and discussion-stage `_park_question` / `_park_discussion` funnels;
  extras: `stage` (read from the current
  workflow label, not passed in), `reason` (e.g. `agent_timeout`, `push_failed`, `failed_checks`, `agent_question`,
  `agent_session_limit` (a quota-exhausted agent message, parked retryably as `agent_silent`),
  `agent_provider_unavailable` (a transient provider refusal — `API Error: 529 Overloaded` and its 5xx siblings —
  arriving as the agent's final message, parked retryably as `agent_silent` too), `dirty_worktree`,
  `unreadable_worktree` (a seam that publishes from a checkout, or resumes an agent over one, could not read what it
  is carrying — the implementing publication and the conflict stage's clean rebase could not PROVE the tree clean
  (`git status` failed, or an index entry is marked `assume-unchanged` / `skip-worktree`, which is a repository to
  look at rather than the file list `dirty_worktree` carries), and the conflict stage's two dev resumes got no
  reading at all), `unreadable_head` (nothing could name a commit a `resolving_conflict` round turns on — the head a
  clean rebase left, the head it started from, the head a body-edit resume begins at, or the head recovered commits
  leave the branch on — so the push behind it would carry neither a lease nor a named candidate),
  `reviewer_timeout`, `verify_failed` / `verify_timeout` / `verify_dirty` /
  `verify_head_changed`, `agent_run_limit` (the issue has spent every agent run its lifetime ceiling allows),
  `question_*`, `discussion_*`, ...). `dirty_worktree` carries `dirty_files` (how many paths
  git named); `unreadable_worktree` carries none, since naming a count there would report a failed read as an empty
  tree.
- `retry_cap` — the per-issue spawn budget's park, emitted by `workflow/engine/retry_budget.py`; extras: `stage`
  (read off the park rather than off the label, since the budget is shared and a parked issue's label is not always
  the stage that ran out — dropped when the park carries none), `phase` — `delivered` (the notice said for the first
  time), `reconciled` (the thread was found already carrying it, so it was recorded as said rather than repeated),
  `standing` (a later tick refused another spawn under the same park and said nothing — emitted by the gate where a
  stage re-asks it, and by the `decomposing`, late-adjudication, and `implementing` holds, which stop the tick
  before the gate is reached. A hold reports once per tick that REACHES it, which is fewer ticks than the issue
  gets: a `paused` / `backlog` hard skip runs no handler at all, and on `workflow:implementing` the preflight ahead
  of the park can own the tick itself — a merged pull request or a closed issue finalizes, a stale `question` /
  `discussion` relabel is refused, a recorded candidate is reconciled — so a poll that leaves no record behind is
  not a park that lifted),
  `continued` (an explicit renewal cleared the park and bought one more attempt). The `park_awaiting_human` record
  with `reason="retry_cap"` is emitted beside the `delivered` one, by the shared park the delivery goes through.
  A late adjudication that ran the budget out reports on this same stream and with the same four phases, even
  though the park itself is staged and delivered by the late mode's own owner: what an operator counts that refusal
  beside is every other stage's refusal on the same per-issue day of tokens, not the `late_verdict` and
  `late_failure` records a generation reports. Its `delivered` and `reconciled` are emitted where that mode's own
  notice reaches the thread, so a park whose comment GitHub refused records neither until the redelivery lands.
- `agent_run_limit` — the spent lifetime agent-run ledger's park, emitted through `workflow/engine/run_limit.py`
  (the two command phases by `workflow/engine/run_grant.py`, which shares this stream because what they answer is
  this park); extras:
  `stage` (the workflow label the issue is wearing, which is the whole of what this park can say about where the
  issue stopped — the ledger is spent by every role at every stage, so no one stage ran out of it), `phase` —
  `delivered` (the notice said for the first time), `reconciled` (the thread was found already carrying it, so it
  was recorded as said rather than repeated) — both emitted wherever the notice reaches the thread, which is the
  tracked spawn boundary where the refusal is taken (`workflow/engine/run_circuit.py`) and the dispatcher's hold
  replaying one an earlier tick left owed — `standing` (a later tick met the same explained park and said nothing
  — emitted by the dispatcher's own hold, which stops the tick before any handler is reached, so it reports once per
  tick that REACHES it: a `paused` / `backlog` hard skip runs no dispatch at all, a closed issue is let past to its
  terminal, and a guard ahead of the hold can own the tick itself), `granted` (a trusted
  `/orchestrator add-agent-runs N` widened the allowance and the park came down, so the tick went on to the stage its
  label names), `refused` (a request the park could not act on — malformed, zero, negative, or past the per-command
  maximum — left both counts where they were and earned its one receipt; emitted on the replay that recognizes a
  receipt already on the thread too, since what the phase records is the tick's own answer). The
  `park_awaiting_human` record with `reason="agent_run_limit"` is emitted beside the `delivered` one, by the shared
  park the delivery goes through. There is no `continued` phase here and nothing for one to renew: a lifetime total
  is spent once and no window reopens under this park — what `granted` records is a wider ceiling, not a returned
  run. The five phases here are about a notice and a command; the moment the park is *taken*, and the counts behind
  it, are on the `agent_run_budget` stream below, which is where an operator counts a refusal against the runs that
  spent it.
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
- `agent_run_budget` — the per-issue lifetime agent-run ledger's four durable transitions, emitted by
  `workflow/engine/run_budget.py` together with an identical analytics record; extras: `phase`, the whole ledger
  reading, the launch correlation, and the bounded refusal reason in
  [Agent-run budget records](#agent-run-budget-records-both-sinks). It sits beside `agent_run_limit` rather than
  inside it: that stream is about a notice and a command, this one about the runs an issue spent.
- `late_measurement` / `late_verdict` / `late_failure` / `late_snapshot` / `late_cleanup` / `late_cancellation` /
  `late_restart` / `late_transfer` — the late size gate's eight families, each emitted by
  `workflow/late_split/telemetry.py` together
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
- `agent_run_budget` — `workflow/engine/run_budget.py`, one record per durable agent-run budget transition beside the
  audit event of the same kind; carries `stage` plus the same payload — see
  [Agent-run budget records](#agent-run-budget-records-both-sinks). This is the one family here that is *not* one
  record per tracked run: an ordinary launch writes two (`reserved`, `started`) beside its `agent_exit`.
- `late_measurement` / `late_verdict` / `late_failure` / `late_snapshot` / `late_cleanup` / `late_cancellation` /
  `late_restart` / `late_transfer` — `workflow/late_split/telemetry.py`, one record per late event beside the audit
  event of the same
  kind; carries `stage` plus the same bounded payload — see
  [Late-split records](#late-split-records-both-sinks).
- `terminal_artifact_cleanup` — `runtime/artifact_records.py`, one record per candidate the daily terminal-artifact
  maintenance pass decided about; carries `outcome`, `reason`, `layout`, and a `branch` only where the reason names
  one — see [`terminal_artifact_cleanup` records](#terminal_artifact_cleanup-records). No `stage`: the pass is host
  maintenance between polling passes rather than a workflow stage, and the issues it is about have already ended.

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
bucket, and the primary-key fallback is stable across both session scans below.

**Active window vs. historical lookback.** `get_skill_adoption` runs two `agent_exit` scans plus one over the
repo-level catalog records, and combines all three in Python:

- The **active-window** scan applies the full reporting-window filters (date `[start, end)` / repo / stage / issue). It
  selects the *active* sessions (those with a run in the window) and computes the window-scoped invocation diagnostics.
- The **historical-lookback** scan (`WindowFilters.historical_scope`) gathers each active session's availability + load
  evidence from every `agent_exit` row *before the window end*, deliberately dropping the window `start` bound and the
  stage / events filters while keeping `end` / repo / issue — so a load or an availability report from a prior
  stage, or from before the reporting window, still counts toward that session's denominator and `adopted`. History
  rows for sessions that were not active in the window are ignored, so their evidence never leaks into the aggregate.
- The **repository-catalog** scan (`WindowFilters.catalog_scope`) reads the `repo_skill_catalog` records the level fill
  below is drawn from, keeping the date bounds and the repo filter and dropping the stage / events / issue selection —
  a catalog record is a repository-level fact carrying neither an issue nor a stage, so pushing the session selection
  onto it would drop every row and leave the fill with nothing to read. It is the same scan at the same scope that
  `get_skill_trigger_matrix` draws its zero-padding from.

The retained `end` bound is the **future-evidence cutoff**: evidence recorded *after* the window end never leaks
backward into an earlier window's aggregate, so a later load cannot retroactively raise a past window's adoption.

**A cell is keyed by source level too.** Both per-skill read models file their counts under the skill's `skill_levels`
level as well as its name, so a repository's own `develop` and a same-named global one stay two cells rather than one
blended average. The level is read off the same row that named the skill, so an offer and a load are matched by
provenance as well as by name. A name no level map covers — a record written before levels existed, or a claude run
whose stream names no source directory — is resolved against the repository's catalog first, as the paragraph below
describes; only what that cannot settle reads `unknown`, which is one spelling an operator can look up rather than a
scattering of blanks. Because an offer and a load take the same resolution, a legacy session's load still counts as
adoption of what it was offered. A `repo_skill_catalog` name the record itself left unclassified is read at `project`,
since that scan enumerates a repository's own checked-in definitions. Both tables render the level as its own sortable
`Level` column, so two rows whose Repo / Role / Backend / Skill read the same are legible as the two definitions their
differing counts come from.

**Both models fill a blank level from the catalog.** A claude stream names no source directory for the skills it
lists, so its loads arrive unclassified and would otherwise report as an `unknown` row beside the `project` one a
classified record put there — one definition's use split across two cells. `get_skill_trigger_matrix` and
`get_skill_adoption` therefore resolve an unclassified name against the repository's own `repo_skill_catalog`: a name
that repository offers at exactly one level is filed at that level, so the matrix's load merges into the padded cell
(a `develop / project` cell reading 3 of 166 runs rather than a padded zero next to an unknown-level 3) and the
adoption model's sessions merge into one (`develop / project` reading 3 adopting of 26 available rather than two
half-cells). A name the catalog never offered, or one it offers at two levels, stays `unknown` — there is no single
definition to file it under — and the lookup is per repository, so a level another repository classified the same name
at never reaches these runs. A level the run itself recorded is never overwritten: an observation from the run
outranks the repository-wide inference, so a globally installed `develop` a run named `user` keeps that level.

Adoption applies the fill to **every** category of evidence a cell is built from — the active-window loads, the
incidental references beside them, and the historical availability and loads the lookback scan gathers. Filling one
and not another is what would leave a session offered a `develop` at one level and credited with loading it at
another, which reads as an offer nobody took up next to a load nobody was offered. The catalog scan behind the
lookup takes the same repository-level scope on both reads (`WindowFilters.catalog_scope`: date and repo only, since
an issue or a stage pushed onto a repo-level record would drop every row).

This is a read-side resolution only — no record changes shape, and the `repo_skill_catalog` producer keeps
classifying what it enumerates as `project`. Windows already recorded therefore correct themselves the next time the
page is drawn: nothing is migrated or backfilled, and a window whose catalog records are missing simply degrades to
the levels the runs themselves recorded.

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

### `terminal_artifact_cleanup` records

`orchestrator/runtime/artifact_records.py` appends one `event="terminal_artifact_cleanup"` analytics record for every
candidate the terminal-artifact maintenance pass **decided about** — never one per phase, per artifact, or per
deletion step. A candidate is one finished issue's artifacts on this host and its remote, and the pass takes it as a
unit, so a count of these records is a count of finished issues considered and a count grouped by `outcome` is what
the host did about them. An issue carrying both published branch layouts is still one record; a candidate whose
teardown ran three steps and one whose teardown ran none are one record each. What the pass *is* and when it runs is
in [`../configuration/operations.md#reclaiming-a-finished-issues-artifacts`](../configuration/operations.md#reclaiming-a-finished-issues-artifacts).

Only candidates the pass reached appear. A pass that stops — a signal, a closed scheduler, its 120s host-hold budget
spent — answers for the prefix it got to and records exactly that prefix; the rest are rediscovered next interval and
recorded then. Nothing here is a retry list, and no record is a promise that anything will be revisited.

**Fields.** The `ts` / `repo` / `issue` / `event` envelope, plus four extras and nothing else:

- `outcome` — `cleaned`, `retained`, or `failed`. `cleaned` is every artifact the candidate was found holding now gone
  from this host and the remote, absences included; `retained` is the pass declining to act; `failed` is a step that
  ran and was refused.
- `reason` — the closed code that *fixes* the outcome, listed below. The two fields are separate because two readers
  want different things: a count of what a pass did comes off `outcome`, and what to go and look at off `reason`.
- `layout` — which of the layouts this orchestrator published the candidate under: `current` (the slug-namespaced
  branch it publishes now, and the per-repository checkout beside it), `legacy` (the flat `orchestrator/issue-<n>` an
  issue in flight when namespacing landed is still on), `mixed` (both at once, which a migration leaves behind), or
  `remote_only` (this host holds no checkout and no branch — whatever the remote's copy is called, there is nothing
  here to look at).
- `branch` — the artifact the reason is about, **only** where that artifact is a branch, and dropped entirely
  otherwise. Which kind of artifact a result names is settled from its `reason` rather than from the shape of the
  string, because the two kinds are not distinguishable as text: with `WORKTREES_DIR` set to `orchestrator`, a
  checkout's path and its issue's branch are the same characters. The vocabulary splits three ways.
  `branch_checked_out`, `remote_delete_failed`, and `local_delete_failed` are spelled on a branch and nowhere else,
  so they always name one, however that host spells its checkouts. `reclaimed`, the two claim reasons, the two
  quiet-period ones, and `worktree_removal_failed` never name a branch. `unproven`, `tip_moved`, and `tip_unreadable`
  can be about either artifact, so those alone are measured against the candidate's own checkout paths and drop a
  subject that is both — the record has to say which artifact it means. The value written is then the name re-derived
  from the repository and the issue number rather than the one the result carried, so it is always one of the exact
  two names this orchestrator publishes that issue under; a checkout path, an issue reference, and a branch some
  other configured repo sharing the clone owns each fail that match. A record with no `branch` key is a reason that
  was never about one, or one nothing could attribute.

**Reason codes.** Closed, and each is fixed to exactly one outcome, so the two fields cannot disagree:

| `reason` | `outcome` | What it means |
| --- | --- | --- |
| `reclaimed` | `cleaned` | The teardown reached the end: checkout removed, every branch gone here and on the remote. |
| `unproven` | `retained` | The classification kept it; which artifact and which question is on the log below. |
| `recent_activity` | `retained` | A checkout was touched inside the one-hour quiet period. |
| `activity_unreadable` | `retained` | That modification-time reading could not be taken. |
| `active_claim` | `retained` | Something is running for this issue right now. |
| `claim_unreadable` | `retained` | The guard that answers whether anything is could not be asked. |
| `tip_moved` | `retained` | An artifact left the proved commit between the proof and the mutation. |
| `tip_unreadable` | `retained` | That last reading failed, or nothing cleared a commit for a named artifact. |
| `branch_checked_out` | `retained` | Some worktree of the clone is still standing on the branch. |
| `worktree_removal_failed` | `failed` | `git worktree remove` refused the checkout; it is never forced. |
| `remote_delete_failed` | `failed` | The remote refused the delete leased at the proved commit. |
| `local_delete_failed` | `failed` | The pinned local `update-ref -d` would not run. |

`unproven` stands for one or more retentions the classifier recorded, and those are deliberately not on the record:
each names an artifact, and an artifact is a branch *or* a checkout path. They are reported per candidate on the
`orchestrator.worktree_lifecycle` log instead, which names the artifact the pass kept the candidate for. The families
they fall into — a question that was asked and answered no (an open pull request, a dirty tree, a tree hiding files
its own rules cover, commits nothing accounts for), a question that could not be put (an issue, a pinned comment, a
pull-request lookup, a git read), and a question answered with something nobody can act on (an issue in two workflow
states at once, a pinned comment holding something that is not a state, a checkout on a branch this issue never
published) — are what the remediation section below is organized by.

What each one asks an operator to do is in
[`../configuration/operations.md#reading-a-cleanup-result`](../configuration/operations.md#reading-a-cleanup-result).

**What never travels.** No command line, no git output, no exception text, no checkout path, no file names, no tree
contents, no credentials — a sink is a metric surface, and a host's filesystem layout is not a metric. Three of the
four extras are members of vocabularies the code itself declares, so a record cannot say anything the code does not
already name, and each is proved a member again as the record is built: a lookalike string is refused as squarely as
prose, and a refused field writes no record at all rather than a record with the field dropped. The full per-candidate
detail — the artifact each reason is about — is on the `orchestrator.worktree_lifecycle` log channel, which is the
operator's own host rather than a sink.

**Analytics only, no audit twin.** The audit log is written through `GitHubClient.emit_event` and records workflow
events against an issue the tick is driving. This pass drives nothing: it writes no label, no pinned state, and no
comment, it runs between polling passes (and under `--cleanup-terminal-artifacts` with read-only clients), and every
issue it is about has already ended. So the record goes to the metric sink and the audit stream stays what it says it
is.

**Fail-open, on two levels.** The pass has already decided and acted by the time any record is built, so nothing on
this path can change a cleanup decision — and neither level of failure raises out of it. The shared sink writer
answers the first two: a **disabled** sink (`ANALYTICS_LOG_PATH` unset to `off` / `disabled` / `none` / empty) is a
silent no-op that opens nothing and logs nothing, and a filesystem that **refuses the append** — read-only mount, full
disk, permission denied, a misconfigured path — is caught in `append_jsonl_record`, downgraded to a `log.warning` on
`orchestrator.analytics`, and the record is dropped. Neither reaches the maintenance owner, so
`orchestrator.worktree_lifecycle` stays quiet for both. The per-candidate boundary in
`runtime/artifact_records.py` covers what is left — a payload that could not be built (a field outside its
vocabulary) or serialized, and anything unexpected — and reports it on `orchestrator.worktree_lifecycle` naming the
issue and the failure's *type*, never the exception's own words, which are the same unbounded content the record is
bounded against. Either way one candidate's record is the whole cost: every candidate behind it is still recorded.

## Agent-run budget records (both sinks)

The per-issue lifetime agent-run ledger writes to **both** streams, deliberately: the audit copy has to answer offline
what the database answers over the analytics sink — how much of a lifetime an ordinary issue actually spends, whether
a deployment's ceiling is turning launches away and at which stages, how often a charge is taken that never reaches a
process, and how often a human has to buy past the limit. One call on
[`workflow/engine/run_budget.py`](../../orchestrator/workflow/engine/run_budget.py) emits both —
`GitHubClient.emit_event` for the audit record and the recording package's `build_record` / `append_record` pair for
the analytics record — so the two carry the same payload under their own envelopes. The two records for one
transition differ only in `ts`.

**One family, four phases.** The kind is always `agent_run_budget`; `phase` says which durable step it is. Splitting
them into four event kinds would make an operator join four streams to read one issue's lifetime.

| `phase` | Written by | What it means |
| --- | --- | --- |
| `reserved` | `workflow/engine/run_circuit.py` | a run was charged to the issue and no process has been invoked |
| `started` | `workflow/engine/run_circuit.py` | the standing charge moved to the phase the spawn happens in |
| `exhausted` | `workflow/engine/run_limit.py` | a launch was refused and the lifetime park was taken on it |
| `extended` | `workflow/engine/run_grant.py` | a trusted `/orchestrator add-agent-runs N` widened the allowance |

**Payload.** Every phase carries the whole ledger reading the transition was taken on, rather than the one field that
moved — a record naming only the delta is one an operator has to join against a setting that may have changed since,
and offline, against the audit copy alone, there is nothing to join to:

- **The ceiling** — `configured` (what `MAX_AGENT_RUNS_PER_ISSUE` says right now) and `allowance` (what this issue is
  actually held to). They differ exactly where the issue carries an allowance of its own, which is what an
  `extended` phase writes; a refusal explained by the deployment's number would name a ceiling this issue was never
  held to.
- **The spend** — `used`, the monotonic count of runs this issue has taken over its whole life.
- **What is left** — `remaining`, on **every** record. Under a bounded allowance it is `max(allowance - used, 0)` —
  floored, since an issue carried past its ceiling has nothing left rather than a negative amount. Under an unlimited
  allowance (`allowance <= 0`) it is the string `"unlimited"` (`REMAINING_UNLIMITED`). Neither of the alternatives
  works: a number there is one a query could compare against zero and read as an issue about to stop, and a dropped
  field is one a consumer cannot tell from a count some writer, envelope, or replay lost — which defeats the point of
  a copy that answers offline. A query that treats the field as a number therefore filters or coalesces that word:
  `NULLIF(extras->>'remaining', 'unlimited')::int`.
- **Correlation** — `reservation_id`, `<12-char fingerprint head>-<used>`: the first `FINGERPRINT_HEAD_LENGTH`
  characters of the launch fingerprint the circuit charges under, then the ledger's `used` count as it stood after
  that charge. It joins the tick that reserved a run to the tick that spawned on it. Both halves are needed. The
  fingerprint alone repeats — it is deliberately stable across ticks so a standing reservation can be recognized and
  reused, so the same shape is charged again every time a launch that already reached `started` comes back, and two
  unrelated charges would carry one id. `used` goes up by exactly one per charge and never comes down, so it is that
  charge's own sequence number on the issue: the pair names one charge, names it identically from both of its phases
  (the count does not move between them, and a reused reservation reports the count its own charge wrote), and never
  names two. With the envelope around it, `(repo, issue, reservation_id)` identifies one charge. Present on
  `reserved` and `started` and on neither of the others: a refused launch never took a charge, and a grant is not a
  launch. The fingerprint is a SHA-256 over the role, stage, backend, spec, resumed session, review round, and retry
  count — never the prompt or the worktree — so the correlation carries none of what a launch was built out of.
- **The work** — `stage` on every phase, and `agent_role` on the three launch phases only. On those three both are
  the literals the `agent_spawn` / `agent_exit` pair records for the same launch, so a budget record and the spawn
  beside it cannot name different work. `extended` still carries a `stage` — read off the workflow label the issue is
  wearing rather than named by a launch — but no `agent_role` at all: the ledger is spent by every role at every
  stage, so there is no one role a human bought runs for.
- **The refusal** — `reason`, on `exhausted` only, from a closed vocabulary: `allowance_spent` (`used == allowance` —
  the issue reached its ceiling by running) or `allowance_exceeded` (`used > allowance` — the issue was already past
  the ceiling in force, so the ceiling came down on it rather than the runs adding up). Bounded rather than free
  text, like every other field here: a sink carries whatever it is handed.

**Transitions, not states.** Every record rides the write that makes its step durable, and is emitted *after* it. That
is the whole of what keeps the stream a count of what an issue spent rather than of what a tick tried to spend:

- A launch honoring a **reservation** an earlier tick left standing pays for no new run, so it records `started` and
  no second `reserved`.
- A **standing park** records nothing. An exhausted issue meets the same refusal on every launch it has left and the
  dispatcher's hold meets it on every tick, so a record per meeting would report one ending as a stream of them; what
  says a park went on holding is the `agent_run_limit` event's `standing` phase. A park re-taken while it still owes
  the thread its sentence records nothing either — the sentence is re-said, but the lifetime ended once.
- A **replayed command** records the extension that landed. A tick that died between its receipt and its write bought
  nothing durable, so the next tick's grant is the only one there is to report; a grant that landed takes its own
  park down and is never re-read.
- The two refusals that decide **nothing about the issue** — a pinned comment nobody could read, a write nobody could
  take — record nothing at all, on either sink.

**Postgres ingestion.** `analytics_events` has no column for a budget field and needs none. `stage` and `agent_role`
are already promoted columns; `phase`, `configured`, `allowance`, `used`, `remaining`, `reservation_id`, and `reason`
all land in `extras JSONB` with **no DDL change and no schema reapply** — the same path the opt-in skill fields take
(see [`analytics-database.md`](analytics-database.md#schema)). `remaining` is the one of those whose JSON type varies
by row (a number, or `"unlimited"`), which JSONB carries as written.

**Fail-open, all the way through.** Both sinks already swallow a filesystem refusal, and each of the two writes
additionally rides its own guard on the `orchestrator.workflow` channel, so a failing sink costs the record and
nothing else — and a failure on one side does not skip the other. Building the record is guarded on the same channel
for the same reason: `extended` is the one phase whose `stage` costs a GitHub read, and it is taken *after* the write
that widened the ceiling, so an escaping exception there would break a tick that had already taken the park down and
lose the transition to both sinks on the way out. A read that fails costs the `stage` and not the record — the field
is optional in both envelopes, a phase with no stage is still countable, and a transition nothing recorded is one an
operator can never count. Everything else in a payload is built from a frozen ledger reading and a frozen launch, so
there is nothing else here that can fail. The transition a record describes is already durable on the issue by the
time any of this runs, so nothing about workflow disposition depends on delivery.

## Late-split records (both sinks)

The late size gate writes to **both** streams, deliberately: the audit copy has to answer offline what the database
answers, so an operator with only the JSONL file can tell whether depth 3 is being approached, which repositories keep
producing artifact-dominated `single` verdicts, whether the ceiling is being crossed before anything is published or
by the fix commits a review asks for afterwards, and whether the configured threshold creates more adjudication than
it prevents. One call on `workflow/late_split/telemetry.py` emits both — `GitHubClient.emit_event` for the audit record
and the recording package's `build_record` / `append_record` pair for the analytics record — so the two carry the same
fields under their own envelopes.

**Families.** `late_measurement` (a clean committed candidate was measured), `late_verdict` (an adjudication decided),
`late_failure` (a typed step could not be completed), `late_snapshot` and `late_cleanup` (what happened to one
external resource), `late_cancellation` (the owner was observed closed), `late_restart` (a restart after a
completed cancellation), and `late_transfer` (an adjudication's exemption was carried onto the commit a workflow
rewrite replaced it with). The kind is the family; `stage` is the bare stage tag the issue sat in, spelled by the
emitter like every other event on this page. Every one of the eight also says which side of publication its
generation was entered on, because each of them means something different under each: a `late_cleanup` reconciling a
snapshot cut for an initial publication and one reconciling a pull request the remote already carries are the same
family describing two different steps. That answer is the `publication` field below, not a family of its own.

**What a stream carries.** Four producers write to these families. The first is the publication seam itself
([`../workflow/roles.md`](../workflow/roles.md#the-size-gate-a-committed-candidate-passes)), which is where a
candidate is measured at all: it writes one `late_measurement` per clean committed candidate — small and oversized
alike, since a threshold study needs the candidates that *passed* as much as the ones that did not. `stage` is the
one the reading was taken in rather than this package's own name: `implementing` for the push that opens the pull
request, and `validating` / `documenting` / `in_review` / `fixing` / `resolving_conflict` for a push onto one the
remote already carries, so a measurement is never filed under a stage no developer of it ran in. Beside them it writes
a `late_failure` carrying `measurement_failed` for **every** reading it could not take: a base the remote would not
name, a base or candidate object this host does not hold, a diff nothing could pin, and a recorded candidate a reaped
worktree took with it. Which of those it was is on the record rather than left to the reader — `measurement_failure`
names the step, and `detail` carries the line that step wrote — so a run of these can be counted by cause without
reading the issue thread back. Every one of them is recorded whether or not a human is told about it — the two
transport steps are retried quietly, three consecutive misses on one pair before the fourth parks the issue, and the
stream is where those misses are visible at all. A quiet miss and the park that ends the run write the same record:
the same `late_failure`, the same `measurement_failed`, the same step and line, since a reading that did not happen
is the thing being counted and a stream counting causes may not lose the ones nobody was told about. What tells them
apart is the `park_awaiting_human` record beside them, carrying `reason="late_measurement_failed"` — and it dates an
ANNOUNCEMENT rather than the state of the bound. One goes down where the fourth miss parks, again where a reading
stops at a member the thread has not been told about, and again where a human's own reply has spent the park before
it: a bare `/orchestrator continue` clears the latch and the reason ahead of the gate, so the reading it buys is
announced whatever it stops at, the member the last notice named included. The readings between those — the ones
inside the bound, and the ones a standing park holds silently — write their `late_failure` with no park record beside
them and read alike here. What a run of them is takes the records before it under the same cycle and CANDIDATE
(`source_sha`) rather than the same generation: a base the remote would not name records no base at all, so each
retry of that pair freezes afresh and mints the next generation under the one cycle. Or it takes the pinned
`late_measurement_miss_count` and `late_measurement_failure`, which no payload field carries. What that
park bounds is the MENTIONS rather
than the readings: on the five stages that publish onto a pull request the remote already carries, the
reconciliation ahead of every handler goes on
re-reading the parked pair once a poll, and every one of those
readings reports again. So a single unreachable base can carry an unbounded run of `late_failure` records under one
cycle and generation while the thread stays silent, and it is the reading that finally lands — not a human — that
ends the run. The silence is scoped to the STEP the park's notice named, which the pinned
`late_measurement_failure` records: a run of readings that go on stopping there is silent however long it is, while
one that starts stopping somewhere else — a remote that has come back far enough to name the base while a fetch
still brings nothing — breaks it exactly once, and the records go on either way. A candidate refused before either
end of the diff was frozen has no generation of its own to be correlated by, so the identity is *minted* for the
record — derived from what the pinned comment already says, so a reading that keeps failing reports the same
attempt rather than a fresh cycle per tick — and deliberately not persisted, since a pinned cycle with no
candidate under it freezes nothing and would be read as a live cycle by the guard that ends one when the issue
closes. A candidate the gate skips emits nothing, and five do. Three are commits this workflow has already decided
about, each named exactly and only by its own record: the one an adjudication accepted (`late_exempt_sha`), the one
the gate itself approved and has still to push (`late_approved_sha`, brought back by a crash between the write that
approves a candidate and the push it licenses), and the one this stage already pushed
(`implementing_published_sha`, brought back by a relabel to `workflow:validating` that did not land). The fourth is
a NEW candidate while `DECOMPOSE=off`, and the fifth is a workflow rewrite that EARNED the exemption of the commit
it replaced — a squash on approval, or the clean base rebase the per-tick refresh publishes (`late_rewrite_*`,
granted only over two recomputed fingerprints that agree). So a reading that
never happened is not always a reading that failed: an issue whose branch is published, or whose commit a verdict
settled, reaches the seam again and leaves no `late_measurement` behind, which is the shape a threshold study sees
for a candidate that was counted once and acted on twice. The switch is not silence either — a candidate this issue
already has a recorded generation for is still measured with it off, and so is a reconciliation answering a reading
a previous tick recorded, so a repository running with the switch off still writes these families for the work
already in the gate. What it stops is records for work that never enters it. The seam writes one more family, and
rarely: a `late_cancellation` — under the same entry stage — where a close a poll latched reaches the retirement
that runs ahead of a publication — asked before that write and again on the window it is held inside, so a close
arriving as the record stops naming its cycle is reported rather than lost. It is the same family and the same
shape the adjudication's own barriers emit, so a cancelled cycle reads alike wherever it was ended.

The seam writes one family more still, and it is the only one on this page that reports a decision MOVING rather
than being taken: `late_transfer`, one record per exemption carried onto the commit a workflow rewrite replaced the
accepted one with ([`../workflow/roles.md`](../workflow/roles.md#the-size-gate-a-committed-candidate-passes)). It
rides the write that receipts the landed push, so it is written only where the pull request really carries the
rewritten commit, and it is filed under the stage the rewrite was entered from rather than under `implementing`. The
record names both ends of both contributions — `source_sha` / `base_sha` for the pair the verdict moved ONTO,
`transferred_from_sha` / `transferred_from_base_sha` for the pair it moved off — the pull request it happened on,
`rewrite_kind` for which rewrite this workflow made (`squash` for the collapse an approval earns,
`auto_clean_rebase` for the replay the base refresh publishes, and `conflict_rebase` for the one
`workflow:resolving_conflict` runs when a branch has stopped merging cleanly), and `transfer_proof` for which
reading proved the push landed: `pushed` where the leased force-push moved the publication off the head the permit
was granted against, and `already_published` where a tick that pushed and died before its receipt came back to a
pull request standing there already and the leased no-op found it so. On the base-refresh side that second reading
is the crash recovery's own: the anchor an interrupted rebase left pinned is what brings the tick back, and where
the remote is already on the rewritten commit the recovery takes the leased no-op itself — on the permit alone —
rather than relabelling and leaving the permission for a stage the rewrite was never entered from
([`../state-machine/labels-and-state.md#base-refresh`](../state-machine/labels-and-state.md#base-refresh)). There is
no third value — a remote anywhere else is a permit that was refused, which settles nothing and reports nothing.
The proof is also the one thing here a later tick could not re-derive, so the write that settles a transfer keeps
it on the pinned comment until this record has been made and drops it behind the record: a process lost between
the settlement and the report comes back to a verdict that moved and a proof still standing, and makes the record
from it rather than leaving the move unannounced.
No `late_verdict` joins it: the transfer carries a decision a human already made onto the object that replaced the
one they made it about, and a second `single` here would read as a second adjudication of work nobody was asked
about twice. Two roads leave the
permission unspent and are silent here for the same reason — nothing moved. A publication the permit went PAST —
some other commit reached the pull request — drops it. And a permission this tick's permit did not VOUCH for is left
exactly where it stands: a refusal on the re-ask is not a hold, so the rewritten commit falls through to the
ordinary cumulative gate and a count under the ceiling publishes it anyway. So a pull request carrying the rewritten
commit with no `late_transfer` beside it is the ordinary reading having published it, and the `late_measurement`
that reading wrote is what says so.

The remaining three arrive once an oversized candidate is under adjudication. The second is the late adjudication under
`workflow:decomposing`
([`../workflow/roles.md`](../workflow/roles.md#what-a-late-adjudication-is-asked-and-what-it-may-answer)): it writes
one `late_verdict` per completed adjudication, one `late_measurement` per candidate a developer revision
re-froze and re-measured
([`../workflow/roles.md`](../workflow/roles.md#what-the-humans-can-still-change-while-a-candidate-is-frozen)) —
under `stage: decomposing`, which is where a re-measurement happens, unlike the gate's own — one
`late_cancellation` per cycle whose owner the post-agent guard found closed after ANY completed run — a question
and a timeout included
([`../workflow/roles.md`](../workflow/roles.md#the-owner-read-a-finished-run-has-to-pass)) — and a
`late_failure` carrying `plan_pr_hold_failed` when the cycle-marked hold cannot be reconciled or released on a
still-open pull request (a notice a human removed from one included, which starts no new agent under it),
`measurement_failed` when a revised candidate could not be measured — with the same `measurement_failure` and
`detail` the gate's own records carry, since a re-measurement is taken in a checkout an agent has been running in —
`owner_read_failed` on every read that
guard could not take, or `pr_reconcile_failed` when the pull request an accepted candidate would be handed on
against could not be established. Two more arrive with the split transaction
([`../workflow/roles.md`](../workflow/roles.md#what-a-cleared-split-actually-does)): one `late_snapshot` per
snapshot ref established (`retained`) or refused (`failed`), and one `late_cleanup` per superseded branch the
transaction reconciled (`reconciled`) or could not (`failed`) — the latter beside a `late_failure` carrying
`snapshot_failed` or `branch_cleanup_failed`, and `child_create_failed` or `supersession_failed` where those
steps park instead. The third producer is the reclamation, and it has three entries into the same emission. One
is the umbrella's terminal gate, where what the transaction could not reclaim is retried: it emits the same pair
under `stage: umbrella` on every tick that finds every child resolved and something still owed — the branch
whenever it is owed, and the snapshot ref once every recorded direct consumer is terminal, carrying
`snapshot_delete_failed` where the remote refuses one. One case emits nothing at all for an owed branch: a split
entered past publication does not delete the branch while the pull request it superseded is open again, and nothing
was attempted there, so no `late_cleanup` and no `branch_cleanup_failed` describe it — the terminal that never fires
and the log line on every visit that holds are what say so. The same gate's *park* is the second, and it emits the
same way: a child `rejected` or closed by hand ends every consumer exactly as all-resolved does, so an umbrella
stopped for a human settles from the same scan on its way out. The third is the closed-owner cleanup sweep
([`../state-machine/delivery-stages.md`](../state-machine/delivery-stages.md#closed-owner-cleanup-sweep-no-label-of-its-own)),
which asks the same question of an issue a human closed mid-cycle and emits the same families under whichever of
`stage: decomposing` / `stage: umbrella` that issue was closed on — the stage is read off the issue rather than
named by the caller, so a record says where the reclamation happened rather than which owner drove it. It is
also the second producer of `late_cancellation`, under the same bound: the record rides the write that first
marks the cycle, so a close at a boundary no agent was running at is reported exactly once however many passes
the cleanup behind it takes. And it adds one `late_cleanup` member no other producer emits — `resource:
plan_pr`, for the held pull request a cancelled cycle closes — carrying `pr_reconcile_failed` where that
pull request could not be released or closed.

The fourth producer is the **restart** an operator authorizes by taking a settled cancellation's `rejected` back off
([`../workflow/roles.md`](../workflow/roles.md#the-restart-that-ending-authorizes)), and it is the only one that
writes `late_restart`. Two records per restart, one per half of its transaction. `restart_step: pending` rides the
write that first makes the marker durable — so a restart held for ticks by a label GitHub keeps refusing is one
record rather than one per tick — and carries the cancelled cycle's own `cycle_id` beside `restart_target` and
`predecessor_cycle_id`, under `phase: restarting`. `restart_step: reconciled` rides the retirement behind both
external effects and carries the *fresh* cycle's `cycle_id`, with `predecessor_cycle_id` naming the one it succeeds
and no `restart_target`, since the marker is gone by then. Between them it emits `late_failure` carrying
`restart_failed` on every pass whose notice or label GitHub declined. The fresh cycle deliberately carries no
commits and no measurement, which is why neither `late_restart` nor the failure beside it is held to the
self-contained rule the two size families are.

What reaches these streams is a **transition**, not a state. An entry that was already reconciled is not asked
about at all; an obligation merely *held* (a ref whose consumers are not all ended) attempts nothing; and a retry
that reaches the same answer as the visit before it — a remote that goes on refusing one delete — records nothing
either, because the record already carries that answer and repeating it per cadence is one fact restated rather
than a second thing having gone wrong. The pinned write rides the same reading, so a standing refusal costs one
request per visit rather than a request and a comment write. What is *not* bounded that way is the log: every
obligation short of `reconciled` is warned about on every visit that attempted it, and the umbrella stays open — or
the closed owner keeps its label — for as long as it is held. So the shape of an obligation nobody can settle is one
`late_cleanup` with `outcome: failed` followed by a terminal that never fires, rather than a stream of identical
records.

`outcome: reclaiming` is progress rather than failure and is why the state reaches this stream at all: the decision
goes down *before* the delete, so a record carrying it is an obligation whose ref may already be gone while a
consumer it owes a receipt could not be told — the next visit finishes the telling and reports `reconciled`.

**Family-typed events.** A record is built from a `LateEvent` on `workflow/late_split/events.py`, and each family
declares which detail fields it requires and which it may carry (`_FAMILY_FIELDS`). Anything else raises
`InvalidLateValue` where the event is constructed, so a measurement claiming a verdict, a verdict with no verdict on
it, or a cleanup with no resource cannot reach either sink at all:

| Family | Requires | May also carry |
| --- | --- | --- |
| `late_measurement`, `late_cancellation` | — | — |
| `late_verdict` | `verdict`; `category` too when `question` | `category` on any verdict, `child_count` with `split` |
| `late_failure` | `failure` | `measurement_failure` with `failure: measurement_failed`, `detail` with a step |
| `late_snapshot`, `late_cleanup` | `resource` | — |
| `late_restart` | `restart_step` | — |
| `late_transfer` | `rewrite_kind`, `transfer_proof`, `transferred_from_sha`, `transferred_from_base_sha` | — |

A category is allowed on **every** verdict and required only of a `question`: a `single` verdict that explains itself
as `generated_artifacts` is exactly the artifact-dominated signal this page promises, so the schema has to be able to
carry it. A child count is a split's and nobody else's, in both directions.

The failure family's two are optional, and each is pinned to exactly one thing rather than merely permitted — a
field allowed beside every member describes none of them. `measurement_failure` belongs to `measurement_failed` and
to no other member of `LateFailure`: a snapshot the remote refused, a hold nobody could release, and a restart GitHub
declined each took no reading, so one of them carrying `base_absent` would report a measurement stopping where none
was taken, and the contract refuses it. `detail` belongs to `measurement_failure` the same way and is refused without
it. Both refusals are checked in the constructor and re-checked in the payload builder, like every other pairing on
this page.

A refusal that *was* a reading therefore carries the step — the git layer's own vocabulary (`base_unreadable`,
`base_absent`, `candidate_unreadable`, `candidate_absent`, `diff_unpinnable`, `diff_failed`, `diff_unreadable`) — and,
where that step wrote one, the `detail` line beside it. One that reached no reading carries neither: the size gate
also parks on a pinned record too damaged to act on and on a debt no push can pay, and what those hold instead is the
sentence they were about to tell a human, which is prose and has no field here. Every one of them is still
`event: late_failure` with `failure: measurement_failed`, so a filter written against that pair matches all of them
and the two fields only ever *narrow* what an analysis can group by. `events.measurement_failure_event` is the single
constructor the emitters go through: it records the step only when it is a member, drops the line with it, and
reduces what survives to what the contract accepts rather than letting it be refused there — and a refused record is
the only account there is of a reading that never happened.

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
- **The publication it was entered on** — `publication`, on every family's record: `pre_publication` for a candidate
  the gate stopped before anything was published, `post_publication` for one whose cumulative diff took a pull request
  the remote already carries past the ceiling. Both halves are spelled, so an analysis groups on the field rather than
  on whether it is there. A `post_publication` record carries the three fields the entry froze beside it —
  `source_stage` (the state the gate took the issue out of, as the bare tag the envelope's own `stage` is spelled by),
  `published_pr_number`, and `published_sha` (the head that pull request was left standing on). A `pre_publication`
  record carries none of the three: the context travels only under the marker that claims it, so a record reporting an
  initial publication cannot also carry an existing pull request's context.
- **The commits** — `source_sha` (the frozen candidate) and `base_sha` (the exact remote base it was measured against).
- **The measurement** — `threshold` and `additions`.
- **Family fields** — `verdict` (`single` / `split` / `question`), `category`, `child_count`, `failure` (the typed
  reason) with `measurement_failure` and `detail` where a refused size reading named them,
  `resource` (`snapshot_ref` / `branch` / `plan_pr` / `child`) with `resource_id` and `outcome` — the
  ledger's own state vocabulary, projected verbatim: `pending` / `retained` / `reclaiming` / `reconciled` /
  `failed` — plus `restart_step` (`pending` / `reconciled`), and `restart_target` +
  `predecessor_cycle_id`.

Extras whose value is `None` are dropped by both envelope builders, so each family carries only what applies to it.

**What is deliberately absent.** No file paths, no diff content, no prompt, no agent rationale, no agent output, and
none of a pull request's own text.
The payload has no argument any of those could arrive through — it is built from a frozen record and a family-typed
event — and every field that could otherwise smuggle text through is closed at the boundary:

- **`detail`** is the one exception and the only free-text field on a late record, so it is bounded rather than
  closed. It is the git layer's own diagnostic and nothing an agent writes: one line — git names the fault first and
  spends what follows on advice, hints, and the remote's banner — capped at 200 characters, and already scrubbed of
  the credential by the transport that produced it, since the same line is what the park notice on the issue shows a
  human. It travels only under `measurement_failure`, and that is enforced rather than documented: no other family,
  no other failure member, and no refusal that named no step has a field to reach a sink through. "One line" is asked
  as `splitlines` rather than as a search for `\n`, because a lone carriage return, a form feed, and the two Unicode
  separators each start a new line in a terminal, in `jq`, and in a value read back out of Postgres — so a value
  arriving as a transcript under any of them, untrimmed, or past the cap is refused by the event contract rather than
  written. What it buys is the question the member cannot answer: a base the remote would not name is an expired
  token, a repository this installation cannot see, or a host that was down, and by the time anybody reads the record
  the process that saw that stderr is minutes and a tick gone.

- **A verdict `category`** is a member of `LateVerdictCategory` (`generated_artifacts`, `scope_ambiguous`,
  `unsafe_split`, `lineage_bound`, `unknown`), not a label an agent writes. `events.verdict_category` maps a parsed
  answer onto it and answers `unknown` for everything it does not recognize, so an adjudication's rationale — the
  sentences, the file names in them — has no path into a record. Widening the vocabulary is an edit here, in review.
- **A resource's own name** — a ref, a branch, an issue number — is not recorded. What identifies it is `resource_id`,
  the bounded 12-character fingerprint `identity.resource_fingerprint` takes over the entry's kind and target. It is
  stable across retries of one resource and distinct between two of the same kind, which is what lets a consumer tell
  two children's cleanups apart without being told which children they were.
- **A pull request's own text.** What a `post_publication` record says about the pull request it overflowed is the
  number, the head it was standing on, and the stage the issue came from — three bounded fields. The title, the body,
  and the body the adjudication holds in pinned state (`late_plan_pr_body`) are free-form text a human and an agent
  both write into, and no argument here reaches them. The head a hold was taken over (`late_plan_pr_head`) is bounded
  and still not recorded: it is the hold's own reading, and nothing a record is correlated by.
- **The generation's own fields** are checked by `workflow/late_split/validation.py` before anything is built, because
  `candidate_sha`, `base_sha`, `published_sha`, `phase`, `source_stage`, and `restart_target` are typed `str` — or
  annotated with a vocabulary that a lookalike string satisfies every comparison of — and would otherwise be written
  verbatim. A commit field must be a whole git object id — 40 or 64 hex, since nothing here records an abbreviation —
  a phase and a source stage must be actual members, a restart target must be one of the two labels a restart may
  apply, and every count must be a real non-negative integer. The four correlating identities — `cycle_id`,
  `generation`, `root_issue`, `current_issue` — are **required**: a record nothing can be joined to is not one this
  domain writes. So is the group a publication marker stands over: a generation flagged `post_publication` that
  cannot name its stage, its pull request, and its head is refused rather than recorded, since a record claiming an
  overflow it cannot describe would reach both sinks as a post-publication entry with no publication in it. A refusal
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
is not written. `late_transfer` is held to the two commits and to neither count, since what it records is precisely
that a reading did not have to be taken; it is also the one family held to the **marker**, because a verdict moves
only onto a commit a pull request the remote already carries has been pushed to, so a transfer that could not name
that pull request is a move nothing could be attributed to. The other five families describe reconciliation rather
than size, and a restart's fresh cycle has deliberately let its commits go, so none of them is held to any of it.

**A refused record is a non-emission, not an exception.** `telemetry.emit_late_event` runs the build inside the same
fail-open guard as the two writes: the refusal is logged on the `orchestrator.workflow` channel, the call returns an
empty payload, neither sink is touched, and the tick that asked carries on. The log line is held to the same boundary
the record is — an issue number that is not one and a family that is not a member are reported as `?` rather than as
they arrived, since a log is the same surface one step over from the sinks it was protecting. That extends to why the
record was refused: this domain's own refusals are built from field names, vocabularies, and type names, so their
message is repeated, while an exception raised anywhere below is named by its type alone. Nothing renders the
exception itself — `log.exception` would append its text and traceback, and only a refusal this domain built is
guaranteed safe to repeat.

**Postgres ingestion.** `analytics_events` has no column for a late field and needs none. `stage` is already a
promoted column; every field in `LATE_PAYLOAD_FIELDS` — `measurement_failure` and `detail` with the rest — lands in
`extras JSONB` with **no DDL change and no schema reapply**, the same path the budget and opt-in skill fields take
(see [`analytics-database.md`](analytics-database.md#schema)). So widening this record is an emitter change: a
database already carrying rows from an older orchestrator keeps answering, and one carrying rows from a newer one
loses nothing to a column it does not have.

**Duplicates.** Records are emitted before the step they describe is durable, so a crash can produce the same record
twice. Consumers deduplicate on `records.CORRELATION_FIELDS`, which is **the whole record apart from `ts`**: the four
envelope fields (`repo`, `issue`, `event`, `stage`) plus every field in `LATE_PAYLOAD_FIELDS`. A retried step writes
every field again identically, so the timestamp is the only thing that can differ between one step's two emissions,
and any other difference is a different step by construction — one candidate split into two children and into seven,
two questions asked under different categories, two cleanups of two children, two restarts aimed at different states,
two measurements against different bases, two refused readings that stopped at different steps or were reported by
different lines, an initial publication and the overflow of a pull request that already exists. Naming the
distinguishing fields one at a time instead is what let pairs like those collide, because the list
has to be remembered every time the payload grows. Nothing about workflow disposition may depend on delivery, which
is the other half of the same rule.

**Fail-open, twice.** Both sinks already swallow a filesystem refusal, and each emission additionally rides its own
guard on the `orchestrator.workflow` channel, so a failing sink costs the record and nothing else — and a failure on
one side does not skip the other. A late generation is reconciled from its pinned state
([`state-machine/labels-and-state.md#late-generation-state`](../state-machine/labels-and-state.md#late-generation-state)),
never from what a sink accepted.
