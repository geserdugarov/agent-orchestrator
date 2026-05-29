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

    -- Source line for audit / dedup. The ingest job should populate
    -- this from the JSONL byte offset or the source filename so
    -- replaying the same log twice can be detected.
    source_path         TEXT,
    source_line         BIGINT
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
