# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard timezone, issue, stage, and cache-key normalization."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional, Sequence

from orchestrator._dashboard_windows import DateWindow


class DashboardCacheKey(NamedTuple):
    start: datetime
    end: datetime
    repo: Optional[str]
    events: Optional[tuple[str, ...]]
    stages: Optional[tuple[str, ...]]
    issue: Optional[int]


def format_tz_offset(hours: int) -> str:
    if hours == 0:
        return "UTC"
    sign = "+" if hours > 0 else "-"
    return f"UTC{sign}{abs(int(hours))}"


def shift_ts(timestamp: Any, offset: timedelta) -> Any:
    if timestamp is None:
        return None
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            return timestamp + offset
        return timestamp.astimezone(timezone(offset))
    return timestamp


def parse_issue_number(raw_issue: str) -> Optional[int]:
    if not raw_issue:
        return None
    cleaned = raw_issue.strip().lstrip("#").strip()
    if not cleaned:
        return None
    try:
        issue_number = int(cleaned)
    except ValueError:
        return None
    return issue_number if issue_number > 0 else None


def resolve_stage_filter(
    selected: Sequence[str],
    available: Sequence[str],
) -> Optional[list[str]]:
    if not available or set(selected) == set(available):
        return None
    return list(selected)


def cache_key(
    window: DateWindow,
    repo: Optional[str],
    events: Optional[Sequence[str]],
    stages: Optional[Sequence[str]],
    issue: Optional[int],
) -> DashboardCacheKey:
    event_names = None if events is None else tuple(events)
    stage_names = None if stages is None else tuple(stages)
    return DashboardCacheKey(
        start=window.start,
        end=window.end,
        repo=repo,
        events=event_names,
        stages=stage_names,
        issue=issue,
    )
