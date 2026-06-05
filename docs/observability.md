# Observability

The orchestrator emits two independent JSONL sinks plus an optional Postgres aggregation target. None are read by the polling tick — workflow correctness keys off the pinned `<!--orchestrator-state ...-->` JSON comment on the issue (and the workflow label). Every observability surface here is observation-only and safe to truncate, rotate, or delete at any time.

- **Audit event log** (`EVENT_LOG_PATH`) — opt-in JSONL audit of workflow events, written through `GitHubClient.emit_event`.
- **Analytics sink** (`ANALYTICS_LOG_PATH`) — project-local JSONL of raw metric records, owned by the `orchestrator/analytics/` package.
- **Analytics database** (`analytics-db/`) — operator-deployed Postgres service that is the aggregation target for the analytics sink, with an operator-driven sync CLI and a Streamlit dashboard on top.
- **Usage parser** (`orchestrator/usage.py`) — decoder for the agent CLI JSONL stdout that produces the token / cost detail the analytics `agent_exit` record carries.

## Audit event log (`EVENT_LOG_PATH`)

Optional, opt-in JSONL sink. When `config.EVENT_LOG_PATH` is set, `github._write_event_record` appends one JSON object per audit event to that file inside `GitHubClient.emit_event`; when unset (the default) the helper short-circuits to a no-op. The fake `GitHubClient` in `tests/fakes.py` calls the same helper.

**Schema.** Every record is built by `github.build_event_record` and carries `ts` (UTC ISO-8601 at second precision), `repo` (the slug `owner/name`), `issue` (issue number, int), and `event` (the kind). `stage` is included when the emitter passes one (effectively always today). Extras whose value is `None` are dropped. `json.dumps` is called with `sort_keys=True` so on-disk order is stable across writers.

**Event kinds.** Every kind is emitted through the single `GitHubClient.emit_event` chokepoint, which also appends to a capped in-memory tail (`recorded_events`, `_RECORDED_EVENTS_CAP = 500`) for tests and short-window debugging — the file is the durable record.

| `event` | Emitter | Notable extras |
|---|---|---|
| `stage_enter` | `set_workflow_label` (via `_emit_stage_enter`) for every label flip | `stage` |
| `agent_spawn` / `agent_exit` | `workflow._run_agent_tracked` wraps every `run_agent` call (decomposer, implementer, reviewer, dev-resume, conflict-resolution dev) | `agent` (backend), `agent_role`, `review_round`, `retry_count`. `session_id` and `agent_exit`-only fields are described below the table. |
| `review_verdict` | `_handle_validating` after `_parse_review_verdict` reads the reviewer's last message | `verdict` (`approved` / `changes_requested` / `unknown`), `review_round`, `pr_number`, `session_id` |
| `park_awaiting_human` | every `_park_awaiting_human` call site, plus `_on_question`, `_on_dirty_worktree`, `_park_verify_failure`, and the question-stage `_park_question` funnel | `stage` (read from the current workflow label, not passed in), `reason` (e.g. `agent_timeout`, `push_failed`, `failed_checks`, `agent_question`, `dirty_worktree`, `reviewer_timeout`, `verify_failed` / `verify_timeout` / `verify_dirty` / `verify_head_changed`, `question_*`, ...) |
| `pr_opened` | `_on_commits` after `gh.open_pr` succeeds | `pr_number`, `branch`, `sha`, `retry_count` |
| `pr_merged` | External merge terminal arcs in `_handle_in_review`, `_handle_fixing`, `_handle_resolving_conflict`; plus `_finalize_if_pr_merged` from `_handle_implementing` / `_handle_documenting` / `_handle_validating` entry checks and from the `_handle_blocked` / `_handle_umbrella` manually-closed child recovery | `pr_number`, `sha`, `merge_method="external"`, `review_round`, `conflict_round`, `retry_count`; `stage` reflects the workflow label at finalize entry |
| `pr_closed_without_merge` | `_handle_in_review`, `_handle_fixing`, `_handle_resolving_conflict` when the PR is closed without merge; plus `_finalize_if_issue_closed` from `_handle_implementing` / `_handle_documenting` / `_handle_validating` entry checks (only when the linked PR is also closed; an open PR with a manually-closed issue is left alone) | `pr_number`, `sha`, `review_round`, `conflict_round`, `retry_count`; `stage` reflects the workflow label at finalize entry |
| `merge_attempt` | Every `git rebase origin/<base>` inside `_handle_resolving_conflict` | `method="base_rebase"`, `result` (`success` / `failed` / `conflict`), `pr_number`, `sha`, `conflict_round`, `review_round`, `retry_count` |
| `conflict_round` | `_route_pr_worktree_to_resolving_conflict` emits `action="entered"` only when the refresh-time rebase actually leaves conflicted files (a merely-behind-base clean rebase no longer emits this); every increment site (`_emit_conflict_round_incremented`) emits `action="incremented"` with `outcome` | `pr_number`, `conflict_round`, `review_round`, `retry_count`, `outcome` (for increments), `sha` |
| `base_rebased` | `_sync_pr_worktree_to_base` after a clean refresh-time rebase + push that routes the issue from `validating` / `documenting` / `in_review` / `fixing` back to `validating`; also `_recover_pending_auto_base_rebase` when a crashed prior tick is finalized | `pr_number`, `sha` (new head), `method` ∈ {`auto_clean_rebase`, `crash_recovery_pushed`, `crash_recovery_relabel_only`}, `review_round` (post-reset, so 0), `retry_count`; `stage` reflects the workflow label at the start of the rebase |

**`agent_spawn` / `agent_exit` extras.** On top of the shared fields:

- On `agent_spawn`, `session_id` is the resume session id and is OMITTED for fresh spawns (`resume_session_id=None` is dropped by `build_event_record`).
- On `agent_exit`, `session_id` is the result id from `AgentResult`. `agent_exit` additionally carries `duration_s`, `exit_code`, and `timed_out`.

**No built-in rotation.** `_write_event_record` reopens the file in append mode for every event after `path.parent.mkdir(parents=True, exist_ok=True)`; there is no long-lived file descriptor, no size cap, no rename, and no compression. External rotation is operator-managed — pair `EVENT_LOG_PATH` with `logrotate` (or equivalent). Because each append re-resolves the path, create/rename-style rotation is as safe as `copytruncate`: the next event picks up the new inode without any `SIGHUP` or restart.

An `OSError` during the append is caught and downgraded to a `log.warning` so a misconfigured path (read-only mount, disk full, permission failure) cannot stop the per-issue tick from making progress; the missing record is silently dropped and the pinned state on GitHub remains correct.

**Pinned state is authoritative.** The event log is append-only and observation-only. The orchestrator never reads it back; every dispatch decision keys off the pinned `<!--orchestrator-state ...-->` JSON comment on the issue (and the issue's workflow label). If the two disagree, trust pinned state. The append-only log is safe to truncate or delete at any time without affecting workflow correctness.

## Analytics sink (`ANALYTICS_LOG_PATH`)

Project-local JSONL sink for raw metric records, separate from `EVENT_LOG_PATH`. Opts in or out independently via `ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS` and the helpers in `orchestrator/analytics/`.

**Settings ownership.** `ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`, and `ANALYTICS_DB_URL` are parsed at import inside `orchestrator/analytics/__init__.py` — *not* in `orchestrator/config.py` — and exposed as module attributes (`analytics.ANALYTICS_LOG_PATH`, etc.). Tests patch them directly via `patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`. The audit event log (`config.EVENT_LOG_PATH`) stays in `config` because `GitHubClient.emit_event` is a general-purpose audit surface.

**Filesystem only.** No PostgreSQL, Streamlit, or external services — the sink is one JSONL file under the project log area. Default path is `<LOG_DIR>/analytics.jsonl`, already covered by the `logs/` `.gitignore` rule. Set `ANALYTICS_LOG_PATH=` (empty) or to `off` / `disabled` / `none` to disable writes entirely; in that mode `append_record` and `prune_old_records` are silent no-ops and no file is opened.

**Schema.** Every record is built by `analytics.build_record` and carries `ts` (UTC ISO-8601 at second precision), `repo` (the slug `owner/name`), `issue` (issue number, int), and `event` (the kind). `stage` is included when the caller passes one; extras whose value is `None` are dropped. `json.dumps` uses `sort_keys=True` so on-disk order is stable. The JSONL file is the raw foundation layer for the Postgres aggregation step.

**Event kinds written today:**

| `event` | Emitter | Notes |
|---|---|---|
| `stage_enter` | `GitHubClient._emit_stage_enter` alongside the audit `stage_enter` | one record per workflow label transition; carries `stage` |
| `stage_evaluation` | `workflow._process_issue` dispatcher (try/except/finally wrapper) | carries `stage`, `duration_s` (handler wall-clock), `result` (`"ok"` / `"error"`); omitted for `backlog`-skipped issues (no handler runs) |
| `agent_exit` | `workflow._run_agent_tracked` | one record per tracked agent invocation; agent context + parsed token / model / cost details (see below) |

**Append.** `analytics.append_record(record)` reopens the file in append mode for every record after `path.parent.mkdir(parents=True, exist_ok=True)`. An `OSError` is caught and downgraded to a `log.warning`.

**Retention pruning.** `analytics.prune_old_records(*, now=None)` reads the file and removes records whose `ts` is older than `ANALYTICS_RETENTION_DAYS`. No-op (returns `0`) when the sink is disabled, retention is non-positive, or the file does not exist. The rewrite goes through a temp file followed by `os.replace` so a crash mid-prune cannot truncate the analytics file. Records with a missing / non-string / unparseable `ts` (and any line that is not valid JSON) are preserved verbatim so the prune step never silently drops data it cannot interpret.

**Append/prune serialization.** Append and prune share a process-local `threading.Lock` inside the analytics module so a concurrent `append_record` cannot land between the prune's read and its `os.replace`. Under the scheduler-driven dispatch, `workflow.tick` returns as soon as it has submitted per-issue callables, so scheduler workers may still be running — and calling `append_record` — when `main._run_tick` invokes `prune_with_retention_logging()`. Without the lock, an append that opened the old inode after the prune's read but before the replace would be silently lost. The lock is held only around the filesystem ops; JSON serialization happens outside the critical section.

**Retention cadence.** `main._run_tick` calls `analytics.prune_with_retention_logging()` exactly once per polling iteration after `workflow.tick` returns for every configured repo, regardless of how many repos are configured — the sink is process-wide, not per-repo. Right before the prune, `_run_tick` calls `scheduler.reap()` exactly once per polling pass so worker failure-completion records drain before the next iteration. `_dispatch_via_scheduler` deliberately does NOT reap. The wrapper catches exceptions and logs the `"removed N record(s)"` message so the call site in `main` stays a one-liner. Per-tick cost is bounded: the helper reads the file at most once and only rewrites it when at least one record is older than the retention window.

**Pinned GitHub state is unaffected.** The prune touches only the local file — no issue comment, label, or other GitHub state is rewritten. The analytics sink is local-filesystem observability and is safe to truncate or delete at any time.

### `agent_exit` records

`workflow._run_agent_tracked` appends a single `event="agent_exit"` analytics record after every tracked agent run, distinct from (and in addition to) the audit `agent_spawn` / `agent_exit` events on `EVENT_LOG_PATH`. Each record carries:

- **Context** — `repo`, `issue`, `stage`, `agent_role`, `backend`, `review_round`, `retry_count`, `duration_s`, `exit_code`, `timed_out`.
- **Spec / session** — the configured `agent_spec` (the role's full `*_AGENT_SPEC` string, e.g. `claude --model claude-opus-4-7`), both the `resume_session_id` passed into the spawn and the live `session_id` from the result.
- **Usage parser output** — `input_tokens`, `output_tokens`, `cached_tokens`, `cache_read_tokens`, `cache_write_tokens`, the distinct `models` observed in the stream, `turns`, `cost_usd`, and `cost_source`.

The configured model is pulled out of the role's `extra_args` (via `_configured_model`; recognises `-m <model>` / `-m=<model>` for codex and `--model <model>` / `--model=<model>` for claude) and forwarded as the parser's `fallback_model` so a codex run whose stdout includes usage frames but omits the model still records the configured model and — when it matches a priced family — an estimated `cost_usd`. A stream-reported model always wins over the fallback.

Prompts, raw stdout / stderr, secrets, and worktree contents are deliberately NOT stored — the sink is a usage / cost surface, not a debugging mirror. A parser exception or sink IO failure is swallowed so an analytics misconfiguration cannot stop the per-issue tick.

## Analytics database (`analytics-db/`)

Local Postgres service that is the aggregation target for the JSONL sink. The service contract and schema are operator-deployed via Docker compose; the JSONL→Postgres replay is implemented in `orchestrator/analytics/sync.py` as an operator-driven CLI — NOT wired into the polling tick. Orchestrator correctness must not depend on database availability.

### Service layout

[`../analytics-db/compose.yml`](../analytics-db/compose.yml) brings up a single `postgres:16` container with the data directory on a host bind (`./data`, gitignored) and the init directory mounted read-only. The port binding is pinned to `127.0.0.1` so the database is unreachable off-host regardless of firewall configuration; re-binding to `0.0.0.0` is intentionally a code change rather than an env-var change. Credentials default to `orchestrator` / `orchestrator` and are overridable via `analytics-db/.env` (`POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_PORT`). `docker compose` reads `.env` from the compose-file directory, not the orchestrator root.

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

The sync reads a single libpq URL — `ANALYTICS_DB_URL` (default unset, example `postgresql://orchestrator:orchestrator@127.0.0.1:5432/orchestrator_analytics`) — rather than separate host / port / user / password variables. Moving the database off-host later (managed Postgres, a different VM, a unix socket) is a one-line repoint. Empty value and the sentinels `off` / `disabled` / `none` (case-insensitive) disable the sync, matching `ANALYTICS_LOG_PATH`.

### Schema

[`../analytics-db/init/01-schema.sql`](../analytics-db/init/01-schema.sql) defines:

- **`analytics_events` table.** Columns mirror the JSONL record shape produced by `analytics.build_record`. `ts`, `repo`, `issue`, `event` are `NOT NULL`; everything else is nullable so any record across the three event kinds is a valid row. An `extras JSONB` column captures any field added to `build_record` before the DDL knows about it. `source_path` / `source_line` are forensic context; the authoritative dedup key is `content_hash` — SHA-256 over the canonical (`sort_keys=True`) JSON form of the record.
- **Indexes.** A plain (non-partial) unique index on `content_hash` plus `INSERT ... ON CONFLICT (content_hash) DO NOTHING` makes repeated sync runs idempotent. Additional indexes cover the expected query dimensions: `ts`; `(event, ts)`; `(repo, issue)`; a partial index on non-null `stage`; per-event-kind partial indexes on `(repo, ts DESC)` for `event='agent_exit'` and `event='stage_enter'`; and a composite `(event, repo, stage, ts)` index.
- **`analytics_daily_rollup` materialized view.** Keyed on `(day, repo, issue, event, stage, backend, cost_source)` and carrying the aggregates the dashboard's window-bounded widgets need without re-scanning `analytics_events`: token totals (`total_input_tokens`, `total_output_tokens`, `total_cached_tokens`, `total_cache_read_tokens`, `total_cache_write_tokens`), `total_cost_usd`, `duration_s_sum` + `duration_s_count` (so consumers recover `AVG(duration_s)` as `sum / count`), `failed_count` (rows with non-NULL non-zero `exit_code`), `timed_out_count` (scoped to `event='agent_exit'` with `timed_out=TRUE`), and `event_count`. `day` is `(ts AT TIME ZONE 'UTC')::date`. A unique index on the full key (`NULLS NOT DISTINCT`, Postgres 15+) backs the rollup; a `(day, repo)` supporting index keeps `WHERE day BETWEEN x AND y` predicates on a range scan.
- **`analytics_agent_runs` view.** `CREATE OR REPLACE VIEW` over `event = 'agent_exit'` rows that promotes derivations: `model` from `COALESCE(models->>0, 'unknown')`, `total_tokens` = `input + output`, `total_cache_tokens` = `cached + cache_read + cache_write`, a categorical `review_round_bucket` (`0`, `1`, `2`, `3-5`, `6+`), `failed = exit_code <> 0` (NULL preserved), and `has_cost = cost_usd IS NOT NULL` (true for `cost_source` in {`reported`, `estimated`}). Raw nullable columns pass through alongside derived ones; `cost_source` passes through verbatim.

The init script runs once when the data volume is empty. `IF NOT EXISTS` guards plus trailing `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `CREATE UNIQUE INDEX IF NOT EXISTS` for `content_hash` keep it idempotent for the operator-driven case (`psql -f` against an existing instance) and migrate a pre-`content_hash` data volume without dropping data. MV column changes require `DROP MATERIALIZED VIEW analytics_daily_rollup` followed by a reapply; the sync's refresh hook does NOT recover from a column mismatch.

### Sync CLI (`orchestrator/analytics/sync.py`)

Run on demand:

```sh
uv run python -m orchestrator.analytics.sync                                                # uses configured env vars
uv run python -m orchestrator.analytics.sync --log-path /path/to/rotated.jsonl --db-url postgresql://other/db
```

**Batched inserts.** Reads `ANALYTICS_LOG_PATH` line by line, accumulates validated row tuples into a `_BATCH_SIZE`-sized buffer (default 500), and flushes each full batch via `cur.executemany("INSERT ... ON CONFLICT (content_hash) DO NOTHING", batch)`. A multi-thousand-record replay pays one Postgres round-trip per batch instead of one per row. A final partial batch is flushed at EOF so the tail still lands.

**Pre-check dedup.** Before opening the input file the sync issues a single `SELECT content_hash FROM analytics_events WHERE content_hash IS NOT NULL` and pulls the result into a Python set, so already-present rows are filtered out before they enter the batch. Newly queued hashes are added to the same set as the loop iterates, so two identical records inside one JSONL file are deduped against each other before reaching `executemany`. The pre-check reads from the unique `analytics_events_content_hash_idx`. The server-side `ON CONFLICT (content_hash) DO NOTHING` arbiter stays the authoritative dedup backstop for racing concurrent writers.

**Counters.** Per-batch `cur.rowcount` drives the cumulative `inserted` / `skipped_duplicate` totals. Duplicates = `len(batch) - rowcount` for wire-side skips, plus the pre-skip counter for in-Python skips.

**Malformed-line tolerance.** Blank lines are silently skipped; lines that are not valid JSON, JSON that is not an object, records missing one of the required (`ts` / `repo` / `issue` / `event`) keys, or carrying an unparseable `ts` are counted as skipped and logged but never enter the batch buffer. The JSONL file is treated as read-only — the sync never rewrites or truncates it, even when it sees malformed lines. Naive timestamps are interpreted as UTC.

**Transaction shape.** A `psycopg` driver-level error inside a batch flush rolls the transaction back and propagates so the CLI exits non-zero rather than reporting "success" on a half-inserted run. After the insert transaction commits, the sync issues `REFRESH MATERIALIZED VIEW analytics_daily_rollup` (non-concurrent) and commits again so the rollup-backed dashboard widgets catch up. The refresh fires unconditionally on every successful commit — including all-duplicates and all-malformed runs — so rerunning the sync is the documented recovery path for a stale rollup. A refresh exception (MV not migrated yet, transient Postgres error, lock-wait timeout) is logged via `log.exception` and swallowed; the committed inserts are durable, and the next sync's refresh recovers the rollup.

**No-op modes.** `sync_jsonl_to_postgres` is a no-op (no connection attempt, no row insertion, no error) when `ANALYTICS_DB_URL` is unset or disabled, when `ANALYTICS_LOG_PATH` is explicitly disabled (note that the env var defaults to `LOG_DIR/analytics.jsonl`, so only the empty value or `off` / `disabled` / `none` turns it off), or when the JSONL file is absent. The CLI is safe to schedule before the operator deploys Postgres. The driver is `psycopg[binary]`; the import is lazy inside the connect helper so the module load path remains driver-free for callers that only need `SyncResult`.

### Operator feedback

The sync surfaces feedback through the module logger and the stdout summary:

- Every log line is timestamped (UTC, with an explicit `UTC` suffix) via `_configure_cli_logging`'s `%(asctime)s` formatter and `formatter.converter = time.gmtime`.
- A `connecting to <redacted-url>` / `connection established` pair brackets the connect call so a remote-Postgres reachability problem surfaces immediately.
- A `progress lines=N inserted=… duplicate=… malformed=… elapsed=…s` record drops after each batched `executemany` flush (`_BATCH_SIZE` and `_PROGRESS_INTERVAL` are both 500, so each flush carries one progress line).
- A final `completed in %.3fs (…)` line carries the wall-clock total.
- The CLI prints a UTC-stamped stdout summary at the end carrying `inserted=` / `duplicate=` / `malformed=` / `total_lines=` / `duration_s=`.
- `ANALYTICS_DB_URL` credentials are stripped before logging — both the `user:password@` netloc form and the libpq query-string form (`?user=`, `?password=`, `?sslpassword=`, `?passfile=`, case-insensitive per libpq parameter-name rules) collapse to `***`.

### Operator workflow

Run `uv run python -m orchestrator.analytics.sync` on whatever cadence you prefer; `--log-path` and `--db-url` override the env values for one-off replays of archived JSONL files. The default cadence is operator-chosen because the JSONL sink is already the authoritative analytics surface on disk — the database is for aggregation and reporting, not durability.

For an unattended deployment, drive the sync from `cron`. A typical entry runs hourly, guards against overlap with `flock`, and captures output:

```cron
00 * * * * cd /path/to/agent-orchestrator && /usr/bin/flock -n /tmp/agent-orchestrator-analytics-sync.lock /home/<user>/.local/bin/uv run python -m orchestrator.analytics.sync --log-path /path/to/agent-orchestrator/logs/analytics.jsonl --db-url 'postgresql://<user>:<password>@<host>:<port>/<database>' >> /path/to/agent-orchestrator/logs/analytics-sync.cron.log 2>&1
```

- `cd /path/to/agent-orchestrator` so `uv run` finds the project's `pyproject.toml`.
- Absolute `/home/<user>/.local/bin/uv` because cron's `PATH` does not include `~/.local/bin`.
- `flock -n` makes the run a no-op when a previous invocation is still holding the lock, so a long replay never overlaps with the next tick.
- `--log-path` and `--db-url` are explicit CLI overrides, so the cron entry does not depend on `.env` being loadable from cron's environment.
- `>> ...analytics-sync.cron.log 2>&1` keeps stdout and stderr in the project log area instead of routing failures to local `mail`.

### Read model (`orchestrator/analytics/read.py`)

Thin, testable data-access layer over `analytics_events`, the `analytics_agent_runs` view, and the `analytics_daily_rollup` materialized view. The dashboard's window-bounded aggregates read from the rollup; per-row drill-downs and widgets the rollup cannot reconstruct exactly stay on the base table or the agent-run view. The module is Streamlit-free so the read path can be wired into any UI.

| Function | Source | Returns |
|---|---|---|
| `get_summary` | rollup | date-bounded totals + per-event / per-stage breakdowns + token / cost sums, plus `total_agent_runs` / `failed_agent_runs` / `timed_out_agent_runs` scoped to `event='agent_exit'`. `distinct_issues` is `COUNT(DISTINCT (repo, issue))`. Single round-trip via `WITH win AS (...)` CTE with three `UNION ALL` branches tagged by a `kind` discriminator. |
| `get_kpi_prev` | rollup | stripped variant of `get_summary` returning only the cost / token / agent-run scalars the dashboard reads off `prev_summary` for KPI deltas. Skips the `COUNT(DISTINCT)`s and `GROUP BY` follow-ups; ~one aggregate scan instead of three. |
| `get_time_series` | rollup | daily `(day, event, count)` rollups with per-cell cost / input / output / cache_read / cache_write token aggregates. |
| `get_stage_breakdown` | rollup | per-stage counts + weighted `AVG(duration_s)` recovered as `SUM(duration_s_sum) / NULLIF(SUM(duration_s_count), 0)`, rolled-up cost / token totals, and a `runs` agent-exit subset count. |
| `get_repo_breakdown` | rollup | per-`repo` rollup of issues / events / agent-exits / cost. |
| `get_backend_efficiency` | rollup | per-backend runs / failed / avg duration / cost / token totals with NULL backends surfaced as `"unknown"`. `event = 'agent_exit'` is pinned in the WHERE clause. |
| `get_throughput_breakdown` | rollup | daily resolved / rejected counts over `stage_enter` rows whose `stage` is `done` or `rejected`. Short-circuits when the events multiselect excludes `stage_enter` or the stages selection excludes both terminals. |
| `get_filter_options` | base table | distinct repos / events / stages / backends / agent_roles for dropdowns. All five columns pulled in a single `UNION`'d round-trip with rows tagged by their column. |
| `get_data_extent` | base table | min / max `ts` so the sidebar date picker defaults to a window that contains rows. |
| `get_event_breakdown` | base table | per-event counts (the rollup pre-aggregates more finely than `event` alone, so the base-table read is cheaper here). |
| `get_recent_agent_exits` | base table | newest rows filtered to `event='agent_exit'`. |
| `get_issues` | base table | date / repo-bounded one-row-per-`(repo, issue)` overview: event count, first / last activity, latest non-null stage, agent-exit count, cost / token totals, `max_review_round`, `failed_agent_runs`, `max_retry_count`. Bounded by `limit` and ordered by `sort_by` (`"last_seen"` default, `"cost"` orders by `SUM(cost_usd) DESC NULLS LAST`; unknown `sort_by` raises `ValueError`). |
| `get_issue_events` | base table | full event trace for a single `(repo, issue)` pair, oldest first. |
| `get_hourly_heatmap` | base table | 7×24 weekday/hour activity cells from `EXTRACT(DOW)` / `EXTRACT(HOUR)` over `(ts AT TIME ZONE 'UTC') + tz_offset_hours * INTERVAL '1 hour'` (normalizing first guards against a non-UTC session timezone re-shifting the buckets) with per-cell event count + `input + output + cache_read + cache_write` token total. `tz_offset_hours` (default `0`, parameter binding only — never spliced) lets the dashboard bucket in a non-UTC zone. |
| `get_review_round_breakdown` | agent-run view | per `review_round_bucket` runs / failed counts + `total_cost_usd`. NULL buckets surface as `"unknown"`. |
| `get_backend_daily_tokens` | agent-run view | per `(day, backend)` token totals feeding the hero chart's "By backend" stacked-area toggle. |
| `get_cost_coverage` | agent-run view | per `cost_source` rollups carrying both runs and `total_tokens`. The `unknown-price` cohort is exposed verbatim (never collapsed into a generic "unknown") because it is the maintenance signal for the pricing table in `orchestrator.usage`. NULL `cost_source` buckets under `"unknown"`. |

**Filter contract.** The agent-run view has no `event` column (its WHERE `event = 'agent_exit'` is baked in), so view-backed functions cannot push an `event IN (...)` clause down. They honor the dashboard's event-filter contract by short-circuiting to empty when the operator's events selection excludes `agent_exit` (or is cleared). Rollup readers preserve the same contract through `_build_rollup_window_where`, which emits a tautologically-false predicate on a cleared multiselect and a parameterised `IN (...)` on a non-empty one.

The rollup window helper translates the dashboard's midnight-aligned UTC `[start, end)` datetimes to `day >= start.date() AND day < end.date()` predicates so the `(day, repo)` index drives a date-range scan. Sub-day-aligned bounds collapse to day granularity (the rollup carries no finer resolution), but the dashboard never passes those.

**Connection model.** Each function returns a frozen dataclass or list of dataclasses. `ANALYTICS_DB_URL` unset short-circuits every function to an empty / zero-valued result with no connection attempt, mirroring the sync's no-op contract. Connection or query failures (driver-level psycopg errors, schema mismatches, network unreachable) are wrapped in a single `AnalyticsReadError` whose `__cause__` preserves the underlying exception. The psycopg import is deferred to call time inside `_default_connect`; tests inject a fake `connect(db_url) -> connection` factory.

Every public reader accepts an optional `conn=` so a caller (typically the dashboard, inside an `analytics_connection` scope) can run many reads on a single shared connection instead of paying the ~1 s psycopg handshake per call; absent `conn=`, the open-per-call / close-in-`finally` path runs unchanged. A caller-supplied `conn=` always wins over the URL short-circuit.

`analytics_connection(*, db_url=None, connect=None)` is a context manager that maintains a single thread-local persistent connection. The first `with` block opens the socket (real psycopg connections open with `autocommit=True`); subsequent `with` blocks on the same thread reuse it; a broken-connection error (`OperationalError` / `InterfaceError`) inside the scope close-and-replaces the cached socket before re-raise. `close_thread_local_connection()` drains it explicitly for shutdown hooks or test teardown. The thread-local cache is keyed on the resolved URL: a later `with` block on the same thread requesting a different `db_url=` closes the stale socket first. The connection is not part of any Streamlit cache key (a raw `psycopg.Connection` is not hashable). Close-time exceptions are logged and swallowed.

The read model is deliberately separate from `analytics/sync.py`: the sync owns the JSONL → Postgres write path, while reads have a different error story and injection shape.

### Dashboard (`orchestrator/dashboard.py`)

Streamlit app over the read model. Opt-in via the `dashboard` dependency group so the default `uv sync --locked` keeps installing only the polling runtime plus `pytest` / `ruff`. Streamlit (and its transitive pandas), `plotly`, the Plotly figure builders in `orchestrator/dashboard_charts.py`, and the plotly-free theme tokens in `orchestrator/dashboard_theme.py` are imported lazily inside `main()` — importing `orchestrator.dashboard` from a test or non-dashboard caller does not require the group to be installed. A regression-guard test in `tests/test_dashboard.py` asserts that loading `orchestrator.dashboard` keeps `streamlit`, `pandas`, `plotly`, and `orchestrator.dashboard_charts` out of `sys.modules`.

```sh
uv sync --group dashboard                                  # install streamlit + plotly alongside the runtime + dev deps
uv run streamlit run orchestrator/dashboard.py             # launches a local browser tab
```

**Page chrome.** A sticky topbar carries the page title with the data extent / repo / event summary on the left and the in-range spend pill on the right. A sticky filter bar exposes `3D` / `7D` / `All` inline presets (anchored at the data extent's max timestamp and clamped to its min) plus two date inputs for arbitrary windows within the extent. The sidebar surfaces a `Custom` preset fallback, a repo selector, event / stage multi-selects, and a `#123` / `123` issue-number input.

**Caching.** Every per-filter read is wrapped in `st.cache_data` keyed by `(start, end, repo, events, stages, issue)`, so a filter change invalidates every cached query in lockstep. `get_data_extent` and `get_filter_options` carry no filter inputs and live in argument-less wrappers under the longer `STATIC_METADATA_TTL_SECONDS = 300` (5 min) TTL so the sidebar / topbar only re-hit Postgres when `analytics.sync` ingests new events.

**Two-wave loading.** The 13 widget reads are staged into two waves:

- **First wave (6 reads).** `summary`, `prev_summary`, `ts_points`, `review_round_rows`, `throughput_rows`, `cost_coverage_rows` — feeds the topbar, filter meta, insight banners, and KPI strip.
- **Second wave (7 reads).** `stage_rows`, `agent_exits`, `issues_rows`, `backend_rows`, `repo_rows`, `heatmap_rows`, `backend_daily_rows` — feeds the rest of the body.

`main()` renders the above-the-fold chrome between waves on the main thread (worker threads only return data through futures, so every `st.*` write runs on the main thread). The second wave is skipped on an empty window. A single inline `st.spinner("Loading analytics…")` brackets both waves; a read error from either wave surfaces as one `st.error` + `st.stop`.

**Body layout, top to bottom:**

1. Computed insight banners (failure rate ≥ 10 %, unpriced cost coverage ≥ 10 %).
2. Four-tile KPI strip — total spend, total tokens (`input + output + cache_read + cache_write`), cost / resolved issue, rework share — each with an inline-SVG sparkline and previous-window delta where applicable.
3. Hero `usage_over_time` stacked-area + cost-line chart with a "By token type / By backend" toggle.
4. Side-by-side `cost_by_stage` and `cost_by_review_round` cards; the review-round card groups development and review cost bars per round.
5. 7/5 split: top-cost issues table (Issue with in-row cost bar, Cost, Runs, Review rds, Retries, status pill) + backend-efficiency cards (`$ / 1M tok`, `% cache hit`, `$ / run`) above the cost-source coverage bar (sized by token share).
6. Another 7/5 split: `cost_by_repo` bars + six-tile reliability panel (agent runs / success rate / resolved / rejected / failures / timeouts — all sourced from the same `Summary` window-wide aggregate) above the issues-resolved-per-day bar chart with explicit zero days backfilled.
7. 7 × 24 weekday × hour activity heatmap rendering token volume, with an in-card `UTC` offset selectbox (range `-12 … +14`, default `UTC+7`) that controls both the heatmap bucketing and the wall-clock conversion of the `ts` column in the recent agent-runs table below. The widget binds to `st.session_state["tz_offset_hours"]`; the offset is read before the second-wave fan-out so the heatmap query buckets in the chosen zone, and the card subtitle / x-axis title render the matching `UTC±N` label.
8. Recent agent-runs table as a collapsible expander; the `ts` column is shifted to the wall-clock of the selected UTC offset via `shift_ts`.
9. Per-issue drill-down when a number is entered.

**Filter contract.** `_build_window_where` distinguishes three cases for the event / stage selections: `None` is "no filter on this column", a non-empty sequence emits a parameterised `IN (...)`, and an empty sequence emits a tautologically-false predicate (`FALSE`). The event multiselect maps straight through (`event` is `NOT NULL` in the schema). The stage multiselect routes through `resolve_stage_filter(selected, available)` because `options.stages` only lists non-null stages: the all-selected default collapses to `None` so NULL-stage rows are included; an explicitly cleared selection still emits `[]`; a proper subset passes through verbatim. Without this asymmetry the default dashboard would silently exclude `stage_evaluation` rows on issues with no workflow label. The issue number acts as a SQL-level filter when a specific repo is selected AND triggers the drill-down section; with the repo filter on "All" it stays inert (GitHub issue numbers are not unique across repos).

**Parallel read fan-out.** Setting `DASHBOARD_PARALLEL_READS=on` (or `1` / `true` / `yes`, case-insensitive) flips the 13 widget reads from sequential to a `ThreadPoolExecutor` capped at eight workers. Each worker opens its own thread-local psycopg connection via `analytics.read.analytics_connection()` — `psycopg.Connection` is not thread-safe, so sharing one socket across workers would corrupt the wire protocol. The fan-out emits a single INFO log line on every dashboard load — `dashboard.load: total=X.Xs reads=13 parallel=true|false` on a full render, or `reads=6` when the empty-window short-circuit skips the second wave — so the two paths can be A/B'd with `grep dashboard.load streamlit.log`. An `AnalyticsReadError` raised by any worker propagates verbatim from the first failing future.

**Chart builders.** `orchestrator/dashboard_charts.py` exposes pure Plotly figure builders: `usage_over_time` (stacked-area + cost-line overlay with `mode="type"` / `mode="backend"` switch), `cost_horizontal_bars` (shared primitive), `cost_by_stage` / `cost_by_repo` (thin adapters over `cost_horizontal_bars`), `cost_by_review_round` (grouped development/review bars per round), `hour_weekday_heatmap` (faint-to-saturated accent gradient over per-cell token totals, Sunday-first, with a `tz_label` parameter that annotates the x-axis — the caller passes the matching offset to `get_hourly_heatmap` so cells already reflect that zone), and `done_per_day_bars` (resolved-per-day bars with explicit `window_start` / `window_end` for zero-day backfill). The cost-source coverage bar, backend-efficiency cards, KPI strip, topbar, and insight banners are rendered directly from HTML strings in `dashboard.py`.

**Theme.** `orchestrator/dashboard_theme.py` is a plotly-free token module: palette (cool gray `#f4f5f8` page, white cards, indigo accent, muted ink tints), spacing tokens, the `1480px` content max-width, per-token-type / per-backend / per-agent-role / per-review-round / per-stage / per-`cost_source` palettes, a shared `base_layout(title=...)` Plotly dict, the `PAGE_CSS` string the dashboard injects through `st.markdown(unsafe_allow_html=True)`, and the `fmt_money` / `fmt_money_exact` / `fmt_tokens` / `fmt_num` formatters. `.streamlit/config.toml` mirrors the palette into Streamlit's `[theme]` and disables the `[browser] gatherUsageStats` POST so the launch stays local-observability-only.

**Independence.** The dashboard process is independent of the polling tick: it does not open a GitHub session, does not write to Postgres, and can be deployed off-host by repointing `ANALYTICS_DB_URL` at a managed Postgres endpoint without changing the orchestrator's deployment.

### Empty and error states

The dashboard never raises an unhandled exception at the user — every missing-data or misconfiguration case surfaces as a labeled banner.

| In-app message                                                                                   | Layer            | Likely cause and fix                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------------------------------------------------ | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `` `ANALYTICS_DB_URL` is not configured. … `` (top-level `st.warning`, app stops)                | env              | `ANALYTICS_DB_URL` is unset, empty, or set to `off` / `disabled` / `none`. Set it in `.env` and **relaunch** `streamlit run orchestrator/dashboard.py` (the dashboard reads the URL from the imported analytics module at startup, so a browser reload alone will not pick up the new value).                          |
| `Could not load analytics filter options: …` (top-level `st.error`, app stops)                   | DB connectivity  | The dashboard could not reach Postgres at startup. Confirm `docker compose ps` shows `analytics-db` healthy, that the host / port / credentials in `ANALYTICS_DB_URL` match `analytics-db/.env`, and that the user can connect with `psql`.                                                                          |
| `Analytics query failed: …` (top-level `st.error`, app stops)                                    | DB schema / I/O  | A read query raised mid-render. Most commonly the `analytics_events` table is missing — either the volume is fresh and the init script has not been applied (`docker compose down && docker compose up -d`) or a manual schema reapply is needed (see [Service layout](#service-layout)).                            |
| `No analytics events have been recorded yet. …` (top-level `st.info`, app stops)                  | data             | The `analytics_events` table holds zero rows. Confirm the JSONL sink is on (`ANALYTICS_LOG_PATH`), that recent workflow activity produced records, and run `python -m orchestrator.analytics.sync` to populate Postgres.                                                                                            |
| `No analytics events match the current filters.` (page banner)                                    | data             | The data extent is non-empty but every row was filtered out. Widen the window preset, pick `All` for the repo, blank the issue-number input, and confirm the event / stage multi-selects still have **every option selected** (an empty multi-select is the documented "show nothing" signal).                       |
| `No stage data matches the current filters.` (chart annotation)                                   | data             | Scoped to the stage breakdown chart. Also empty when the only matching rows have a NULL stage (`stage_evaluation` records on issues with no workflow label).                                                                                                                                                        |
| `` No `agent_exit` rows match the current filters. ``                                            | data             | The window contains `stage_enter` / `stage_evaluation` rows but no agent invocations — surfaces in the review-round chart, backend cards, cost coverage bar, and recent-runs expander.                                                                                                                              |
| `No agent runs with recorded cost in this window.`                                                | data             | The top-cost issues table fell back to its empty state — no `(repo, issue)` pair in the window has any priced agent runs.                                                                                                                                                                                            |
| `No repos match the current filters.`                                                             | data             | The per-repo activity chart is empty for this filter combination.                                                                                                                                                                                                                                                    |
| `Pick a specific repo in the sidebar before drilling into an issue number …`                     | UI guard        | The issue-number input is inert with the repo filter on `All` because GitHub issue numbers are not unique across repos.                                                                                                                                                                                              |
| ``No analytics events recorded for `<repo>#<n>` under the current filters.``                     | data / filter   | The drill-down query returned nothing. Either the issue number is wrong for that repo, the orchestrator has not processed it yet, or the event / stage multi-selects exclude every row for that issue.                                                                                                              |
| `Issue drill-down failed: …`                                                                     | DB I/O           | The drill-down query raised but the headline metrics rendered first. Same fixes as `Analytics query failed: …`.                                                                                                                                                                                                     |

If a sidebar multi-select is **explicitly cleared** (no items selected), every dependent widget falls back to "no data" — that is the documented "show nothing for this dimension" signal. Re-select the items (or hit the `↺` reset chip Streamlit renders on the widget) to restore the default unfiltered shape.

If `python -m orchestrator.analytics.sync` runs cleanly (non-zero `inserted=`) but the dashboard still shows zero rows, double-check the `ANALYTICS_DB_URL` the sync used — passing `--db-url postgresql://other/db` (or a different shell environment) populates a different database than the one the dashboard is reading.

## Usage parser (`orchestrator/usage.py`)

Pure-Python helpers that decode the JSONL stdout `agents.AgentResult` carries into a `UsageMetrics` dataclass — backend, distinct model(s), turn count, input / output / cached / cache-read / cache-write token totals, `cost_usd`, and a `cost_source` tag of `reported` / `estimated` / `unknown-price` / `no-usage`. No external dependency: the parser is jq-free.

**Two parsers, one dispatcher.** `parse_claude_usage(stdout)` consumes claude `--output-format stream-json` events, groups assistant frames by `message.id` so the final-frame usage wins (claude streams partial counts on intermediate frames), and sums per-model. `parse_codex_usage(stdout, fallback_model=None)` consumes codex `--json` events and treats usage as cumulative across the session: the *last* non-zero usage record is the authoritative total. `parse_agent_usage(backend, stdout, fallback_model=None)` dispatches by backend string the same way `agents.run_agent` does.

**Cost precedence.** A `total_cost_usd` reported by the CLI itself always wins (`cost_source="reported"`); otherwise the parser walks first-party Anthropic / OpenAI price tables baked into the module and produces an estimate (`"estimated"`). When usage is present but the model SKU does not match any priced family, the parser returns `cost_source="unknown-price"` and `cost_usd=None` rather than guess at zero or bill cached tokens at the input rate. An empty stream — or one with no usage frames at all — yields `"no-usage"`.

**Resilience.** Malformed JSON lines (banner text, truncated frames, partial flushes) are silently skipped so a single bad line never invalidates the rest of the stream. `workflow._run_agent_tracked` calls `parse_agent_usage` after every tracked agent run and appends the parsed counts to the [analytics sink](#analytics-sink-analytics_log_path) under `event="agent_exit"`; a parser exception is caught and downgraded to a `log.exception`.

## Summary of "what runs when"

| Component | Type | Trigger | Cadence |
|---|---|---|---|
| `analytics.prune_with_retention_logging` | function call | end of each `main._run_tick` after every configured repo drains | once per tick (process-wide, not per-repo); no-op when the sink is disabled or `ANALYTICS_RETENTION_DAYS <= 0` |
| `scheduler.reap` | method call | end of each `main._run_tick` after every configured repo drains, immediately before the analytics prune | exactly once per polling pass regardless of repo count; nonblocking drain of any worker completions since the last poll. `_dispatch_via_scheduler` deliberately does NOT call `reap`. |
