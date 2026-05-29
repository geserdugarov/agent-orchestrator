# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""JSONL -> Postgres replay for the analytics sink.

`orchestrator/analytics/` writes one JSON object per line to
`config.ANALYTICS_LOG_PATH`. This module reads that file and inserts
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

Connection settings come from `config.ANALYTICS_DB_URL`, a single
libpq URL. There is no hardcoded localhost fallback; the sync is a
no-op when the URL is unset so operators who have not deployed the
Postgres service can run the CLI without configuring it. To move the
database off-host, repoint the URL -- no code change required.

The sync is operator-driven: not wired into the polling loop. Run
`python -m orchestrator.analytics_sync` (or import
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import config

log = logging.getLogger(__name__)

# Columns the table promotes from the JSONL record; anything else lands
# in `extras` JSONB so a JSONL record from a newer orchestrator version
# never loses fields. Kept here (not in `orchestrator/analytics/`) because
# it is a database-shape concern, not a record-build concern.
_PROMOTED_COLUMNS = (
    "ts",
    "repo",
    "issue",
    "event",
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

_REQUIRED_KEYS = ("ts", "repo", "issue", "event")


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
    """

    inserted: int = 0
    skipped_duplicate: int = 0
    skipped_malformed: int = 0
    total_lines: int = 0
    malformed_line_numbers: tuple[int, ...] = field(default_factory=tuple)


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


def _split_row(record: dict) -> Optional[tuple[dict, dict]]:
    """Promote known columns and route the rest to `extras`.

    Returns (columns, extras), or None if a required key is missing
    or `ts` does not parse. The caller treats None as a malformed-line
    skip so a record with garbled `ts` does not abort the entire sync.
    """
    for key in _REQUIRED_KEYS:
        if key not in record:
            return None
    ts = _parse_ts(record.get("ts"))
    if ts is None:
        return None
    repo = record.get("repo")
    if not isinstance(repo, str) or not repo:
        return None
    try:
        issue = int(record["issue"])
    except (TypeError, ValueError):
        return None
    event = record.get("event")
    if not isinstance(event, str) or not event:
        return None

    columns: dict[str, Any] = {
        "ts": ts,
        "repo": repo,
        "issue": issue,
        "event": event,
    }
    extras: dict[str, Any] = {}
    for key, value in record.items():
        if key in ("ts", "repo", "issue", "event"):
            continue
        if key in _PROMOTED_COLUMNS:
            columns[key] = value
        else:
            extras[key] = value
    return columns, extras


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


def _row_values(
    columns: dict,
    extras: dict,
    source_path: Optional[str],
    source_line: int,
    content_hash: str,
    json_adapter: Callable[[Any], Any],
) -> tuple:
    values: list[Any] = []
    for col in _PROMOTED_COLUMNS:
        value = columns.get(col)
        if col in _JSONB_COLUMNS and value is not None:
            value = json_adapter(value)
        values.append(value)
    values.append(json_adapter(extras) if extras else None)
    values.append(source_path)
    values.append(source_line)
    values.append(content_hash)
    return tuple(values)


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
    except ImportError as e:
        raise RuntimeError(
            "psycopg is required for analytics_sync; "
            "run `uv sync --locked` to install it"
        ) from e
    return psycopg.connect(db_url)


def _default_json_adapter(value: Any) -> Any:
    """Adapt dict / list to the psycopg JSON wrapper when available.

    Falls back to passing the raw Python object through; psycopg v3's
    default adaptation already handles dict / list as JSONB inserts
    so the wrapper is optional. The factory pattern lets tests inject
    `lambda v: v` and inspect raw structures.
    """
    try:
        from psycopg.types.json import Json
    except ImportError:
        return value
    return Json(value)


def sync_jsonl_to_postgres(
    *,
    log_path: Optional[Path] = None,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
    json_adapter: Optional[Callable[[Any], Any]] = None,
) -> SyncResult:
    """Replay every record in `log_path` into Postgres at `db_url`.

    Defaults come from `config.ANALYTICS_LOG_PATH` and
    `config.ANALYTICS_DB_URL`; either being None or the JSONL file
    being absent yields an empty SyncResult (the no-op path so the
    CLI is safe to schedule before the operator deploys Postgres).

    Malformed lines are logged and counted but never abort the
    sync; the JSONL file is treated as read-only -- this sync never
    rewrites or truncates it, even when it sees malformed lines.

    `connect(db_url) -> connection` and `json_adapter(value) -> value`
    are factory hooks so tests can inject a fake without depending on
    psycopg. Production callers leave both at None to get the real
    psycopg connection and the default `Json` wrapper.
    """
    if log_path is None:
        log_path = config.ANALYTICS_LOG_PATH
    if db_url is None:
        db_url = config.ANALYTICS_DB_URL
    connect_fn = connect or _default_connect
    json_adapter_fn = json_adapter or _default_json_adapter

    if log_path is None:
        log.info("analytics_sync: ANALYTICS_LOG_PATH not configured; nothing to sync")
        return SyncResult()
    if not db_url:
        log.info("analytics_sync: ANALYTICS_DB_URL not configured; nothing to sync")
        return SyncResult()
    if not Path(log_path).exists():
        log.info("analytics_sync: %s does not exist yet; nothing to sync", log_path)
        return SyncResult()

    insert_sql = _build_insert_sql()
    source_path_str = str(log_path)

    inserted = 0
    skipped_duplicate = 0
    skipped_malformed = 0
    total_lines = 0
    malformed_lines: list[int] = []

    conn = connect_fn(db_url)
    try:
        with conn.cursor() as cur:
            with Path(log_path).open("r", encoding="utf-8") as fh:
                for line_number, raw_line in enumerate(fh, start=1):
                    total_lines += 1
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError:
                        skipped_malformed += 1
                        malformed_lines.append(line_number)
                        log.warning(
                            "analytics_sync: skipping line %d (not JSON) in %s",
                            line_number, log_path,
                        )
                        continue
                    if not isinstance(record, dict):
                        skipped_malformed += 1
                        malformed_lines.append(line_number)
                        log.warning(
                            "analytics_sync: skipping line %d (JSON not an object) in %s",
                            line_number, log_path,
                        )
                        continue
                    split = _split_row(record)
                    if split is None:
                        skipped_malformed += 1
                        malformed_lines.append(line_number)
                        log.warning(
                            "analytics_sync: skipping line %d (missing/invalid required keys) in %s",
                            line_number, log_path,
                        )
                        continue
                    columns, extras = split
                    content_hash = _content_hash(record)
                    values = _row_values(
                        columns,
                        extras,
                        source_path_str,
                        line_number,
                        content_hash,
                        json_adapter_fn,
                    )
                    cur.execute(insert_sql, values)
                    # psycopg's rowcount is 1 on insert, 0 on conflict
                    # skip; fall back to counting inserts as "new" so
                    # a driver that reports -1 still produces useful
                    # totals (the duplicate count becomes 0 in that
                    # case, which is acceptable -- the database is the
                    # authority).
                    rowcount = getattr(cur, "rowcount", 1)
                    if rowcount == 0:
                        skipped_duplicate += 1
                    else:
                        inserted += 1
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            log.exception("analytics_sync: rollback failed")
        raise
    finally:
        try:
            conn.close()
        except Exception:
            log.exception("analytics_sync: connection close failed")

    log.info(
        "analytics_sync: inserted=%d duplicate=%d malformed=%d total_lines=%d source=%s",
        inserted, skipped_duplicate, skipped_malformed, total_lines, log_path,
    )
    return SyncResult(
        inserted=inserted,
        skipped_duplicate=skipped_duplicate,
        skipped_malformed=skipped_malformed,
        total_lines=total_lines,
        malformed_line_numbers=tuple(malformed_lines),
    )


def _configure_cli_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.analytics_sync",
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
    args = parser.parse_args(argv)

    _configure_cli_logging(args.log_level)

    try:
        result = sync_jsonl_to_postgres(
            log_path=args.log_path,
            db_url=args.db_url,
        )
    except Exception:
        log.exception("analytics_sync: failed")
        return 1

    # CLI users want a one-line human-readable summary in addition to
    # the structured log line; print to stdout so it survives
    # `--log-level WARNING`.
    print(
        f"analytics_sync: inserted={result.inserted} "
        f"duplicate={result.skipped_duplicate} "
        f"malformed={result.skipped_malformed} "
        f"total_lines={result.total_lines}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
