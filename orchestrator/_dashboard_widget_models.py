# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable state passed through the dashboard widget pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from orchestrator.analytics.read import DataExtent, Summary
from orchestrator.dashboard_reads import _DashboardReadPlan
from orchestrator.dashboard_state import DateWindow


@dataclass(frozen=True)
class _DashboardModules:
    st: Any
    pd: Any
    charts: Any
    theme: Any


@dataclass(frozen=True)
class _DashboardFilters:
    window: DateWindow
    repo: Optional[str]
    issue_input: Optional[int]
    events: Optional[Sequence[str]]
    stages: Optional[Sequence[str]]

    @property
    def issue(self) -> Optional[int]:
        if self.repo is None:
            return None
        return self.issue_input

    @property
    def days(self) -> int:
        return max((self.window.end - self.window.start).days, 1)


@dataclass(frozen=True)
class _DashboardControls:
    filters: _DashboardFilters
    topbar_slot: Any
    meta_slot: Any
    timezone_offset: int


@dataclass(frozen=True)
class _DashboardPage:
    extent: DataExtent
    controls: _DashboardControls
    reads: _DashboardReadPlan


@dataclass(frozen=True)
class _DashboardKpis:
    tiles: Sequence[dict[str, Any]]
    resolved: int
    rejected: int


@dataclass(frozen=True)
class _LoadedDashboard:
    read_results: dict[str, Any]
    kpis: _DashboardKpis


@dataclass(frozen=True)
class _ReliabilityPanelData:
    repos: Sequence[Any]
    summary: Summary
    throughput: Sequence[Any]
    window: DateWindow
    resolved: int
    rejected: int
