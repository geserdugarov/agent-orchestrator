# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Atomic JSONL retention rewrites under the sink lock."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from orchestrator.analytics._recording import log
from orchestrator.analytics._retention_scan import (
    _probe_exists,
    _read_kept_records,
)


def _unlink_quietly(path: str) -> None:
    """Remove `path`, ignoring a missing or unremovable file.

    Best-effort cleanup of the prune's temp file when the rewrite fails; an
    unlink failure leaves an orphaned `.prune.*.tmp` but never masks the write
    error that triggered the cleanup.
    """
    with contextlib.suppress(OSError):
        os.unlink(path)


def _flush_fd_and_replace(
    tmp_fd: int,
    tmp_path: str,
    path: Path,
    lines: list[str],
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
    path: Path,
    cutoff: datetime,
    lock: threading.Lock,
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
            log.warning("could not rewrite file %s after prune: %s", path, error)
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
