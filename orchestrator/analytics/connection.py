# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Connection lifecycle and error type for the analytics read path.

Owns `AnalyticsReadError` (the single exception every read wraps a
driver failure in), the deferred-psycopg connect factories
(`_default_connect` for the open-per-query path, `_default_persistent_connect`
for the autocommit persistent socket), and the thread-local persistent
connection cache behind `analytics_connection` /
`close_thread_local_connection`. The psycopg import is deferred to call
time so the module load path stays driver-free and tests can inject a
fake `connect(db_url)` factory.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from orchestrator.analytics._connection_cache import (
    _cached_entry as _cached_entry,
    _connection_for_url as _connection_for_url,
    _discard_broken_connection as _discard_broken_connection,
    _open_cached_connection as _open_cached_connection,
)
from orchestrator.analytics.db_url import _resolve_db_url

log = logging.getLogger(__name__)
_COMPATIBILITY_EXPORTS = (_cached_entry, _open_cached_connection)


class AnalyticsReadError(RuntimeError):
    """Raised when a query against the analytics DB fails.

    The original psycopg / driver exception is preserved as
    ``__cause__`` so the caller can introspect it for logging without
    the read module re-exporting psycopg's exception hierarchy.
    """


def _default_connect(db_url: str) -> Any:
    """Lazy psycopg import so the module loads without the driver.

    `pyproject.toml` pins `psycopg[binary]`, but the dashboard's read
    path must not surface an ImportError when imported by callers
    that only consume the dataclasses (typing, tests, docs builds).
    Deferring the import to call time keeps the module load path
    driver-free, mirroring `analytics.sync._default_connect`.
    """
    try:
        import psycopg
    except ImportError as error:
        raise AnalyticsReadError(
            "psycopg is required for analytics.read; run `uv sync --locked` to install it"
        ) from error
    try:
        return psycopg.connect(db_url)
    except Exception as error:
        raise AnalyticsReadError(f"could not connect to analytics database: {error}") from error


def _default_persistent_connect(db_url: str) -> Any:
    """`_default_connect` variant that opens with `autocommit=True`.

    `analytics_connection` keeps a single connection alive across
    many sequential reads on the same thread; psycopg's default
    "implicit transaction on first statement" behavior would leave
    the session idle in transaction after every SELECT (holding
    xmin, blocking vacuum) and, on a query error, in `aborted`
    state -- every subsequent read on the same thread-local would
    raise `InFailedSqlTransaction` until something rolled it back.
    Autocommit avoids both. This path is read-only by design; any
    future caller that needs an explicit transaction should open
    one inline with `with conn.transaction():` rather than
    disabling autocommit globally.
    """
    try:
        import psycopg
    except ImportError as error:
        raise AnalyticsReadError(
            "psycopg is required for analytics.read; run `uv sync --locked` to install it"
        ) from error
    try:
        return psycopg.connect(db_url, autocommit=True)
    except Exception as error:
        raise AnalyticsReadError(f"could not connect to analytics database: {error}") from error


_thread_local = threading.local()


def _is_broken_connection_exc(exc: BaseException) -> bool:
    """True when `exc` looks like a torn-down psycopg socket.

    The check unwraps an `AnalyticsReadError` to inspect its
    `__cause__` (every driver-level error wraps through `_query`).
    Class-name matching covers the common test case where a fake
    cursor raises a shim `OperationalError` / `InterfaceError`
    without psycopg installed; falls back to an `isinstance` check
    against the real psycopg classes when the driver is present.
    """
    cause: Optional[BaseException]
    if isinstance(exc, AnalyticsReadError):
        cause = exc.__cause__
    else:
        cause = exc
    if cause is None:
        return False
    name = type(cause).__name__
    if name in ("OperationalError", "InterfaceError"):
        return True
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(cause, (psycopg.OperationalError, psycopg.InterfaceError))


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        log.exception("analytics.read: connection close failed")


@contextmanager
def analytics_connection(
    *,
    db_url: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
) -> Iterator[Any]:
    """Yield a persistent thread-local analytics connection.

    Yields ``None`` when `ANALYTICS_DB_URL` is unset (every public
    read helper short-circuits on `conn=None`, so the caller still
    renders a "no data" page rather than crashing). Otherwise a
    single connection is cached per-thread and reused across
    subsequent `with analytics_connection()` blocks on the same
    thread -- the first call pays the ~1 s psycopg handshake, every
    later call reuses the open socket. Real psycopg connections open
    with `autocommit=True`; see `_default_persistent_connect`.

    The cache is keyed on the resolved URL: if a later `with` block
    on the same thread asks for a different `db_url=` than the one
    the cached connection was opened against, the stale socket is
    closed and a fresh one is opened. Without this guard a thread
    that first read from DB A would silently keep reading from A
    even after the caller switched to DB B, which would violate the
    `db_url=` contract.

    If a broken-connection error (`OperationalError` /
    `InterfaceError`, wrapped or raw) escapes the `with` block, the
    cached connection is closed-and-replaced before the exception
    re-raises so the next caller on the same thread opens a fresh
    socket. The connection is NOT closed on normal scope exit (it
    survives to be reused); call `close_thread_local_connection()`
    explicitly at shutdown or between tests.

    Tests inject a fake `connect(db_url) -> conn` factory the same
    shape as every public helper accepts.
    """
    url = _resolve_db_url(db_url)
    if not url:
        yield None
        return
    conn = _connection_for_url(url, connect or _default_persistent_connect)
    try:
        yield conn
    except BaseException as exc:
        _discard_broken_connection(exc)
        raise


def close_thread_local_connection() -> None:
    """Tear down any thread-local analytics connection on this thread.

    No-op when no connection is open. Intended for shutdown hooks
    and test teardown so a stale connection from one test does not
    bleed into the next.
    """
    entry = getattr(_thread_local, "entry", None)
    if entry is None:
        return
    _thread_local.entry = None
    _close_quietly(entry[1])
