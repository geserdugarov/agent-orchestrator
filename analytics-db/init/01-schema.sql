-- Copyright 2026 Geser Dugarov
-- SPDX-License-Identifier: Apache-2.0
--
-- Analytics database schema for the orchestrator.
--
-- This file mirrors the JSONL record shape produced by
-- `orchestrator/analytics/` (`build_record`) so a future ingestion job
-- can replay the on-disk log line-by-line into Postgres without lossy
-- reshaping. Three event kinds write today (`stage_enter`,
-- `stage_evaluation`, `agent_exit`); fields that only apply to a subset
-- of events are nullable so any single row is valid.
--
-- `extras` is a JSONB column that captures any future fields added to
-- `build_record` before this DDL knows about them; it keeps the ingest
-- path forward-compatible without requiring a migration on every new
-- analytics field. Promoted-to-column fields should be removed from
-- `extras` by the ingest job when it learns about them.
--
-- The init script is run by the `postgres` Docker image once when the
-- data volume is empty (via `/docker-entrypoint-initdb.d`). Re-running
-- the container against an existing volume is a no-op: the
-- `IF NOT EXISTS` guards make the DDL idempotent for the operator-driven
-- case (e.g. running `psql -f` against an existing instance) as well.

CREATE TABLE IF NOT EXISTS analytics_events (
    id              BIGSERIAL PRIMARY KEY,

    -- Common to every record built by `build_record`.
    ts              TIMESTAMPTZ NOT NULL,
    repo            TEXT        NOT NULL,
    issue           INTEGER     NOT NULL,
    event           TEXT        NOT NULL,
    stage           TEXT,

    -- `stage_evaluation` and `agent_exit` carry handler/agent duration.
    duration_s      DOUBLE PRECISION,

    -- `stage_evaluation` only: `"ok"` or `"error"`.
    result          TEXT,

    -- `agent_exit` invocation context.
    agent_role          TEXT,
    backend             TEXT,
    agent_spec          TEXT,
    resume_session_id   TEXT,
    session_id          TEXT,
    review_round        INTEGER,
    retry_count         INTEGER,
    exit_code           INTEGER,
    timed_out           BOOLEAN,

    -- `agent_exit` token / model / cost parse from `usage.parse_agent_usage`.
    input_tokens        BIGINT,
    output_tokens       BIGINT,
    cached_tokens       BIGINT,
    cache_read_tokens   BIGINT,
    cache_write_tokens  BIGINT,
    models              JSONB,
    turns               INTEGER,
    cost_usd            NUMERIC(20, 10),
    cost_source         TEXT,

    -- Forward-compatibility catch-all: any record field that does not
    -- have an explicit column above lands here so the ingest never drops
    -- data it doesn't recognise.
    extras              JSONB,

    -- Source line for audit / dedup. The ingest job populates this
    -- from the JSONL source filename and the 1-indexed line number so
    -- replaying the same log twice can be detected. Line numbers shift
    -- whenever `analytics.prune_old_records` rewrites the file, so
    -- these are forensic-only -- the authoritative dedup key is
    -- `content_hash` below.
    source_path         TEXT,
    source_line         BIGINT,

    -- SHA-256 over the canonical (sort_keys=True) JSON form of the
    -- record as it appeared on the JSONL line. Stable across prune
    -- rewrites, so repeated `analytics.sync` runs that re-read a
    -- pruned file do not re-insert rows whose content the database
    -- already holds. The unique index defined below combined with
    -- `ON CONFLICT (content_hash) DO NOTHING` is the only dedup
    -- guarantee the sync relies on; `source_path` / `source_line`
    -- are forensic context. Nullable so pre-`content_hash` rows
    -- migrated from an older schema coexist; Postgres treats NULL
    -- values as distinct in a unique index so multiple legacy rows
    -- with NULL hashes do not conflict.
    content_hash        TEXT
);

CREATE INDEX IF NOT EXISTS analytics_events_ts_idx
    ON analytics_events (ts);

CREATE INDEX IF NOT EXISTS analytics_events_event_ts_idx
    ON analytics_events (event, ts);

CREATE INDEX IF NOT EXISTS analytics_events_repo_issue_idx
    ON analytics_events (repo, issue);

CREATE INDEX IF NOT EXISTS analytics_events_stage_idx
    ON analytics_events (stage)
    WHERE stage IS NOT NULL;

-- Per-event-kind partial indexes for the two hot dashboard queries:
-- `agent_exit` powers the cost / token aggregates and the recent-runs
-- table, `stage_enter` powers the stage transition counts. Both
-- queries always carry an `event = '...'` filter, so a partial index
-- keyed on `(repo, ts DESC)` is roughly an order of magnitude smaller
-- than a full-table index on the same columns and lets Postgres skip
-- the event filter at scan time. `WHERE event = '...'` predicates
-- are stable string literals so the planner can match them against
-- the partial index without coercion.
CREATE INDEX IF NOT EXISTS analytics_events_agent_exit_idx
    ON analytics_events (repo, ts DESC)
    WHERE event = 'agent_exit';

CREATE INDEX IF NOT EXISTS analytics_events_stage_enter_idx
    ON analytics_events (repo, ts DESC)
    WHERE event = 'stage_enter';

-- Composite index for the multi-filter dashboard widgets that narrow
-- by event + repo + stage and order by ts (per-stage breakdowns, the
-- per-issue drill-down, the time-series). `(event, repo, stage, ts)`
-- is the column order the dashboard queries actually filter in: an
-- equality on `event` first, then `repo`, then `stage`, then a range
-- / sort on `ts`. The partial `stage_idx` above stays useful for
-- "stage IS NOT NULL" probes that don't carry an `event` predicate.
CREATE INDEX IF NOT EXISTS analytics_events_event_repo_stage_ts_idx
    ON analytics_events (event, repo, stage, ts);

-- Idempotent column / index additions so an operator who applies this
-- file via `psql -f` against an instance created before the column
-- existed picks up the new dedup key without dropping the data volume.
ALTER TABLE analytics_events
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

-- Plain (non-partial) unique index so `INSERT ... ON CONFLICT
-- (content_hash) DO NOTHING` can infer this index as the arbiter --
-- Postgres requires the partial predicate to be repeated in the
-- conflict target otherwise, which would force the sync to carry a
-- WHERE clause it has no business knowing about. Migration safety
-- is unaffected: Postgres treats NULL values as distinct in a unique
-- index, so multiple pre-`content_hash` rows with NULL hashes
-- coexist under this same index without conflicts.
CREATE UNIQUE INDEX IF NOT EXISTS analytics_events_content_hash_idx
    ON analytics_events (content_hash);

-- Backend view over `event = 'agent_exit'` rows that exposes the
-- analytics shape the dashboard / read model want without re-coding
-- the same derivations everywhere. `CREATE OR REPLACE` keeps this
-- idempotent against a re-run of the init script and lets an operator
-- apply schema updates via `psql -f` without dropping the view first.
--
-- The view promotes `models->>0` (Postgres extracts the first array
-- element of the typed JSONB column as text; NULL when the array is
-- empty or missing) and falls back to a single canonical model label
-- via COALESCE so downstream group-bys never blow up on NULL keys.
--
-- Derived fields:
--   * `total_tokens`       = input + output (the canonical "billed"
--                            total most dashboards want to plot)
--   * `total_cache_tokens` = cached + cache_read + cache_write
--   * `review_round_bucket` collapses the long tail of high review
--     rounds into a single bucket so per-bucket counts stay readable
--     in dashboards regardless of an outlier round-12 issue
--   * `failed`             = exit_code is non-zero (NULL exit_code
--                            stays NULL so we don't conflate "no
--                            data" with "succeeded")
--   * `has_cost`           = cost_usd IS NOT NULL (i.e. `cost_source`
--                            in {`reported`, `estimated`}; both
--                            `no-usage` and `unknown-price` leave
--                            cost NULL so the dashboard can split
--                            coverage-known runs from coverage-gap
--                            runs without a string comparison)
--
-- The view deliberately exposes raw nullable columns alongside the
-- derived ones so callers that want the unprocessed value still have
-- it. `cost_source` passes through verbatim -- a dashboard can group
-- by it to surface coverage gaps (the `unknown-price` cohort is the
-- pricing-table maintenance signal).
CREATE OR REPLACE VIEW analytics_agent_runs AS
SELECT
    id,
    ts,
    repo,
    issue,
    stage,
    agent_role,
    backend,
    agent_spec,
    resume_session_id,
    session_id,
    review_round,
    CASE
        WHEN review_round IS NULL THEN NULL
        WHEN review_round <= 0 THEN '0'
        WHEN review_round = 1 THEN '1'
        WHEN review_round = 2 THEN '2'
        WHEN review_round BETWEEN 3 AND 5 THEN '3-5'
        ELSE '6+'
    END AS review_round_bucket,
    retry_count,
    duration_s,
    exit_code,
    timed_out,
    CASE
        WHEN exit_code IS NULL THEN NULL
        ELSE exit_code <> 0
    END AS failed,
    input_tokens,
    output_tokens,
    cached_tokens,
    cache_read_tokens,
    cache_write_tokens,
    COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
        AS total_tokens,
    COALESCE(cached_tokens, 0)
        + COALESCE(cache_read_tokens, 0)
        + COALESCE(cache_write_tokens, 0)
        AS total_cache_tokens,
    models,
    COALESCE(models->>0, 'unknown') AS model,
    turns,
    cost_usd,
    (cost_usd IS NOT NULL) AS has_cost,
    cost_source
FROM analytics_events
WHERE event = 'agent_exit';

-- Daily rollup of `analytics_events` keyed on
-- (day, repo, issue, event, stage, backend, cost_source). Dashboard
-- widgets that need only date-bucketed totals (KPI strip, time
-- series, cost-by-stage / cost-by-repo, backend efficiency,
-- throughput, reliability tiles) can read this view instead of the
-- raw events table so per-window aggregates stay bounded as the
-- events table grows past ~1M rows. The widgets that genuinely need
-- per-row resolution (hourly heatmap, recent agent exits, top-cost
-- issue drill-down, review-round breakdown) keep hitting the raw
-- table.
--
-- `day` is the UTC date of the event -- `ts` is TIMESTAMPTZ, so the
-- cast to `date AT TIME ZONE 'UTC'` normalises to UTC regardless of
-- the row's source timezone. `issue` is in the key because every
-- dashboard read accepts an `issue_filter`; without it an
-- issue-scoped read would double-count. `cost_source` is in the key
-- so the cost-coverage panel can read from the rollup without
-- decomposing `unknown-price` / `no-usage` / `reported` /
-- `estimated` cohorts after the fact.
--
-- Aggregates carry every column a downstream consumer needs to
-- recover row-weighted means without averaging averages: token sums
-- per band, `duration_s_sum` + `duration_s_count` so `AVG` is
-- `sum / count`, `failed_count` for the reliability "Failures" tile,
-- `timed_out_count` for the reliability "Timeouts" tile, and
-- `total_cost_usd` for every spend tile. `event_count` is the raw
-- row count per group.
--
-- `IF NOT EXISTS` makes the create idempotent for the
-- operator-driven `psql -f` reapply path. Postgres CREATE
-- MATERIALIZED VIEW does not support OR REPLACE, so changing the
-- column list / aggregates requires
-- `DROP MATERIALIZED VIEW analytics_daily_rollup` followed by a
-- reapply; the sync's refresh hook does NOT recover from a column
-- mismatch.
--
-- Postgres populates the view at create time: the
-- `CREATE MATERIALIZED VIEW ... AS SELECT` form omits `WITH NO
-- DATA`, so the create runs the select and seeds the view in the
-- same statement. On a fresh deploy the events table is empty so
-- the seed scan is free; on a `psql -f` migration against an
-- already-populated instance the create scans the events table
-- once to build the initial snapshot. The `IF NOT EXISTS` guard
-- keeps subsequent reapplies a no-op, so that initial scan is the
-- only migration cost the operator pays for this view. The
-- operator-driven sync (see `orchestrator/analytics/sync.py`)
-- issues `REFRESH MATERIALIZED VIEW analytics_daily_rollup` after
-- every successful commit -- rerunning the sync is therefore the
-- documented recovery path for a stale rollup (e.g. when a
-- previous sync's refresh failed mid-rebuild).
CREATE MATERIALIZED VIEW IF NOT EXISTS analytics_daily_rollup AS
SELECT
    (ts AT TIME ZONE 'UTC')::date AS day,
    repo,
    issue,
    event,
    stage,
    backend,
    cost_source,
    SUM(input_tokens)        AS total_input_tokens,
    SUM(output_tokens)       AS total_output_tokens,
    SUM(cached_tokens)       AS total_cached_tokens,
    SUM(cache_read_tokens)   AS total_cache_read_tokens,
    SUM(cache_write_tokens)  AS total_cache_write_tokens,
    SUM(cost_usd)            AS total_cost_usd,
    SUM(duration_s)          AS duration_s_sum,
    SUM(CASE WHEN duration_s IS NOT NULL THEN 1 ELSE 0 END)
        AS duration_s_count,
    SUM(CASE WHEN exit_code IS NOT NULL AND exit_code <> 0 THEN 1 ELSE 0 END)
        AS failed_count,
    SUM(CASE WHEN event = 'agent_exit' AND timed_out = TRUE THEN 1 ELSE 0 END)
        AS timed_out_count,
    COUNT(*) AS event_count
FROM analytics_events
GROUP BY
    (ts AT TIME ZONE 'UTC')::date,
    repo, issue, event, stage, backend, cost_source;

-- Unique index over the GROUP BY columns. `NULLS NOT DISTINCT`
-- (Postgres 15+; the analytics service runs on `postgres:16` per
-- `analytics-db/compose.yml`) collapses NULL stage / backend /
-- cost_source values into one row -- the same way `GROUP BY`
-- already does -- so the index is genuinely unique across the
-- view's contents. Having a unique index is the prerequisite for
-- `REFRESH MATERIALIZED VIEW CONCURRENTLY`; the current sync uses
-- the non-concurrent variant for simplicity, so the index is
-- forward-compat plumbing today and load-bearing the moment an
-- operator wants to refresh without locking the view.
CREATE UNIQUE INDEX IF NOT EXISTS analytics_daily_rollup_key_idx
    ON analytics_daily_rollup
        (day, repo, issue, event, stage, backend, cost_source)
    NULLS NOT DISTINCT;

-- Day-range scan support for the dashboard's window-bounded reads
-- (KPI strip, time series, throughput). A `WHERE day BETWEEN x AND
-- y` predicate hits this index as a range scan, which keeps the
-- rollup-backed widgets in the few-millisecond range even when the
-- view itself grows to hundreds of thousands of rows.
CREATE INDEX IF NOT EXISTS analytics_daily_rollup_day_repo_idx
    ON analytics_daily_rollup (day, repo);
