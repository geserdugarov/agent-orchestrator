# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Canonical hashing and required-field parsing for sync rows."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from orchestrator.analytics._sync_row_schema import (
    _COL_EVENT,
    _COL_ISSUE,
    _COL_REPO,
    _COL_TS,
    _PROMOTED_COLUMNS,
    _REQUIRED_KEYS,
)


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
