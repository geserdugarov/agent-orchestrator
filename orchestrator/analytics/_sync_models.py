# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed counters, results, and ingest context for analytics sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


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
    duration_s: float = field(default_factory=float)


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


@dataclass(frozen=True)
class _IngestContext:
    """Stable inputs and mutable counters shared by one ingest pass."""

    log_path: Path
    insert_sql: str
    source_path: Optional[str]
    json_adapter: Callable[[Any], Any]
    counters: _SyncCounters
    start: float
