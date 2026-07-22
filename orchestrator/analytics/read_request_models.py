# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed values consumed by analytics readers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Sequence


@dataclass(frozen=True)
class ReadFilters:
    """Window and domain filters shared by analytics readers."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    repo: Optional[str] = None
    events: Optional[Sequence[str]] = None
    stages: Optional[Sequence[str]] = None
    issue: Optional[int] = None


@dataclass(frozen=True)
class ReadConnection:
    """Connection selection for one analytics read."""

    db_url: Optional[str] = None
    connect: Optional[Callable[[str], Any]] = None
    conn: Any = None


@dataclass(frozen=True)
class ReadOptions:
    """Reader-specific limit, ordering, and timezone controls."""

    limit: Optional[int] = None
    sort_by: Optional[str] = None
    tz_offset_hours: int = 0


@dataclass(frozen=True)
class ReadRequest:
    """Normalized input consumed by analytics query implementations."""

    filters: ReadFilters
    connection: ReadConnection
    options: ReadOptions
