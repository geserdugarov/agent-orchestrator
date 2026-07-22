# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Command-line parser, logging, and summary output for analytics sync."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.analytics._sync_models import SyncResult


def _configure_cli_logging(level: str) -> None:
    """Install a UTC-stamped log formatter on the root logger.

    The CLI also prints a UTC-stamped one-line summary to stdout at
    the end of `main`; pinning the log timestamps to UTC -- with an
    explicit "UTC" suffix in datefmt -- means a mixed stdout / stderr
    stream stays a coherent time-ordered sequence regardless of the
    host's local timezone. Without this, a TZ-skewed host (the
    reviewer hit a TZ+7 machine) prints log lines and the summary
    line hours apart for the same wall-clock event because
    `logging.basicConfig` defaults to local time while
    `datetime.now(timezone.utc)` is UTC.
    """
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S UTC",
    )
    # `gmtime` on this formatter instance only -- mutating
    # `logging.Formatter.converter` globally would change every other
    # formatter's timezone behavior in the same process (the unit
    # tests pull `assertLogs` records through their own formatters,
    # and a process-wide flip would surprise them).
    formatter.converter = time.gmtime

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Replace prior handlers so a re-invocation in the same process
    # (a test that calls `main()` twice, a long-lived shell running
    # `python -m`) actually picks up the new formatter rather than
    # silently no-op'ing the way `basicConfig` does once the root
    # already has a handler.
    for prior_handler in list(root.handlers):
        root.removeHandler(prior_handler)
    root.addHandler(stream_handler)


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.analytics.sync",
        description=(
            "Replay records from ANALYTICS_LOG_PATH into the Postgres "
            "analytics service at ANALYTICS_DB_URL. Deduplicates by "
            "content hash so repeated runs are idempotent. No-op when "
            "either env var is unset or the JSONL file is absent."
        ),
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=None,
        help=("Override ANALYTICS_LOG_PATH for this run. Useful for replaying a rotated / archived JSONL file."),
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help=(
            "Override ANALYTICS_DB_URL for this run. Accepts any libpq "
            "URL so a one-off replay against a different database does "
            "not require touching the environment."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def _print_cli_result(sync_result: SyncResult, cli_start: float) -> None:
    """Print the UTC summary retained even when structured logs are hidden."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    duration_s = sync_result.duration_s or round(time.monotonic() - cli_start, 3)
    sys.stdout.write(
        f"{timestamp} analytics_sync: inserted={sync_result.inserted} "
        f"duplicate={sync_result.skipped_duplicate} "
        f"malformed={sync_result.skipped_malformed} "
        f"total_lines={sync_result.total_lines} "
        f"duration_s={duration_s:.3f}\n"
    )
