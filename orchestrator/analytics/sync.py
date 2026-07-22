# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""JSONL-to-Postgres analytics sync facade and CLI entry point."""

from __future__ import annotations

import argparse
import importlib
import logging
import pathlib
import sys
import time
import typing

_deps = importlib.import_module("orchestrator.analytics._sync_dependencies")


SyncResult = _deps._sync_models.SyncResult
_SyncCounters = _deps._sync_models._SyncCounters
_IngestContext = _deps._sync_models._IngestContext
_redacted_netloc = _deps._sync_redaction._redacted_netloc
_redacted_query = _deps._sync_redaction._redacted_query
_redact_db_url = _deps._sync_redaction._redact_db_url
_default_connect = _deps._sync_database._default_connect
_default_json_adapter = _deps._sync_database._default_json_adapter
_rollback_quietly = _deps._sync_database._rollback_quietly
_close_quietly = _deps._sync_database._close_quietly
_execute_rollup_refresh = _deps._sync_database._execute_rollup_refresh
_refresh_daily_rollup = _deps._sync_database._refresh_daily_rollup
_emit_progress = _deps._sync_ingest._emit_progress
_flush_batch = _deps._sync_ingest._flush_batch
_note_malformed_line = _deps._sync_ingest._note_malformed_line
_existing_hashes = _deps._sync_ingest._existing_hashes
_RecordIngester = _deps._sync_ingest._RecordIngester
_stream_records = _deps._sync_ingest._stream_records
_ingest_records = _deps._sync_ingest._ingest_records
_build_insert_sql = _deps._sync_rows._build_insert_sql
_prepare_record = _deps._sync_rows._prepare_record
_row_values = _deps._sync_rows._row_values
_RowProvenance = _deps._sync_rows._RowProvenance
_SyncRequest = _deps._sync_run._SyncRequest
_SyncRun = _deps._sync_run._SyncRun
_configure_cli_logging = _deps._sync_cli._configure_cli_logging
_cli_parser = _deps._sync_cli._cli_parser
_print_cli_result = _deps._sync_cli._print_cli_result
_BATCH_SIZE = _deps._sync_ingest._BATCH_SIZE
_PROGRESS_INTERVAL = _BATCH_SIZE
_DAILY_ROLLUP_VIEW = _deps._sync_database._DAILY_ROLLUP_VIEW

log = logging.getLogger(__name__)


def sync_jsonl_to_postgres(
    *,
    log_path: typing.Optional[pathlib.Path] = None,
    db_url: typing.Optional[str] = None,
    connect: typing.Optional[typing.Callable[[str], typing.Any]] = None,
    json_adapter: typing.Optional[typing.Callable[[typing.Any], typing.Any]] = None,
) -> SyncResult:
    """Replay the configured analytics JSONL records into Postgres."""
    request = _SyncRequest.resolve(
        log_path,
        db_url,
        connect,
        json_adapter,
    )
    if not request.ready():
        return SyncResult()
    return _SyncRun(request).execute()


def _run_cli(args: argparse.Namespace) -> int:
    cli_start = time.monotonic()
    try:
        sync_result = sync_jsonl_to_postgres(
            log_path=args.log_path,
            db_url=args.db_url,
        )
    except Exception:
        log.exception(
            "analytics_sync: failed after %.3fs",
            time.monotonic() - cli_start,
        )
        return 1
    _print_cli_result(sync_result, cli_start)
    return 0


def main(argv: typing.Optional[list[str]] = None) -> int:
    args = _cli_parser().parse_args(argv)
    _configure_cli_logging(args.log_level)
    return _run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
