# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.analytics_read_helpers import (
    _FakeConnection,
    _reload_read,
)

# The stand-in DSN the lifecycle tests thread through
# `analytics_connection(db_url=...)` and the default-factory probe;
# only its identity matters, the fake connection never dials it.
_DB_URL = "postgresql://h/db"


class OperationalError(Exception):
    """Stand-in for `psycopg.OperationalError`.

    `_is_broken_connection_exc` matches broken sockets by class
    `__name__`, so the name must be verbatim (no leading underscore)
    for the fake to drive the close-and-replace path without psycopg
    installed.
    """


class InterfaceError(Exception):
    """Stand-in for `psycopg.InterfaceError`, matched by class name
    the same way as `OperationalError`.
    """


class _ConnectionScopeCase(unittest.TestCase):
    """Reload the read module under a hermetic env and clear any
    stale thread-local connection before each lifecycle test.

    The reload gives a fresh `_thread_local`, but the explicit
    teardown first drops any entry a prior test left behind so a
    failure cannot bleed into this one.
    """

    def setUp(self) -> None:
        analytics_read = _reload_read()
        analytics_read.close_thread_local_connection()
        self.analytics_read = analytics_read


class AnalyticsConnectionScopeTest(_ConnectionScopeCase):
    """`analytics_connection` is a context manager that:

    - yields `None` when ``ANALYTICS_DB_URL`` is unset (so the
      dashboard's "no data" path still works);
    - opens the connection lazily via the injected factory and reuses
      the same connection across subsequent `with` blocks on the
      same thread;
    - closes-and-replaces the cached connection when an
      `OperationalError` / `InterfaceError` (wrapped or raw) escapes
      the `with` block, so the next caller opens a fresh socket;
    - exposes `close_thread_local_connection()` for explicit
      teardown.
    """

    def test_yields_none_when_db_url_unset(self) -> None:
        analytics_read = _reload_read(db_url="")
        with analytics_read.analytics_connection() as conn:
            self.assertIsNone(conn)

    def test_reuses_conn_across_scopes_on_same_thread(self) -> None:
        analytics_read = self.analytics_read
        opens: list[str] = []
        conn_obj = _FakeConnection()
        with analytics_read.analytics_connection(
            connect=lambda url: opens.append(url) or conn_obj,
        ) as c1:
            self.assertIs(c1, conn_obj)
        with analytics_read.analytics_connection(
            connect=lambda url: opens.append(url) or conn_obj,
        ) as c2:
            self.assertIs(c2, conn_obj)
        # The factory opened the socket once; the second scope reused it.
        self.assertEqual(len(opens), 1)
        # Persistent connection: the CM must NOT close on normal exit.
        self.assertEqual(conn_obj.close_called, 0)

    def test_close_thread_local_closes_cached_conn(self) -> None:
        analytics_read = self.analytics_read
        conn_obj = _FakeConnection()
        with analytics_read.analytics_connection(
            connect=conn_obj.as_connect,
        ) as opened:
            self.assertIs(opened, conn_obj)
        analytics_read.close_thread_local_connection()
        self.assertEqual(conn_obj.close_called, 1)
        # Idempotent: a second teardown does not raise or re-close.
        analytics_read.close_thread_local_connection()
        self.assertEqual(conn_obj.close_called, 1)

    def test_broken_conn_clears_thread_local(self) -> None:
        analytics_read = self.analytics_read
        first = _FakeConnection()
        second = _FakeConnection()
        sequence = iter((first, second))
        # First scope opens `first`, raises a broken-connection error
        # mid-scope; the CM closes-and-discards `first` so the next
        # scope opens `second`.
        with self.assertRaises(OperationalError):
            with analytics_read.analytics_connection(
                connect=lambda _url: next(sequence),
            ):
                raise OperationalError("server closed the connection")
        self.assertEqual(first.close_called, 1)
        with analytics_read.analytics_connection(
            connect=lambda _url: next(sequence),
        ) as c2:
            self.assertIs(c2, second)
        # `second` is not closed on normal exit (persistent).
        self.assertEqual(second.close_called, 0)

    def test_unrelated_error_does_not_invalidate(self) -> None:
        # A SQL syntax error or programmer mistake is NOT a torn-down
        # socket; the cached connection must survive so subsequent
        # reads on the same thread reuse it.
        analytics_read = self.analytics_read
        conn_obj = _FakeConnection()
        with self.assertRaises(ValueError):
            with analytics_read.analytics_connection(
                connect=conn_obj.as_connect,
            ):
                raise ValueError("not a broken socket")
        self.assertEqual(conn_obj.close_called, 0)
        with analytics_read.analytics_connection(
            connect=conn_obj.as_connect,
        ) as c2:
            self.assertIs(c2, conn_obj)


class AnalyticsConnectionUrlKeyTest(_ConnectionScopeCase):
    """The thread-local cache is keyed on the resolved URL and the
    default persistent factory opens with `autocommit=True`.
    """

    def test_db_url_change_replaces_cached_conn(self) -> None:
        # If a later `with` block on the same thread asks for a
        # different `db_url=`, the stale socket has to close before a
        # fresh one opens. Otherwise a thread that first read from DB
        # A would silently keep reading from A even after the caller
        # switched to DB B.
        seen: list[str] = []
        first = _FakeConnection()
        second = _FakeConnection()
        pending = iter((first, second))
        with self.analytics_read.analytics_connection(
            db_url="postgresql://A/db",
            connect=lambda url: seen.append(url) or next(pending),
        ) as opened:
            self.assertIs(opened, first)
        with self.analytics_read.analytics_connection(
            db_url="postgresql://B/db",
            connect=lambda url: seen.append(url) or next(pending),
        ) as opened:
            self.assertIs(opened, second)
        self.assertEqual(seen, ["postgresql://A/db", "postgresql://B/db"])
        # `first` (opened for DB A) was closed when the URL changed.
        self.assertEqual(first.close_called, 1)
        # `second` persists for further reads on DB B.
        self.assertEqual(second.close_called, 0)

    def test_same_url_does_not_reopen(self) -> None:
        # Re-entering with the same explicit URL on the same thread
        # reuses the cached connection -- the URL-change invalidation
        # must not over-trigger.
        opens: list[str] = []
        cached = _FakeConnection()
        with self.analytics_read.analytics_connection(
            db_url=_DB_URL,
            connect=lambda url: opens.append(url) or cached,
        ) as opened:
            self.assertIs(opened, cached)
        with self.analytics_read.analytics_connection(
            db_url=_DB_URL,
            connect=lambda url: opens.append(url) or cached,
        ) as opened:
            self.assertIs(opened, cached)
        self.assertEqual(opens, [_DB_URL])

    def test_persistent_factory_sets_autocommit(self) -> None:
        # The default factory wraps `psycopg.connect(db_url,
        # autocommit=True)` so a long-lived thread-local socket does
        # not leave the session idle in transaction after every
        # SELECT. The stubbed `psycopg.connect` captures the kwargs;
        # the real driver need not be installed for the contract.
        captured: dict[str, Any] = {}
        fake_psycopg = SimpleNamespace(
            connect=lambda url, **connect_kwargs: captured.update(url=url, kwargs=connect_kwargs) or _FakeConnection(),
        )
        with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            conn = self.analytics_read._default_persistent_connect(_DB_URL)
        self.assertEqual(captured["url"], _DB_URL)
        self.assertEqual(captured["kwargs"].get("autocommit"), True)
        self.assertIsNotNone(conn)


class IsBrokenConnectionExcTest(unittest.TestCase):
    """The broken-connection detector unwraps `AnalyticsReadError`
    (every driver-level failure goes through `_query` which wraps)
    and matches by class name so a fake without psycopg installed
    can drive the close-and-replace path.
    """

    def test_matches_by_class_name(self) -> None:
        analytics_read = _reload_read()
        self.assertTrue(
            analytics_read._is_broken_connection_exc(OperationalError("dead")),
        )
        self.assertTrue(
            analytics_read._is_broken_connection_exc(InterfaceError("dead")),
        )

    def test_unwraps_analytics_read_error(self) -> None:
        analytics_read = _reload_read()
        wrapper = analytics_read.AnalyticsReadError("wrap")
        wrapper.__cause__ = OperationalError("dead")
        self.assertTrue(analytics_read._is_broken_connection_exc(wrapper))

    def test_unrelated_error_is_not_broken(self) -> None:
        analytics_read = _reload_read()
        self.assertFalse(
            analytics_read._is_broken_connection_exc(
                ValueError("not a broken socket"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
