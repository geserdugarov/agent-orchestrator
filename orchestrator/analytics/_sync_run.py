# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Resolved analytics sync request and execution lifecycle."""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from orchestrator.analytics._sync_database import (
    _close_quietly,
    _default_connect,
    _default_json_adapter,
    _refresh_daily_rollup,
    _rollback_quietly,
)
from orchestrator.analytics._sync_ingest import _ingest_records
from orchestrator.analytics._sync_models import (
    SyncResult,
    _IngestContext,
    _SyncCounters,
)
from orchestrator.analytics._sync_redaction import _redact_db_url
from orchestrator.analytics._sync_rows import _build_insert_sql

log = logging.getLogger("orchestrator.analytics.sync")


@dataclass(frozen=True)
class _SyncRequest:
    """Resolved source, destination, and injected adapters for one sync."""

    log_path: Optional[Path]
    db_url: Optional[str]
    connect_fn: Callable[[str], Any]
    json_adapter: Callable[[Any], Any]

    @classmethod
    def resolve(
        cls,
        log_path: Optional[Path],
        db_url: Optional[str],
        connect: Optional[Callable[[str], Any]],
        json_adapter: Optional[Callable[[Any], Any]],
    ) -> _SyncRequest:
        analytics_package = sys.modules["orchestrator.analytics"]
        return cls(
            log_path=(analytics_package.ANALYTICS_LOG_PATH if log_path is None else log_path),
            db_url=(analytics_package.ANALYTICS_DB_URL if db_url is None else db_url),
            connect_fn=connect or _default_connect,
            json_adapter=json_adapter or _default_json_adapter,
        )

    def ready(self) -> bool:
        """Log and reject configured no-op paths before any connection work."""
        if self.log_path is None:
            log.info("analytics_sync: ANALYTICS_LOG_PATH not configured; nothing to sync")
            return False
        if not self.db_url:
            log.info("analytics_sync: ANALYTICS_DB_URL not configured; nothing to sync")
            return False
        if not self.log_path.exists():
            log.info(
                "analytics_sync: %s does not exist yet; nothing to sync",
                self.log_path,
            )
            return False
        return True


@dataclass
class _SyncRun:
    """Connection lifecycle, ingest state, and reporting for one replay."""

    request: _SyncRequest
    counters: _SyncCounters = field(default_factory=_SyncCounters)
    start: float = field(default_factory=time.monotonic)

    def connect(self) -> Any:
        redacted_url = _redact_db_url(self.request.db_url or "")
        log.info(
            "analytics_sync: connecting to %s (source=%s)",
            redacted_url,
            self.request.log_path,
        )
        conn = self.request.connect_fn(self.request.db_url)
        log.info(
            "analytics_sync: connection established to %s after %.3fs",
            redacted_url,
            time.monotonic() - self.start,
        )
        return conn

    def ingest_context(self) -> _IngestContext:
        log_path = Path(self.request.log_path)
        return _IngestContext(
            log_path=log_path,
            insert_sql=_build_insert_sql(),
            source_path=str(log_path),
            json_adapter=self.request.json_adapter,
            counters=self.counters,
            start=self.start,
        )

    def commit(self, conn: Any) -> None:
        """Commit rows and refresh the rollup even for duplicate-only runs."""
        log.info(
            "analytics_sync: committing transaction (lines=%d inserted=%d duplicate=%d malformed=%d elapsed=%.3fs)",
            self.counters.total_lines,
            self.counters.inserted,
            self.counters.skipped_duplicate,
            self.counters.skipped_malformed,
            time.monotonic() - self.start,
        )
        conn.commit()
        _refresh_daily_rollup(conn)

    def finalize(self) -> SyncResult:
        duration_s = round(time.monotonic() - self.start, 3)
        log.info(
            "analytics_sync: completed in %.3fs (inserted=%d duplicate=%d malformed=%d total_lines=%d source=%s)",
            duration_s,
            self.counters.inserted,
            self.counters.skipped_duplicate,
            self.counters.skipped_malformed,
            self.counters.total_lines,
            self.request.log_path,
        )
        return SyncResult(
            inserted=self.counters.inserted,
            skipped_duplicate=self.counters.skipped_duplicate,
            skipped_malformed=self.counters.skipped_malformed,
            total_lines=self.counters.total_lines,
            malformed_line_numbers=tuple(self.counters.malformed_lines),
            duration_s=duration_s,
        )

    def execute(self) -> SyncResult:
        conn = self.connect()
        try:
            self._ingest_and_commit(conn)
        except Exception:
            _rollback_quietly(conn, "analytics_sync: rollback failed")
            raise
        finally:
            _close_quietly(conn)
        return self.finalize()

    def _ingest_and_commit(self, conn: Any) -> None:
        _ingest_records(conn, self.ingest_context())
        self.commit(conn)
