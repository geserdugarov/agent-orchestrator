# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""JSONL fixture I/O shared by analytics recording tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


ENCODING = "utf-8"


def read_text(path: Path) -> str:
    """Read one UTF-8 fixture file."""
    return path.read_text(encoding=ENCODING)


def read_lines(path: Path) -> list[str]:
    """Read fixture lines without trailing newlines."""
    return read_text(path).splitlines()


def read_records(path: Path) -> list[dict]:
    """Parse nonblank JSONL records when the fixture exists."""
    if not path.exists():
        return []
    return [json.loads(line) for line in read_lines(path) if line.strip()]


def write_json_lines(path: Path, records: list[dict]) -> None:
    """Write one stable JSON object per line, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=ENCODING) as stream:
        for record in records:
            stream.write(f"{json.dumps(record, sort_keys=True)}\n")


def timestamp_days_ago(days: int, *, now: datetime) -> str:
    """Return a second-precision timestamp relative to ``now``."""
    return (now - timedelta(days=days)).isoformat(timespec="seconds")
