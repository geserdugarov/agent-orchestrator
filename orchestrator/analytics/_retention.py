# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics and trajectory retention -- JSONL pruning and atomic rewrite.

Backs the by-age retention pass for both project-local JSONL sinks: the
always-on analytics event sink in `orchestrator.analytics._recording`
(`ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS`) and the opt-in
trajectory sink in `orchestrator.analytics._trajectories`
(`TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS`). Both wrappers share
one prune core (`_prune_jsonl_records`): drop every record whose `ts` is
older than the retention window, preserve malformed / unparseable lines
verbatim so cleanup stays operator-driven, and swap the file in place
through a temp file plus `os.replace` so a crash mid-prune cannot
truncate it.

Public entry points, re-exported on the `orchestrator.analytics` facade:

- `prune_old_records(*, now=None)` -- prune the analytics sink under its
  `_FILE_LOCK`.
- `prune_trajectory_records(*, now=None)` -- prune the trajectory sink
  under its own `_TRAJECTORY_FILE_LOCK`, never touching the analytics
  file.
- `prune_with_retention_logging()` -- the per-tick wrapper
  `main._run_tick` calls after every configured repo drains; it delegates
  to `prune_old_records` through the facade, swallows any exception
  (analytics is observability, never authoritative workflow state), and
  logs the removed-record count.

Each wrapper reads its sink's path / retention knob off the facade at
call time via `_recording._live_settings`, so a patched or reloaded
setting takes effect and `prune_with_retention_logging -> prune_old_records`
stays interceptable through the facade. The per-sink locks are the same
objects the append side holds -- `_recording._FILE_LOCK` and
`_trajectories._TRAJECTORY_FILE_LOCK` -- so a prune's read + rewrite
cannot race an `append_record` onto the soon-unlinked inode.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from orchestrator.analytics._recording import (
    _FILE_LOCK as _FILE_LOCK,
    _live_settings as _live_settings,
    log as log,
)
from orchestrator.analytics._trajectories import (
    _TRAJECTORY_FILE_LOCK as _TRAJECTORY_FILE_LOCK,
)

# One prune scan's outcome: the JSONL lines retained and the count removed.
_KeptRemoved = tuple[list[str], int]


def prune_with_retention_logging() -> None:
    """Drop analytics records past `ANALYTICS_RETENTION_DAYS` and log the
    outcome. Intended for the per-tick caller in `main._run_tick`.

    A no-op when the sink is disabled or retention is non-positive (the
    documented "keep raw data indefinitely" knob); `prune_old_records`
    itself handles the absent-file / unparseable-line / IO-failure cases.
    A runaway programming error here must not abort the polling loop --
    analytics is observability, never authoritative workflow state -- so
    any escape is logged and swallowed. Per-tick cadence is cheap: the
    helper reads the file at most once and only rewrites it when at
    least one record is older than the retention window.

    Delegates through the facade (`_live_settings().prune_old_records()`)
    so the call stays interceptable via `patch.object(analytics,
    "prune_old_records", ...)`.
    """
    try:
        removed = _live_settings().prune_old_records()
    except Exception:
        log.exception("analytics retention prune raised; continuing")
        return
    if removed:
        log.info("analytics retention prune removed %d record(s)", removed)


def prune_old_records(*, now: Optional[datetime] = None) -> int:
    """Remove records whose `ts` is older than `ANALYTICS_RETENTION_DAYS`.

    Reads the `ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS` bound on the
    `orchestrator.analytics` facade (parsed from the env at import).

    Returns the number of records removed. No-op (returns 0) when the
    sink is disabled, retention is non-positive (keep forever), or the
    file does not exist yet. `now` defaults to the current UTC time and
    is parameter-overridable so tests can pin the comparison point.

    Records whose `ts` is missing, not a string, or unparseable are
    preserved verbatim -- the prune step does not silently drop malformed
    data; an operator can clean it up. Likewise lines that are not valid
    JSON survive the rewrite.

    The rewrite goes through a temp file in the same directory followed
    by `os.replace` so a crash mid-prune cannot truncate the analytics
    file.

    Holds `_FILE_LOCK` across the read + rewrite so a concurrent
    `append_record` cannot land between the read and the `os.replace`
    -- without this, an append that observed the old inode after we
    read but before `os.replace` would write to the soon-unlinked inode
    and be silently lost. Scheduler workers may still be running when
    the polling loop calls this between ticks, so serializing with
    `append_record` is what keeps that prune-window invisible.
    """
    settings = _live_settings()
    return _prune_jsonl_records(
        settings.ANALYTICS_LOG_PATH, settings.ANALYTICS_RETENTION_DAYS,
        _FILE_LOCK, now,
    )


def prune_trajectory_records(*, now: Optional[datetime] = None) -> int:
    """Remove trajectory records older than `TRAJECTORY_RETENTION_DAYS`.

    Reads the `TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS` bound on
    the `orchestrator.analytics` facade. Mirrors `prune_old_records` exactly
    (no-op when the sink is disabled, retention is non-positive, or the
    file is absent; malformed / unparseable lines preserved; atomic
    temp-file + `os.replace` rewrite) but operates solely on the
    trajectory file under `_TRAJECTORY_FILE_LOCK` -- it never touches
    `ANALYTICS_LOG_PATH`, the analytics Postgres sync, or the dashboard
    rollups. `now` is parameter-overridable so tests can pin the
    comparison point.
    """
    settings = _live_settings()
    return _prune_jsonl_records(
        settings.TRAJECTORY_LOG_PATH, settings.TRAJECTORY_RETENTION_DAYS,
        _TRAJECTORY_FILE_LOCK, now,
    )


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
    path: Path, cutoff: datetime,
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


def _unlink_quietly(path: str) -> None:
    """Remove `path`, ignoring a missing or unremovable file.

    Best-effort cleanup of the prune's temp file when the rewrite fails; an
    unlink failure leaves an orphaned `.prune.*.tmp` but never masks the write
    error that triggered the cleanup.
    """
    with contextlib.suppress(OSError):
        os.unlink(path)


def _flush_fd_and_replace(
    tmp_fd: int, tmp_path: str, path: Path, lines: list[str],
) -> None:
    """Write `lines` through `tmp_fd`, then atomically replace `path`."""
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.replace(tmp_path, str(path))


def _atomic_rewrite(path: Path, lines: list[str]) -> None:
    """Replace `path`'s contents with `lines` via a temp file + `os.replace`.

    The temp file lands in `path.parent` so `os.replace` is a same-filesystem
    atomic rename: a crash mid-write cannot truncate the original. On any
    write / replace OSError the partial temp file is unlinked (best-effort)
    before the error propagates, so a failed prune leaves neither a truncated
    original nor an orphaned temp file.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f"{path.name}.prune.",
        suffix=".tmp",
    )
    try:
        _flush_fd_and_replace(tmp_fd, tmp_path, path, lines)
    except OSError:
        _unlink_quietly(tmp_path)
        raise


def _rewrite_pruned_file(
    path: Path, cutoff: datetime, lock: threading.Lock,
) -> int:
    """Under `lock`, drop records older than `cutoff` and return the count.

    The lock is held across the read + rewrite so a concurrent append cannot
    land on the soon-unlinked inode; every filesystem touch downgrades OSError
    to a logged no-op.
    """
    with lock:
        # Re-check existence under the lock: a concurrent operator `rm`
        # between the pre-lock probe and acquiring the lock would
        # otherwise let `path.open` raise an unhandled FileNotFoundError.
        if not _probe_exists(path):
            return 0
        kept_removed = _read_kept_records(path, cutoff)
        if kept_removed is None:
            return 0
        kept, removed = kept_removed
        if removed == 0:
            return 0
        try:
            _atomic_rewrite(path, kept)
        except OSError as error:
            log.warning(
                "could not rewrite file %s after prune: %s", path, error
            )
            return 0
        return removed


def _prune_jsonl_records(
    path: Optional[Path],
    days: int,
    lock: threading.Lock,
    now: Optional[datetime],
) -> int:
    """Remove records whose `ts` is older than `days` from `path` under
    `lock`.

    Shared core for the analytics and trajectory prune wrappers. Returns
    the number of records removed; a no-op (returns 0) when `path` is
    None (sink disabled), `days` is non-positive (keep forever), or the
    file does not exist. Malformed lines -- not valid JSON, or a record
    whose `ts` is missing / non-string / unparseable -- are preserved
    verbatim so the prune never silently drops data an operator can
    clean up. The rewrite goes through a temp file plus `os.replace` so
    a crash mid-prune cannot truncate the file, and `lock` is held
    across the read + rewrite so a concurrent append cannot land on the
    soon-unlinked inode.

    Every filesystem touch -- the existence probes (`_probe_exists`), the
    read (`_read_kept_records`), and the rewrite (`_atomic_rewrite`) --
    downgrades OSError to a logged no-op, so a misconfigured path (e.g.
    ENAMETOOLONG) never escapes to the per-tick caller.
    """
    if path is None or days <= 0:
        return 0
    # Pre-lock probe for the fast zero-cost no-op path on a disabled sink.
    if not _probe_exists(path):
        return 0

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return _rewrite_pruned_file(path, cutoff, lock)
