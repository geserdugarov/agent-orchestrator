# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Record -> DB row mapping and dedup-hash layer for the analytics sync.

`orchestrator.analytics.sync` owns the JSONL -> Postgres ingest driver
(connection lifecycle, batching, rollup refresh, CLI). This module carries the
pure, driver-free half of that pipeline: the promoted-column / JSONB / required
-key schema, the canonical-JSON content hash used for idempotent dedup, and the
per-record validation that promotes known columns, routes the rest to `extras`,
and turns a validated record into the positional row tuple the INSERT expects.

Kept import-light (stdlib + typing only, no psycopg) and one-directional -- it
never imports from `sync`, so the ingest driver depends on the mapping layer and
not the other way around.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# Required JSONL/DB record field names, shared by the promoted-column list, the
# required-key guard, and the per-record extraction.
_COL_TS = "ts"
_COL_REPO = "repo"
_COL_ISSUE = "issue"
_COL_EVENT = "event"


# Columns the table promotes from the JSONL record; anything else lands
# in `extras` JSONB so a JSONL record from a newer orchestrator version
# never loses fields. Kept here (not in `orchestrator/analytics/`) because
# it is a database-shape concern, not a record-build concern.
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
    placeholders = ", ".join("%s" for _ in columns)
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


@dataclass(frozen=True)
class _PreparedRecord:
    """Validated promoted fields, extras, and hash for one JSONL record."""

    columns: dict[str, Any]
    extras: dict[str, Any]
    content_hash: str


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
