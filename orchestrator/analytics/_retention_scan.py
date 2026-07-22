# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Retention timestamp parsing and kept-record scans."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from orchestrator.analytics._recording import log

_KeptRemoved = tuple[list[str], int]


def _probe_exists(path: Path) -> bool:
    """True if `path` exists; False when it is absent or the probe raised.

    `Path.exists()` re-raises OSErrors that do not mean "absent" -- e.g.
    ENAMETOOLONG on a misconfigured path -- so the probe itself must be
    guarded, otherwise it escapes the per-tick caller. A probe failure is
    logged and treated as "absent" (a no-op prune), same as a read/rewrite
    OSError.
    """
    try:
        return path.exists()
    except OSError as error:
        log.warning("could not probe %s for prune: %s", path, error)
        return False


def _prune_timestamp(raw_line: str) -> Optional[datetime]:
    """Parse a JSONL record timestamp, returning None for kept malformed data."""
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    raw_timestamp = record.get("ts") if isinstance(record, dict) else None
    if not isinstance(raw_timestamp, str):
        return None
    try:
        timestamp = datetime.fromisoformat(raw_timestamp)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _normalized_jsonl_line(raw_line: str) -> str:
    if raw_line.endswith("\n"):
        return raw_line
    return f"{raw_line}\n"


@dataclass
class _PruneScan:
    """Mutable partition of retained and expired JSONL records."""

    kept: list[str] = field(default_factory=list)
    removed: int = 0

    def add(self, raw_line: str, cutoff: datetime) -> None:
        if not raw_line.strip():
            return
        timestamp = _prune_timestamp(raw_line)
        if timestamp is not None and timestamp < cutoff:
            self.removed += 1
            return
        self.kept.append(_normalized_jsonl_line(raw_line))


def _read_kept_records(
    path: Path,
    cutoff: datetime,
) -> Optional[_KeptRemoved]:
    """Split `path`'s lines into (kept, removed_count) by the `cutoff` time.

    A record is removed only when its `ts` parses to a time strictly before
    `cutoff`. Records whose `ts` is missing / non-string / unparseable, and
    lines that are not valid JSON, are kept verbatim so the prune never
    silently drops data an operator can clean up; a naive `ts` is read as UTC
    to match the writer's forward-compat behavior. Returns None when the read
    itself raises OSError, which the caller turns into a logged no-op.
    """
    scan = _PruneScan()
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                scan.add(raw_line, cutoff)
    except OSError as error:
        log.warning("could not read file %s for prune: %s", path, error)
        return None
    return scan.kept, scan.removed
