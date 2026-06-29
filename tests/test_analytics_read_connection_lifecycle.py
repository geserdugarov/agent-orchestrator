# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sys
import unittest
from typing import Any
from unittest.mock import patch

from tests.analytics_read_helpers import (
    _FakeConnection,
    _connector,
    _reload,
)


class AnalyticsConnectionScopeTest(unittest.TestCase):
    """`analytics_connection` is a context manager that:

    - yields `None` when ``ANALYTICS_DB_URL`` is unset (so the
      dashboard's "no data" path still works);
    - opens the connection lazily via the injected factory and reuses
      the same connection across subsequent `with` blocks on the
      same thread (the whole point of the rewrite -- the previous
      `_query` opened/closed per call);
    - closes-and-replaces the cached connection when an
      `OperationalError` / `InterfaceError` (wrapped or raw) escapes
      the `with` block, so the next caller opens a fresh socket;
    - exposes `close_thread_local_connection()` for explicit
      teardown.
    """

    def setUp(self) -> None:
        # Each test reloads the module under a hermetic env -- that
        # gives a fresh `_thread_local` -- but explicitly drop any
        # stale entry first so a prior test's failure cannot bleed
        # into this one.
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        analytics_read.close_thread_local_connection()
        self.analytics_read = analytics_read

    def test_yields_none_when_db_url_unset(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": ""})
        with analytics_read.analytics_connection() as conn:
            self.assertIsNone(conn)

    def test_reuses_connection_across_scopes_on_same_thread(self) -> None:
        analytics_read = self.analytics_read
        opens: list[str] = []
        conn_obj = _FakeConnection()

        def _factory(url: str) -> _FakeConnection:
            opens.append(url)
            return conn_obj

        with analytics_read.analytics_connection(connect=_factory) as c1:
            self.assertIs(c1, conn_obj)
        with analytics_read.analytics_connection(connect=_factory) as c2:
            self.assertIs(c2, conn_obj)
        self.assertEqual(len(opens), 1)
        # Persistent connection: the CM must NOT close on normal exit.
        self.assertEqual(conn_obj.close_called, 0)

    def test_close_thread_local_closes_cached_connection(self) -> None:
        analytics_read = self.analytics_read
        conn_obj = _FakeConnection()
        with analytics_read.analytics_connection(
            connect=_connector(conn_obj)
        ):
            pass
        analytics_read.close_thread_local_connection()
        self.assertEqual(conn_obj.close_called, 1)
        # Idempotent: a second teardown does not raise or re-close.
        analytics_read.close_thread_local_connection()
        self.assertEqual(conn_obj.close_called, 1)

    def test_broken_connection_invalidates_thread_local(self) -> None:
        analytics_read = self.analytics_read

        # Stand-in for psycopg.OperationalError -- the class-name
        # match in `_is_broken_connection_exc` lets the test simulate
        # the broken-socket path without psycopg installed. The
        # `__name__` must match verbatim (no leading underscore).
        class OperationalError(Exception):
            pass

        first = _FakeConnection()
        second = _FakeConnection()
        sequence = iter([first, second])

        def _factory(_url: str) -> _FakeConnection:
            return next(sequence)

        # First scope opens `first`, raises a broken-connection error
        # mid-scope; the CM must close-and-discard `first` so the
        # next scope opens `second`.
        with self.assertRaises(OperationalError):
            with analytics_read.analytics_connection(connect=_factory):
                raise OperationalError("server closed the connection")
        self.assertEqual(first.close_called, 1)

        with analytics_read.analytics_connection(connect=_factory) as c2:
            self.assertIs(c2, second)
        # `second` is not closed on normal exit (persistent).
        self.assertEqual(second.close_called, 0)

    def test_unrelated_error_does_not_invalidate(self) -> None:
        # A SQL syntax error or programmer mistake is NOT a torn-down
        # socket; the cached connection must survive so subsequent
        # reads on the same thread reuse it.
        analytics_read = self.analytics_read
        conn_obj = _FakeConnection()

        def _factory(_url: str) -> _FakeConnection:
            return conn_obj

        with self.assertRaises(ValueError):
            with analytics_read.analytics_connection(connect=_factory):
                raise ValueError("not a broken socket")
        self.assertEqual(conn_obj.close_called, 0)
        with analytics_read.analytics_connection(connect=_factory) as c2:
            self.assertIs(c2, conn_obj)

    def test_switching_db_url_replaces_cached_connection(self) -> None:
        # The thread-local must be keyed on the resolved URL: if a
        # later `with` block on the same thread asks for a different
        # `db_url=`, the stale socket has to close before a fresh
        # one opens. Otherwise a thread that first read from DB A
        # would silently keep reading from A even after the caller
        # switched to DB B.
        analytics_read = self.analytics_read
        seen: list[str] = []
        first = _FakeConnection()
        second = _FakeConnection()
        sequence = iter([first, second])

        def _factory(url: str) -> _FakeConnection:
            seen.append(url)
            return next(sequence)

        with analytics_read.analytics_connection(
            db_url="postgresql://A/db", connect=_factory,
        ) as c1:
            self.assertIs(c1, first)
        with analytics_read.analytics_connection(
            db_url="postgresql://B/db", connect=_factory,
        ) as c2:
            self.assertIs(c2, second)
        self.assertEqual(seen, ["postgresql://A/db", "postgresql://B/db"])
        # `first` (opened for DB A) was closed when the URL changed.
        self.assertEqual(first.close_called, 1)
        # `second` persists for further reads on DB B.
        self.assertEqual(second.close_called, 0)

    def test_same_url_does_not_reopen(self) -> None:
        # Sanity: re-entering with the same explicit URL on the same
        # thread reuses the cached connection -- the URL-change
        # invalidation must not over-trigger.
        analytics_read = self.analytics_read
        opens: list[str] = []
        cached = _FakeConnection()

        def _factory(url: str) -> _FakeConnection:
            opens.append(url)
            return cached

        with analytics_read.analytics_connection(
            db_url="postgresql://h/db", connect=_factory,
        ):
            pass
        with analytics_read.analytics_connection(
            db_url="postgresql://h/db", connect=_factory,
        ) as c2:
            self.assertIs(c2, cached)
        self.assertEqual(opens, ["postgresql://h/db"])

    def test_persistent_factory_sets_autocommit(self) -> None:
        # The default factory wraps `psycopg.connect(db_url,
        # autocommit=True)` so a long-lived thread-local socket does
        # not leave the session idle in transaction after every
        # SELECT. We stub `psycopg.connect` to capture the kwargs;
        # the real driver does not need to be installed for this
        # test to validate the contract.
        analytics_read = self.analytics_read
        captured: dict[str, Any] = {}

        class _FakePsycopg:
            class OperationalError(Exception):
                pass

            class InterfaceError(Exception):
                pass

            @staticmethod
            def connect(url: str, **kwargs: Any) -> _FakeConnection:
                captured["url"] = url
                captured["kwargs"] = kwargs
                return _FakeConnection()

        with patch.dict(sys.modules, {"psycopg": _FakePsycopg}):
            conn = analytics_read._default_persistent_connect(
                "postgresql://h/db"
            )
        self.assertEqual(captured["url"], "postgresql://h/db")
        self.assertEqual(captured["kwargs"].get("autocommit"), True)
        self.assertIsNotNone(conn)


class IsBrokenConnectionExcTest(unittest.TestCase):
    """The broken-connection detector unwraps `AnalyticsReadError`
    (every driver-level failure goes through `_query` which wraps)
    and matches by class name so a fake without psycopg installed
    can drive the close-and-replace path.
    """

    def test_matches_by_class_name(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})

        class OperationalError(Exception):
            pass

        class InterfaceError(Exception):
            pass

        self.assertTrue(
            analytics_read._is_broken_connection_exc(
                OperationalError("dead")
            )
        )
        self.assertTrue(
            analytics_read._is_broken_connection_exc(
                InterfaceError("dead")
            )
        )

    def test_unwraps_analytics_read_error(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})

        class OperationalError(Exception):
            pass

        wrapper = analytics_read.AnalyticsReadError("wrap")
        wrapper.__cause__ = OperationalError("dead")
        self.assertTrue(
            analytics_read._is_broken_connection_exc(wrapper)
        )

    def test_unrelated_error_is_not_broken(self) -> None:
        _, analytics_read = _reload({"ANALYTICS_DB_URL": "postgresql://h/db"})
        self.assertFalse(
            analytics_read._is_broken_connection_exc(
                ValueError("not a broken socket")
            )
        )


if __name__ == "__main__":
    unittest.main()
