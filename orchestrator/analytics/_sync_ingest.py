# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Batched analytics JSONL ingest and progress accounting."""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.analytics._sync_models import _IngestContext, _SyncCounters
from orchestrator.analytics._sync_rows import (
    _RowProvenance,
    _prepare_record,
    _row_values,
)

_BATCH_SIZE = 500
log = logging.getLogger("orchestrator.analytics.sync")


def _emit_progress(counters: _SyncCounters, start: float) -> None:
    """Log one progress record: cumulative counts + elapsed wall-clock.

    Fired after every batched `executemany` flush so an operator can watch a
    multi-thousand-record replay advance.
    """
    log.info(
        "analytics_sync: progress lines=%d inserted=%d duplicate=%d malformed=%d elapsed=%.3fs",
        counters.total_lines,
        counters.inserted,
        counters.skipped_duplicate,
        counters.skipped_malformed,
        time.monotonic() - start,
    )


def _flush_batch(
    cur: Any,
    insert_sql: str,
    batch: list[tuple],
    counters: _SyncCounters,
    start: float,
) -> None:
    """Flush the accumulated row batch in one `executemany`, then clear it.

    A single `executemany` per batch collapses N protocol round-trips into
    one pipeline; `ON CONFLICT (content_hash) DO NOTHING` in the INSERT stays
    the server-side dedup backstop. psycopg's rowcount on `executemany` is the
    total rows inserted across the batch, so the duplicate count is
    `len(batch) - rowcount`. A driver that reports -1 falls back to counting
    the whole batch as inserted -- the database is the authority and
    `inserted` stays a lower bound only if a driver bug strips the count
    entirely. A no-op on an empty batch, so the caller can invoke it
    unconditionally at EOF.
    """
    if not batch:
        return
    cur.executemany(insert_sql, batch)
    rowcount = getattr(cur, "rowcount", len(batch))
    if rowcount < 0:
        rowcount = len(batch)
    counters.inserted += rowcount
    counters.skipped_duplicate += len(batch) - rowcount
    batch.clear()
    _emit_progress(counters, start)


def _note_malformed_line(
    counters: _SyncCounters,
    line_number: int,
    log_path: Path,
    reason: str,
) -> None:
    """Count and log one skipped malformed line without aborting the sync.

    `reason` names why the line was rejected (`not JSON`, `JSON not an
    object`, `missing/invalid required keys`). The line number is logged so
    the operator can clean it up out-of-band; the sync never rewrites the
    JSONL file.
    """
    counters.skipped_malformed += 1
    counters.malformed_lines.append(line_number)
    log.warning(
        "analytics_sync: skipping line %d (%s) in %s",
        line_number,
        reason,
        log_path,
    )


def _existing_hashes(cur: Any) -> set[str]:
    cur.execute("SELECT content_hash FROM analytics_events WHERE content_hash IS NOT NULL")
    return {row[0] for row in cur if row[0] is not None}


@dataclass
class _RecordIngester:
    """Classify, deduplicate, and batch records for one open cursor."""

    cur: Any
    context: _IngestContext
    existing_hashes: set[str]
    batch_size: int
    batch: list[tuple] = field(default_factory=list)

    def add(self, line_number: int, raw_line: str) -> None:
        self.context.counters.total_lines += 1
        prepared, reason = _prepare_record(raw_line)
        if prepared is None:
            if reason is not None:
                _note_malformed_line(
                    self.context.counters,
                    line_number,
                    self.context.log_path,
                    reason,
                )
            return
        if prepared.content_hash in self.existing_hashes:
            self.context.counters.skipped_duplicate += 1
            return
        provenance = _RowProvenance(
            source_path=self.context.source_path,
            source_line=line_number,
            content_hash=prepared.content_hash,
        )
        self.batch.append(
            _row_values(
                prepared.columns,
                prepared.extras,
                provenance,
                self.context.json_adapter,
            )
        )
        self.existing_hashes.add(prepared.content_hash)
        if len(self.batch) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        _flush_batch(
            self.cur,
            self.context.insert_sql,
            self.batch,
            self.context.counters,
            self.context.start,
        )


def _stream_records(ingester: _RecordIngester) -> None:
    with ingester.context.log_path.open("r", encoding="utf-8") as source_file:
        for line_number, raw_line in enumerate(source_file, start=1):
            ingester.add(line_number, raw_line)


def _ingest_records(
    conn: Any,
    context: _IngestContext,
) -> None:
    """Stream `log_path` into `conn` under one cursor, batching valid rows.

    A startup pre-check pulls every persisted `content_hash` into a Python
    set so already-present records are skipped before they ever reach the
    wire: one server-side scan over the unique
    `analytics_events_content_hash_idx` replaces what would otherwise be one
    per-row round-trip per duplicate. The `WHERE content_hash IS NOT NULL`
    predicate filters legacy pre-`content_hash` rows so they do not pollute
    the set; `ON CONFLICT (content_hash) DO NOTHING` in `_flush_batch` stays
    the authoritative dedup backstop for a racing concurrent writer.

    Each input line is then classified: blank lines are skipped silently,
    malformed lines are counted and logged, a hash already known (from the
    pre-check or earlier in this same file) is counted as a duplicate without
    a round-trip, and a genuinely-new row is appended to a `_BATCH_SIZE`
    buffer flushed through `_flush_batch`. The trailing partial batch is
    flushed at EOF. All tallies land on `counters`; the caller commits and
    closes the connection.
    """
    with conn.cursor() as cur:
        ingester = _RecordIngester(
            cur=cur,
            context=context,
            existing_hashes=_existing_hashes(cur),
            batch_size=sys.modules["orchestrator.analytics.sync"]._BATCH_SIZE,
        )
        _stream_records(ingester)
        ingester.flush()
