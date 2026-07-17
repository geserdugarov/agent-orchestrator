# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""JSONL -> Postgres replay for the analytics sink.

`orchestrator/analytics/` writes one JSON object per line to
`analytics.ANALYTICS_LOG_PATH`. This module reads that file and inserts
each record into the `analytics_events` table defined by
`analytics-db/init/01-schema.sql`, deduplicating by the SHA-256 of the
canonical (`sort_keys=True`) JSON form of each record so repeated runs
are idempotent.

Why a content hash rather than `(source_path, source_line)`: line
numbers shift whenever `analytics.prune_old_records` rewrites the
file, so a `(path, line)` key would let the same record be inserted
twice from different cursor positions after a prune. The hash is
stable across prune-induced renumbering as long as the JSON encoding
stays canonical, which `analytics.append_record` already guarantees.

Tolerance for malformed lines matches `prune_old_records`: blank
lines are skipped, lines that are not valid JSON or do not parse to a
dict are counted as skipped and logged, and a record missing one of
the required (`ts` / `repo` / `issue` / `event`) keys is treated the
same way. Tolerance is the point -- this sink is local-filesystem
observability and the JSONL on disk may carry partial flushes from a
crashed write or hand-edits by an operator.

Connection settings come from `analytics.ANALYTICS_DB_URL`, a single
libpq URL. There is no hardcoded localhost fallback; the sync is a
no-op when the URL is unset so operators who have not deployed the
Postgres service can run the CLI without configuring it. To move the
database off-host, repoint the URL -- no code change required.

The sync is operator-driven: not wired into the polling loop. Run
`python -m orchestrator.analytics.sync` (or import
`sync_jsonl_to_postgres` directly) on whatever cadence the operator
prefers. Wiring it into the tick is out of scope for this child --
the polling loop's correctness must not depend on database
availability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from orchestrator import analytics as _analytics

log = logging.getLogger(__name__)

# Cadence of progress logs during a sync. Picked so a small replay
# emits one or two updates and a multi-thousand-record replay still
# shows steady forward motion without flooding the log.
_PROGRESS_INTERVAL = 500

# Number of validated row tuples accumulated before a `cur.executemany`
# flush. Sized to match `_PROGRESS_INTERVAL` so each flush also drops
# one progress line and a multi-thousand-record replay pays one
# Postgres round-trip per batch instead of one per row. Lives next to
# `_PROGRESS_INTERVAL` as an implementation-only knob; tuning it
# requires no CLI flag, env var, or schema change.
_BATCH_SIZE = 500

# Columns the table promotes from the JSONL record; anything else lands
# in `extras` JSONB so a JSONL record from a newer orchestrator version
# never loses fields. Kept here (not in `orchestrator/analytics/`) because
# it is a database-shape concern, not a record-build concern.
# Required JSONL/DB record field names, shared by the promoted-column list, the
# required-key guard, and the per-record extraction.
_COL_TS = "ts"
_COL_REPO = "repo"
_COL_ISSUE = "issue"
_COL_EVENT = "event"


_PROMOTED_COLUMNS = (
    _COL_TS,
    _COL_REPO,
    _COL_ISSUE,
    _COL_EVENT,
    "stage",
    "duration_s",
    "result",
    "agent_role",
    "backend",
    "agent_spec",
    "resume_session_id",
    "session_id",
    "review_round",
    "retry_count",
    "exit_code",
    "timed_out",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "models",
    "turns",
    "cost_usd",
    "cost_source",
)

# JSONB columns; psycopg adapts dict / list to JSON natively but a few
# drivers need an explicit Json wrapper -- callers can pass their own
# `json_adapter` to the sync if needed.
_JSONB_COLUMNS = ("models", "extras")

_REQUIRED_KEYS = (_COL_TS, _COL_REPO, _COL_ISSUE, _COL_EVENT)

# Name of the daily-rollup materialized view defined in
# `analytics-db/init/01-schema.sql`. Kept here as a constant so the
# refresh hook and the schema test agree on the spelling; a rename in
# the SQL must land in lock-step with a rename here.
_DAILY_ROLLUP_VIEW = "analytics_daily_rollup"


@dataclass(frozen=True)
class SyncResult:
    """Counts returned by `sync_jsonl_to_postgres`.

    - `inserted` -- records that hit the database as a new row.
    - `skipped_duplicate` -- records whose `content_hash` already
      existed; the `ON CONFLICT DO NOTHING` path absorbed them.
    - `skipped_malformed` -- lines that were blank, unparseable JSON,
      not a JSON object, or missing one of `ts` / `repo` / `issue` /
      `event`. The line number is logged as a warning so the operator
      can clean them up out-of-band; the sync never deletes or rewrites
      the JSONL file itself.
    - `total_lines` -- raw line count consumed from the file
      (including blanks), so the caller can sanity-check progress.
    - `duration_s` -- wall-clock seconds from connect entry through
      commit / close, rounded to 3 decimals. Lets the CLI surface a
      human-readable elapsed time without re-timing externally; the
      no-op paths (URL unset / file absent) return 0.0.
    """

    inserted: int = 0
    skipped_duplicate: int = 0
    skipped_malformed: int = 0
    total_lines: int = 0
    malformed_line_numbers: tuple[int, ...] = field(default_factory=tuple)
    duration_s: float = 0.0


@dataclass
class _SyncCounters:
    """Mutable tallies threaded through the ingest loop.

    `_ingest_records`, `_flush_batch`, and `_note_malformed_line` update
    these in place instead of closing over `nonlocal` locals, so each helper
    lives at module scope and can be unit-tested on its own. The final counts
    are folded into the frozen `SyncResult` once the sync returns.
    """

    inserted: int = 0
    skipped_duplicate: int = 0
    skipped_malformed: int = 0
    total_lines: int = 0
    malformed_lines: list[int] = field(default_factory=list)


def _canonical_json(record: dict) -> str:
    """Stable JSON form used for the content hash.

    Must match `analytics.append_record`'s on-disk encoding
    (`sort_keys=True`, default separators) so a record round-trips
    through file -> parse -> hash without drift.
    """
    return json.dumps(record, sort_keys=True)


def _content_hash(record: dict) -> str:
    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Parse the `ts` field into a timezone-aware datetime.

    Naive timestamps are interpreted as UTC -- mirrors
    `analytics.prune_old_records`'s behavior so a record written
    without `+00:00` (older writer, hand-edit) survives the round
    trip. Returns None when the input is missing or unparseable; the
    caller treats that as a malformed-line skip.
    """
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _required_text(raw: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw:
        return None
    return raw


def _issue_number(raw: Any) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _required_columns(record: dict) -> Optional[dict[str, Any]]:
    if any(key not in record for key in _REQUIRED_KEYS):
        return None
    timestamp = _parse_ts(record.get(_COL_TS))
    repo = _required_text(record.get(_COL_REPO))
    issue = _issue_number(record.get(_COL_ISSUE))
    event = _required_text(record.get(_COL_EVENT))
    if timestamp is None or repo is None or issue is None or event is None:
        return None
    return {
        "ts": timestamp,
        "repo": repo,
        "issue": issue,
        "event": event,
    }


def _extra_columns(record: dict, columns: dict[str, Any]) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    for key, field_value in record.items():
        if key in _REQUIRED_KEYS:
            continue
        if key in _PROMOTED_COLUMNS:
            columns[key] = field_value
        else:
            extras[key] = field_value
    return extras


def _split_row(record: dict) -> Optional[tuple[dict, dict]]:
    """Promote known columns and route the rest to `extras`.

    Returns (columns, extras), or None if a required key is missing
    or `ts` does not parse. The caller treats None as a malformed-line
    skip so a record with garbled `ts` does not abort the entire sync.
    """
    columns = _required_columns(record)
    if columns is None:
        return None
    return columns, _extra_columns(record, columns)


def _build_insert_sql() -> str:
    """Construct the parameterised INSERT once per call.

    All promoted columns are emitted in a fixed order so the
    parameter tuple in `_insert_row` lines up positionally without a
    per-row dict-to-tuple mapping.
    """
    columns = (
        *_PROMOTED_COLUMNS,
        "extras",
        "source_path",
        "source_line",
        "content_hash",
    )
    placeholders = ", ".join(["%s"] * len(columns))
    column_list = ", ".join(columns)
    return (
        f"INSERT INTO analytics_events ({column_list}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (content_hash) DO NOTHING"
    )


@dataclass(frozen=True)
class _RowProvenance:
    """Source identity and stable dedup hash for one prepared row."""

    source_path: Optional[str]
    source_line: int
    content_hash: str


def _row_values(
    columns: dict,
    extras: dict,
    provenance: _RowProvenance,
    json_adapter: Callable[[Any], Any],
) -> tuple:
    cells: list[Any] = []
    for col in _PROMOTED_COLUMNS:
        cell = columns.get(col)
        if col in _JSONB_COLUMNS and cell is not None:
            cell = json_adapter(cell)
        cells.append(cell)
    cells.append(json_adapter(extras) if extras else None)
    cells.append(provenance.source_path)
    cells.append(provenance.source_line)
    cells.append(provenance.content_hash)
    return tuple(cells)


# libpq accepts credentials in the URL query string as well as the
# netloc -- `postgresql://h/db?user=u&password=secret` is valid and
# carries the password in the query. Redacting only the netloc would
# leak the password into the connection / progress logs whenever an
# operator uses the query-string form. Parameter names are
# case-insensitive per the libpq docs, so the membership check below
# lowercases the key before comparing.
_REDACTED_QUERY_PARAMS = frozenset(
    {"user", "password", "passfile", "sslpassword"}
)


def _redacted_netloc(parts: Any) -> str:
    if not parts.username and not parts.password:
        return parts.netloc
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return f"***@{netloc}" if netloc else "***"


def _redacted_query(query: str) -> str:
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    redacted_pairs = [
        (key, "***" if key.lower() in _REDACTED_QUERY_PARAMS else param_value)
        for key, param_value in pairs
    ]
    if redacted_pairs == pairs:
        return query
    return urlencode(redacted_pairs, safe="*")


def _redact_db_url(url: str) -> str:
    """Strip credentials from a libpq URL before it lands in a log line.

    `ANALYTICS_DB_URL` is a libpq URL that may carry credentials in
    two distinct places: the `user:password@` netloc prefix and the
    `?user=&password=&sslpassword=&passfile=` query string. This CLI
    surfaces connection logs to operators and occasionally to shared
    dashboards, so both forms collapse to `***` before printing -- a
    remote-Postgres password never lands in stdout or in any log
    aggregator the host forwards to.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<db-url-unparseable>"
    return urlunsplit(
        (
            parts.scheme,
            _redacted_netloc(parts),
            parts.path,
            _redacted_query(parts.query),
            parts.fragment,
        )
    )


def _default_connect(db_url: str) -> Any:
    """Lazy psycopg import so the module loads without the driver.

    `pyproject.toml` pins `psycopg[binary]`, but a sync that never
    runs (operator hasn't deployed Postgres) must not surface an
    ImportError -- the orchestrator's polling tick imports this module
    transitively via `config`. Defer the import to call time so the
    module-load path stays driver-free.
    """
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError(
            "psycopg is required for analytics_sync; "
            "run `uv sync --locked` to install it"
        ) from error
    return psycopg.connect(db_url)


def _default_json_adapter(payload: Any) -> Any:
    """Adapt dict / list to the psycopg JSON wrapper when available.

    Falls back to passing the raw Python object through; psycopg v3's
    default adaptation already handles dict / list as JSONB inserts
    so the wrapper is optional. The factory pattern lets tests inject
    `lambda v: v` and inspect raw structures.
    """
    try:
        from psycopg.types.json import Json
    except ImportError:
        return payload
    return Json(payload)


def _rollback_quietly(conn: Any, message: str) -> None:
    """Roll `conn` back, downgrading a rollback failure to a logged no-op.

    A rollback that itself raises leaves cleanup to the driver; there is
    nothing more the sync can do, so `message` is logged and the failure is
    swallowed rather than masking the original error that triggered the
    rollback.
    """
    try:
        conn.rollback()
    except Exception:
        log.exception(message)


def _close_quietly(conn: Any) -> None:
    """Close `conn`, downgrading a close failure to a logged no-op.

    The committed rows are already durable, so a driver whose `close` raises
    must not turn a successful sync into a failure.
    """
    try:
        conn.close()
    except Exception:
        log.exception("analytics_sync: connection close failed")


def _refresh_daily_rollup(conn: Any) -> None:
    """Refresh the daily rollup materialized view after a successful sync.

    Issues a non-concurrent `REFRESH MATERIALIZED VIEW` over the view
    defined in `analytics-db/init/01-schema.sql`. Non-concurrent is
    the safe default because it does not require the view to be
    populated and does not lock the events table; it does take an
    `ACCESS EXCLUSIVE` lock on the view itself for the duration of
    the rebuild, which is fine for the operator-driven sync (the
    dashboard re-reads on a 60-second cache and tolerates a brief
    blocked read).

    Exceptions are logged and swallowed so a refresh failure -- the
    view not existing yet on a pre-migration deployment, a transient
    Postgres error, a lock-wait timeout -- never aborts the sync.
    The committed inserts are already durable in `analytics_events`,
    so the caller's success contract is unaffected; the operator's
    log makes the refresh failure visible and the next sync's
    refresh recovers the rollup once the underlying issue is fixed.
    """
    sql = f"REFRESH MATERIALIZED VIEW {_DAILY_ROLLUP_VIEW}"
    refresh_start = time.monotonic()
    try:
        log.info(
            "analytics_sync: refreshing materialized view %s",
            _DAILY_ROLLUP_VIEW,
        )
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        log.info(
            "analytics_sync: refreshed %s in %.3fs",
            _DAILY_ROLLUP_VIEW, time.monotonic() - refresh_start,
        )
    except Exception:
        log.exception(
            "analytics_sync: refresh of %s failed; sync still committed",
            _DAILY_ROLLUP_VIEW,
        )
        _rollback_quietly(
            conn, "analytics_sync: rollback after refresh failure failed"
        )


def _emit_progress(counters: _SyncCounters, start: float) -> None:
    """Log one progress record: cumulative counts + elapsed wall-clock.

    Fired after every batched `executemany` flush so an operator can watch a
    multi-thousand-record replay advance.
    """
    log.info(
        "analytics_sync: progress lines=%d inserted=%d duplicate=%d "
        "malformed=%d elapsed=%.3fs",
        counters.total_lines, counters.inserted, counters.skipped_duplicate,
        counters.skipped_malformed, time.monotonic() - start,
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
    counters: _SyncCounters, line_number: int, log_path: Path, reason: str,
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
        line_number, reason, log_path,
    )


@dataclass(frozen=True)
class _PreparedRecord:
    """Validated promoted fields, extras, and hash for one JSONL record."""

    columns: dict[str, Any]
    extras: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class _IngestContext:
    """Stable inputs and mutable counters shared by one ingest pass."""

    log_path: Path
    insert_sql: str
    source_path: Optional[str]
    json_adapter: Callable[[Any], Any]
    counters: _SyncCounters
    start: float


def _prepare_record(
    raw_line: str,
) -> tuple[Optional[_PreparedRecord], Optional[str]]:
    stripped = raw_line.strip()
    if not stripped:
        return None, None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return None, "not JSON"
    if not isinstance(record, dict):
        return None, "JSON not an object"
    split = _split_row(record)
    if split is None:
        return None, "missing/invalid required keys"
    columns, extras = split
    return _PreparedRecord(columns, extras, _content_hash(record)), None


def _existing_hashes(cur: Any) -> set[str]:
    cur.execute(
        "SELECT content_hash FROM analytics_events "
        "WHERE content_hash IS NOT NULL"
    )
    return {row[0] for row in cur if row[0] is not None}


@dataclass
class _RecordIngester:
    """Classify, deduplicate, and batch records for one open cursor."""

    cur: Any
    context: _IngestContext
    existing_hashes: set[str]
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
        if len(self.batch) >= _BATCH_SIZE:
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
        )
        _stream_records(ingester)
        ingester.flush()


@dataclass(frozen=True)
class _SyncRequest:
    """Resolved source, destination, and injected adapters for one sync."""

    log_path: Optional[Path]
    db_url: Optional[str]
    connect_fn: Callable[[str], Any]
    json_adapter: Callable[[Any], Any]

    @classmethod
    def resolve(
        cls,
        log_path: Optional[Path],
        db_url: Optional[str],
        connect: Optional[Callable[[str], Any]],
        json_adapter: Optional[Callable[[Any], Any]],
    ) -> _SyncRequest:
        return cls(
            log_path=(
                log_path
                if log_path is not None
                else _analytics.ANALYTICS_LOG_PATH
            ),
            db_url=db_url if db_url is not None else _analytics.ANALYTICS_DB_URL,
            connect_fn=connect or _default_connect,
            json_adapter=json_adapter or _default_json_adapter,
        )

    def ready(self) -> bool:
        """Log and reject configured no-op paths before any connection work."""
        if self.log_path is None:
            log.info(
                "analytics_sync: ANALYTICS_LOG_PATH not configured; "
                "nothing to sync"
            )
            return False
        if not self.db_url:
            log.info(
                "analytics_sync: ANALYTICS_DB_URL not configured; "
                "nothing to sync"
            )
            return False
        if not self.log_path.exists():
            log.info(
                "analytics_sync: %s does not exist yet; nothing to sync",
                self.log_path,
            )
            return False
        return True


@dataclass
class _SyncRun:
    """Connection lifecycle, ingest state, and reporting for one replay."""

    request: _SyncRequest
    counters: _SyncCounters = field(default_factory=_SyncCounters)
    start: float = field(default_factory=time.monotonic)

    def connect(self) -> Any:
        redacted_url = _redact_db_url(self.request.db_url or "")
        log.info(
            "analytics_sync: connecting to %s (source=%s)",
            redacted_url,
            self.request.log_path,
        )
        conn = self.request.connect_fn(self.request.db_url)
        log.info(
            "analytics_sync: connection established to %s after %.3fs",
            redacted_url,
            time.monotonic() - self.start,
        )
        return conn

    def ingest_context(self) -> _IngestContext:
        log_path = Path(self.request.log_path)
        return _IngestContext(
            log_path=log_path,
            insert_sql=_build_insert_sql(),
            source_path=str(log_path),
            json_adapter=self.request.json_adapter,
            counters=self.counters,
            start=self.start,
        )

    def commit(self, conn: Any) -> None:
        """Commit rows and refresh the rollup even for duplicate-only runs."""
        log.info(
            "analytics_sync: committing transaction (lines=%d inserted=%d "
            "duplicate=%d malformed=%d elapsed=%.3fs)",
            self.counters.total_lines,
            self.counters.inserted,
            self.counters.skipped_duplicate,
            self.counters.skipped_malformed,
            time.monotonic() - self.start,
        )
        conn.commit()
        _refresh_daily_rollup(conn)

    def finalize(self) -> SyncResult:
        duration_s = round(time.monotonic() - self.start, 3)
        log.info(
            "analytics_sync: completed in %.3fs (inserted=%d duplicate=%d "
            "malformed=%d total_lines=%d source=%s)",
            duration_s,
            self.counters.inserted,
            self.counters.skipped_duplicate,
            self.counters.skipped_malformed,
            self.counters.total_lines,
            self.request.log_path,
        )
        return SyncResult(
            inserted=self.counters.inserted,
            skipped_duplicate=self.counters.skipped_duplicate,
            skipped_malformed=self.counters.skipped_malformed,
            total_lines=self.counters.total_lines,
            malformed_line_numbers=tuple(self.counters.malformed_lines),
            duration_s=duration_s,
        )

    def execute(self) -> SyncResult:
        conn = self.connect()
        try:
            _ingest_records(conn, self.ingest_context())
            self.commit(conn)
        except Exception:
            _rollback_quietly(conn, "analytics_sync: rollback failed")
            raise
        finally:
            _close_quietly(conn)
        return self.finalize()


def sync_jsonl_to_postgres(
    *,
    log_path: Optional[Path] = None,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    json_adapter: Optional[Callable[[Any], Any]] = None,
) -> SyncResult:
    """Replay every record in `log_path` into Postgres at `db_url`.

    Defaults come from `analytics.ANALYTICS_LOG_PATH` and
    `analytics.ANALYTICS_DB_URL`; either being None or the JSONL file
    being absent yields an empty SyncResult (the no-op path so the
    CLI is safe to schedule before the operator deploys Postgres).

    Malformed lines are logged and counted but never abort the
    sync; the JSONL file is treated as read-only -- this sync never
    rewrites or truncates it, even when it sees malformed lines.

    Progress is reported through the module logger: a "connecting" /
    "connection established" pair brackets the connect call, a
    "progress" record is emitted every `_PROGRESS_INTERVAL` lines
    consumed so an operator can watch a multi-thousand-record replay
    advance, and a "completed in %.3fs" summary fires after commit.
    Connection-string credentials are stripped before logging so a
    `user:password@host` URL does not leak into the operator's stdout.

    `connect(db_url) -> connection` and `json_adapter(value) -> value`
    are factory hooks so tests can inject a fake without depending on
    psycopg. Production callers leave both at None to get the real
    psycopg connection and the default `Json` wrapper.
    """
    request = _SyncRequest.resolve(
        log_path,
        db_url,
        connect,
        json_adapter,
    )
    if not request.ready():
        return SyncResult()
    return _SyncRun(request).execute()


def _configure_cli_logging(level: str) -> None:
    """Install a UTC-stamped log formatter on the root logger.

    The CLI also prints a UTC-stamped one-line summary to stdout at
    the end of `main`; pinning the log timestamps to UTC -- with an
    explicit "UTC" suffix in datefmt -- means a mixed stdout / stderr
    stream stays a coherent time-ordered sequence regardless of the
    host's local timezone. Without this, a TZ-skewed host (the
    reviewer hit a TZ+7 machine) prints log lines and the summary
    line hours apart for the same wall-clock event because
    `logging.basicConfig` defaults to local time while
    `datetime.now(timezone.utc)` is UTC.
    """
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S UTC",
    )
    # `gmtime` on this formatter instance only -- mutating
    # `logging.Formatter.converter` globally would change every other
    # formatter's timezone behavior in the same process (the unit
    # tests pull `assertLogs` records through their own formatters,
    # and a process-wide flip would surprise them).
    formatter.converter = time.gmtime

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Replace prior handlers so a re-invocation in the same process
    # (a test that calls `main()` twice, a long-lived shell running
    # `python -m`) actually picks up the new formatter rather than
    # silently no-op'ing the way `basicConfig` does once the root
    # already has a handler.
    for prior_handler in list(root.handlers):
        root.removeHandler(prior_handler)
    root.addHandler(stream_handler)


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.analytics.sync",
        description=(
            "Replay records from ANALYTICS_LOG_PATH into the Postgres "
            "analytics service at ANALYTICS_DB_URL. Deduplicates by "
            "content hash so repeated runs are idempotent. No-op when "
            "either env var is unset or the JSONL file is absent."
        ),
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=None,
        help=(
            "Override ANALYTICS_LOG_PATH for this run. Useful for "
            "replaying a rotated / archived JSONL file."
        ),
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help=(
            "Override ANALYTICS_DB_URL for this run. Accepts any libpq "
            "URL so a one-off replay against a different database does "
            "not require touching the environment."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def _print_cli_result(sync_result: SyncResult, cli_start: float) -> None:
    """Print the UTC summary retained even when structured logs are hidden."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    duration_s = sync_result.duration_s or round(
        time.monotonic() - cli_start, 3
    )
    print(
        f"{timestamp} analytics_sync: inserted={sync_result.inserted} "
        f"duplicate={sync_result.skipped_duplicate} "
        f"malformed={sync_result.skipped_malformed} "
        f"total_lines={sync_result.total_lines} "
        f"duration_s={duration_s:.3f}"
    )


def _run_cli(args: argparse.Namespace) -> int:
    cli_start = time.monotonic()
    try:
        sync_result = sync_jsonl_to_postgres(
            log_path=args.log_path,
            db_url=args.db_url,
        )
    except Exception:
        log.exception(
            "analytics_sync: failed after %.3fs",
            time.monotonic() - cli_start,
        )
        return 1
    _print_cli_result(sync_result, cli_start)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _cli_parser().parse_args(argv)
    _configure_cli_logging(args.log_level)
    return _run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
