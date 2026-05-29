-- Copyright 2026 Geser Dugarov
-- SPDX-License-Identifier: Apache-2.0
--
-- Analytics database schema for the orchestrator.
--
-- This file mirrors the JSONL record shape produced by
-- `orchestrator/analytics.py` (`build_record`) so a future ingestion job
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
    -- rewrites, so repeated `analytics_sync` runs that re-read a
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
