# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Public analytics and trajectory retention entry points."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from orchestrator.analytics._recording import (
    _FILE_LOCK as _FILE_LOCK,
    _live_settings as _live_settings,
    log as log,
)
from orchestrator.analytics._retention_rewrite import (
    _atomic_rewrite as _atomic_rewrite,
    _flush_fd_and_replace as _flush_fd_and_replace,
    _prune_jsonl_records as _prune_jsonl_records,
    _rewrite_pruned_file as _rewrite_pruned_file,
    _unlink_quietly as _unlink_quietly,
)
from orchestrator.analytics._retention_scan import (
    _PruneScan as _PruneScan,
    _normalized_jsonl_line as _normalized_jsonl_line,
    _probe_exists as _probe_exists,
    _prune_timestamp as _prune_timestamp,
    _read_kept_records as _read_kept_records,
)
from orchestrator.analytics._trajectories import (
    _TRAJECTORY_FILE_LOCK as _TRAJECTORY_FILE_LOCK,
)


_COMPATIBILITY_EXPORTS = (
    _atomic_rewrite,
    _flush_fd_and_replace,
    _rewrite_pruned_file,
    _unlink_quietly,
    _PruneScan,
    _normalized_jsonl_line,
    _probe_exists,
    _prune_timestamp,
    _read_kept_records,
)


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
        settings.ANALYTICS_LOG_PATH,
        settings.ANALYTICS_RETENTION_DAYS,
        _FILE_LOCK,
        now,
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
        settings.TRAJECTORY_LOG_PATH,
        settings.TRAJECTORY_RETENTION_DAYS,
        _TRAJECTORY_FILE_LOCK,
        now,
    )
