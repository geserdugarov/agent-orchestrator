# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Single-SELECT query execution for the analytics read path.

`_ReadQuery` resolves the configured URL and injected connection path for a
reader. `_query` runs one read-only SELECT and returns every row as a tuple,
either reusing a caller-owned `conn=` (typically an
`analytics_connection` scope) or opening and closing a fresh
connection via the injected `connect_fn`. Driver-level failures are
wrapped in `AnalyticsReadError` so callers have one exception type to
catch regardless of which step failed.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from orchestrator.analytics.connection import (
    AnalyticsReadError,
    _close_quietly,
    _default_connect,
)
from orchestrator.analytics.db_url import _resolve_db_url


@dataclass(frozen=True)
class _ReadQuery:
    """Resolved connection inputs shared by one public read operation."""

    db_url: Optional[str]
    connect_fn: Callable[[str], Any]
    conn: Any

    @classmethod
    def resolve(
        cls,
        db_url: Optional[str],
        connect: Optional[Callable[[str], Any]],
        conn: Any,
    ) -> _ReadQuery:
        return cls(
            db_url=_resolve_db_url(db_url),
            connect_fn=connect or _default_connect,
            conn=conn,
        )

    @property
    def available(self) -> bool:
        """Whether a supplied connection or configured URL can serve reads."""
        return self.conn is not None or bool(self.db_url)

    def select(
        self,
        sql: str,
        bindings: Sequence[Any] = (),
    ) -> list[tuple]:
        """Execute one SELECT through the resolved connection path."""
        return _query(
            self.connect_fn,
            self.db_url,
            sql,
            bindings,
            conn=self.conn,
        )


def _execute_select(
    conn: Any, sql: str, bindings: Sequence[Any],
) -> list[tuple]:
    """Run one SELECT on `conn` and return every row as a tuple.

    Neither opens nor closes `conn` -- the caller owns its lifetime. Any
    driver-level exception (cursor, execute, or fetch) is wrapped in
    `AnalyticsReadError` so callers have one type to catch.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(bindings))
            rows = cur.fetchall()
    except Exception as error:
        raise AnalyticsReadError(f"analytics query failed: {error}") from error
    return list(rows or [])


def _connect_for_read(
    connect_fn: Callable[[str], Any], db_url: Optional[str],
) -> Any:
    """Open a fresh read connection, normalizing failures to
    `AnalyticsReadError`.

    An `AnalyticsReadError` the factory already raised (the default psycopg
    factory wraps its own connect failure) passes through unchanged rather
    than being double-wrapped; any other exception is wrapped so the caller
    sees a single type regardless of which driver raised it.
    """
    try:
        return connect_fn(db_url)
    except AnalyticsReadError:
        raise
    except Exception as error:
        raise AnalyticsReadError(
            f"could not connect to analytics database: {error}"
        ) from error


@contextlib.contextmanager
def _read_connection(connect_fn: Callable[[str], Any], db_url: Optional[str]):
    """Open a fresh read connection and close it (best-effort) on exit, so a
    query that raises mid-stream never leaks the descriptor."""
    opened = _connect_for_read(connect_fn, db_url)
    try:
        yield opened
    finally:
        _close_quietly(opened)


def _query(
    connect_fn: Callable[[str], Any],
    db_url: Optional[str],
    sql: str,
    bindings: Sequence[Any] = (),
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
        return _execute_select(conn, sql, bindings)
    with _read_connection(connect_fn, db_url) as opened:
        return _execute_select(opened, sql, bindings)
