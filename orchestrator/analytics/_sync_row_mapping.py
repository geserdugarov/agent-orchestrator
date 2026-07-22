# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validated analytics record mapping and positional row output."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from orchestrator.analytics._sync_row_parse import (
    _content_hash,
    _required_columns,
)
from orchestrator.analytics._sync_row_parse import _extra_columns
from orchestrator.analytics._sync_row_schema import (
    _JSONB_COLUMNS,
    _PROMOTED_COLUMNS,
)


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
    return f"INSERT INTO analytics_events ({column_list}) VALUES ({placeholders}) ON CONFLICT (content_hash) DO NOTHING"


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
