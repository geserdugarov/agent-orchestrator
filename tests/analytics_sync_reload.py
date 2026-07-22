# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Hermetic module and temporary-log setup for analytics sync tests."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from tests.analytics_sync_payloads import write_jsonl, write_raw_lines


LOG_PATH_ENV = "ANALYTICS_LOG_PATH"
DB_URL_ENV = "ANALYTICS_DB_URL"
DB_URL = "postgresql://h/db"
SYNC_MODULE = "orchestrator.analytics.sync"
LOG_FILENAME = "a.jsonl"


def hermetic_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the import-time environment shared by sync tests."""
    environment = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
    if extra:
        environment.update(extra)
    return environment


def reload_sync(
    environment: dict[str, str] | None = None,
) -> tuple[ModuleType, ModuleType]:
    """Reload analytics and sync against one hermetic environment."""
    with patch.dict(os.environ, hermetic_environment(environment), clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.analytics", None)
        sys.modules.pop(SYNC_MODULE, None)
        analytics = importlib.import_module("orchestrator.analytics")
        analytics_sync = importlib.import_module(SYNC_MODULE)
    return analytics, analytics_sync


@contextmanager
def reloaded_sync(
    write_log: Callable[[Path], None],
    *,
    db_url: str = DB_URL,
    filename: str = LOG_FILENAME,
) -> Iterator[tuple[Path, ModuleType]]:
    """Yield a sync module bound to one temporary JSONL log."""
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / filename
        write_log(path)
        _, analytics_sync = reload_sync(
            {LOG_PATH_ENV: str(path), DB_URL_ENV: db_url},
        )
        yield path, analytics_sync


def sync_for_records(records: list[dict], **options):
    """Return a reload context seeded with well-formed records."""
    return reloaded_sync(
        lambda path: write_jsonl(path, records),
        **options,
    )


def sync_for_lines(lines: list[str], **options):
    """Return a reload context seeded with raw JSONL or garbage."""
    return reloaded_sync(
        lambda path: write_raw_lines(path, lines),
        **options,
    )
