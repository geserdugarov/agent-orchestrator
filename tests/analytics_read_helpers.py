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

import importlib
import os
import sys
from unittest.mock import patch

# The stand-in Postgres DSN every read-model test wires into
# `ANALYTICS_DB_URL`; only its presence matters, the fake connection
# never dials it.
_POSTGRES_URL = "postgresql://h/db"
_DB_URL_ENV = "ANALYTICS_DB_URL"


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
        # `import_module` re-imports off `sys.modules`, so popping the
        # entries above forces a fresh load against the patched env; a
        # `from orchestrator import analytics` would instead rebind the
        # stale package attribute and skip the reload.
        analytics = importlib.import_module("orchestrator.analytics")
        analytics_read = importlib.import_module("orchestrator.analytics.read")
        return analytics, analytics_read


def _reload_read(db_url: str = _POSTGRES_URL):
    """Reload only `orchestrator.analytics.read` against `db_url`.

    Most read-model tests never inspect the parent `analytics`
    module, so this folds the `ANALYTICS_DB_URL` wiring behind a
    single default and returns just the reloaded `read` module.
    """
    _, analytics_read = _reload({_DB_URL_ENV: db_url})
    return analytics_read


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
        """No resources to release; the fake never suppresses."""

    def execute(self, sql: str, sql_params: tuple) -> None:
        self._conn.executed.append((sql, tuple(sql_params)))
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

    def as_connect(self, _url: str) -> "_FakeConnection":
        """Serve as the reader's `connect(db_url) -> conn` callable,
        always yielding this same fake so a test can inspect the
        executed SQL after the reader returns.
        """
        return self

    @property
    def first_query(self) -> tuple[str, tuple]:
        """The single (sql, params) round-trip the reader issued."""
        return self.executed[0]
