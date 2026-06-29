# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Single-SELECT query execution for the analytics read path.

`_query` runs one read-only SELECT and returns every row as a tuple,
either reusing a caller-owned `conn=` (typically an
`analytics_connection` scope) or opening and closing a fresh
connection via the injected `connect_fn`. Driver-level failures are
wrapped in `AnalyticsReadError` so callers have one exception type to
catch regardless of which step failed.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from .connection import AnalyticsReadError, _close_quietly


def _query(
    connect_fn: Callable[[str], Any],
    db_url: Optional[str],
    sql: str,
    params: Sequence[Any] = (),
    *,
    conn: Any = None,
) -> list[tuple]:
    """Run a single SELECT and return all rows as tuples.

    When `conn` is provided, reuse it -- the caller owns the
    connection's lifetime (typically an `analytics_connection`
    scope) and the query path neither opens nor closes a descriptor.
    Otherwise open a fresh connection via `connect_fn(db_url)`, run
    the SELECT, and close it in a `finally` so a query that raises
    mid-stream does not leak the descriptor. Read-only path either
    way -- no commit, no rollback. Any driver-level exception is
    wrapped in `AnalyticsReadError` so callers have one type to catch
    regardless of whether the failure was the connect, the execute,
    or the fetch.
    """
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
        except Exception as e:
            raise AnalyticsReadError(
                f"analytics query failed: {e}"
            ) from e
        return list(rows or [])
    try:
        opened = connect_fn(db_url)
    except AnalyticsReadError:
        raise
    except Exception as e:
        raise AnalyticsReadError(
            f"could not connect to analytics database: {e}"
        ) from e
    try:
        try:
            with opened.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
        except Exception as e:
            raise AnalyticsReadError(
                f"analytics query failed: {e}"
            ) from e
    finally:
        _close_quietly(opened)
    return list(rows or [])
