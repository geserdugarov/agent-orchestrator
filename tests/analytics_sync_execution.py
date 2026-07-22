# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Sync execution, log capture, and SQL projections for tests."""

from __future__ import annotations

import contextlib
import logging
import unittest
from types import ModuleType

from tests.analytics_sync_fakes import FakeConnection


SYNC_MODULE = "orchestrator.analytics.sync"
LOG_LEVEL_INFO = "INFO"
REFRESH_STATEMENT = "REFRESH MATERIALIZED VIEW"


def passthrough_json(payload):
    """Keep fake-connection JSON values as plain Python objects."""
    return payload


def run_sync(
    analytics_sync: ModuleType,
    connection: FakeConnection,
    **options,
):
    """Run sync through the standard fake connection boundary."""
    return analytics_sync.sync_jsonl_to_postgres(
        connect=connection.as_connect,
        json_adapter=passthrough_json,
        **options,
    )


def sync_capturing_logs(
    test_case: unittest.TestCase,
    analytics_sync: ModuleType,
    connection: FakeConnection,
    **options,
):
    """Return a sync result and the populated INFO log capture."""
    with contextlib.ExitStack() as cleanup:
        captured = cleanup.enter_context(
            test_case.assertLogs(SYNC_MODULE, level=LOG_LEVEL_INFO),
        )
        sync_result = run_sync(analytics_sync, connection, **options)
    return sync_result, list(captured.output)


def reset_root_logger() -> None:
    """Remove CLI-test handlers from the process root logger."""
    root_logger = logging.getLogger()
    for stale_handler in list(root_logger.handlers):
        root_logger.removeHandler(stale_handler)


def select_sqls(connection: FakeConnection) -> list[str]:
    """Return only startup SELECT statements recorded by the fake."""
    return [
        sql
        for sql, _ in connection.select_calls
        if sql.lstrip().upper().startswith("SELECT")
    ]


def refresh_sqls(connection: FakeConnection) -> list[str]:
    """Return daily-rollup refresh statements recorded by the fake."""
    return [sql for sql, _ in connection.select_calls if REFRESH_STATEMENT in sql]
