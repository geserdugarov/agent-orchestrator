# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Thread-local analytics connection cache operations."""

from __future__ import annotations

from typing import Any, Callable, Optional


def _cached_entry(url: str) -> Optional[tuple[str, Any]]:
    """Return this thread's matching cache entry, closing a stale one."""
    from orchestrator.analytics import connection as _owner

    entry = getattr(_owner._thread_local, "entry", None)
    if entry is None:
        return None
    cached_url, cached_conn = entry
    if cached_url == url:
        return entry
    _owner._thread_local.entry = None
    _owner._close_quietly(cached_conn)
    return None


def _open_cached_connection(
    url: str,
    connect_fn: Callable[[str], Any],
) -> Any:
    """Open and cache one persistent connection with normalized errors."""
    from orchestrator.analytics import connection as _owner

    try:
        conn = connect_fn(url)
    except _owner.AnalyticsReadError:
        raise
    except Exception as error:
        raise _owner.AnalyticsReadError(
            f"could not connect to analytics database: {error}",
        ) from error
    _owner._thread_local.entry = (url, conn)
    return conn


def _connection_for_url(
    url: str,
    connect_fn: Callable[[str], Any],
) -> Any:
    entry = _cached_entry(url)
    if entry is None:
        return _open_cached_connection(url, connect_fn)
    return entry[1]


def _discard_broken_connection(exc: BaseException) -> None:
    """Evict this thread's cached socket when the escaped error broke it."""
    from orchestrator.analytics import connection as _owner

    if not _owner._is_broken_connection_exc(exc):
        return
    entry = getattr(_owner._thread_local, "entry", None)
    if entry is None:
        return
    _owner._thread_local.entry = None
    _owner._close_quietly(entry[1])
