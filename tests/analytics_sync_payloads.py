# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""JSONL payload builders for analytics sync tests."""

from __future__ import annotations

import json
from pathlib import Path


SAMPLE_TIMESTAMP = "2026-05-25T12:00:00+00:00"
STAGE_ENTER = "stage_enter"
ISSUE_KEY = "issue"
ENCODING = "utf-8"


def write_jsonl(path: Path, records: list[dict]) -> None:
    """Write records with the analytics sink's canonical encoding."""
    write_raw_lines(
        path,
        [json.dumps(record, sort_keys=True) for record in records],
    )


def write_raw_lines(path: Path, lines: list[str]) -> None:
    """Write pre-rendered JSONL rows and deliberate malformed lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=ENCODING) as stream:
        for line in lines:
            stream.write(line)
            stream.write("\n")


def sample_record(
    *,
    issue: int = 1,
    event: str = STAGE_ENTER,
    ts: str = SAMPLE_TIMESTAMP,
    **extras,
) -> dict:
    """Build the minimal persisted analytics event envelope."""
    record = {
        "ts": ts,
        "repo": "owner/repo",
        ISSUE_KEY: issue,
        "event": event,
    }
    record.update(extras)
    return record


def sample_records(count: int) -> list[dict]:
    """Build ``count`` events with stable one-based issue numbers."""
    return [sample_record(issue=issue) for issue in range(1, count + 1)]


def record_line(**overrides) -> str:
    """Render one canonical sample record as a JSONL line."""
    return json.dumps(sample_record(**overrides), sort_keys=True)
