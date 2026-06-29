# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fake psycopg connection / cursor and module-reload helpers
for the analytics read-model test suite.

The `test_analytics_read*.py` modules all reload
`orchestrator.analytics.read` against a hermetic env and drive the
readers through an in-memory `_FakeConnection` / `_FakeCursor` pair,
so the fakes and the `_reload` shim live here in one place.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch


def _hermetic_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
    if extra:
        env.update(extra)
    return env


def _reload(env: dict[str, str] | None = None):
    """Reload `orchestrator.config`, `orchestrator.analytics`, and
    `orchestrator.analytics.read` against the given hermetic env,
    mirroring `test_analytics_sync`.

    The analytics package owns the `ANALYTICS_DB_URL` parsing now,
    and `analytics.read` reads it off the parent package at call
    time, so the parent must be popped alongside `read` for the test
    env to land. `config` is popped too so `analytics.__init__`'s
    `from .. import config` reloads against the patched env (it
    still reads `LOG_DIR` for the JSONL default).
    """
    with patch.dict(os.environ, _hermetic_env(env), clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.analytics.read", None)
        sys.modules.pop("orchestrator.analytics", None)
        import orchestrator.analytics as analytics
        from orchestrator.analytics import read as analytics_read
        return analytics, analytics_read


class _FakeCursor:
    """Records every (sql, params) executed and returns canned rows.

    Implemented as a context manager so the production
    `with conn.cursor() as cur:` block works unchanged. `rows_for`
    is a dict mapping a substring of the SQL to the rows the cursor
    should return -- tests register expected query shapes by their
    most distinctive keyword (`COUNT(*) AS total_events`,
    `date_trunc`, etc.) so a refactor of unrelated SQL doesn't
    accidentally trip the assertion.
    """

    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn
        self._next_rows: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        self._conn.executed.append((sql, tuple(params)))
        if self._conn.raise_on_execute is not None:
            raise self._conn.raise_on_execute
        self._next_rows = []
        for needle, rows in self._conn.rows_for.items():
            if needle in sql:
                self._next_rows = list(rows)
                break

    def fetchall(self) -> list[tuple]:
        return list(self._next_rows)


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.rows_for: dict[str, list[tuple]] = {}
        self.raise_on_execute: Exception | None = None
        self.close_called = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        self.close_called += 1


def _connector(conn: _FakeConnection):
    """Build a `connect(db_url) -> conn` factory that always returns
    the same fake connection, so tests can inspect it after.
    """

    def _connect(_url: str) -> _FakeConnection:
        return conn

    return _connect
