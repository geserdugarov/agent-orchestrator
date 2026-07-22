# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Structured GitHub audit event construction and persistence."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator import config

log = logging.getLogger("orchestrator.github")


def append_event_line(event_path: Path, event_record: dict) -> None:
    """Create the parent directory and append one JSONL event line."""
    event_path.parent.mkdir(parents=True, exist_ok=True)
    with event_path.open("a", encoding="utf-8") as event_stream:
        event_stream.write(f"{json.dumps(event_record, sort_keys=True)}\n")


def write_event_record(event_record: dict) -> None:
    """Append an event when the optional audit path is configured."""
    event_path = config.EVENT_LOG_PATH
    if event_path is None:
        return
    try:
        append_event_line(event_path, event_record)
    except OSError as error:
        log.warning("could not write event log %s: %s", event_path, error)


def build_event_record(
    *,
    repo: str,
    issue_number: int,
    event: str,
    stage: Optional[str] = None,
    **extras: Any,
) -> dict:
    """Build a second-precision UTC audit record without null extras."""
    event_record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": repo,
        "issue": int(issue_number),
        "event": event,
    }
    if stage is not None:
        event_record["stage"] = stage
    for field_name, field_value in extras.items():
        if field_value is not None:
            event_record[field_name] = field_value
    return event_record
