# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Immutable trajectory-viewer page and filter state."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from orchestrator import trajectory_reader


@dataclass(frozen=True)
class _TrajectoryFilters:
    repo: Optional[str]
    backends: Optional[Sequence[str]]
    agent_roles: Optional[Sequence[str]]
    stages: Optional[Sequence[str]]
    issue: Optional[int]
    query: str
    hide_fixtures: bool


@dataclass(frozen=True)
class _TrajectoryPage:
    log_path: Optional[Path]
    runs: Sequence[trajectory_reader.TrajectoryRun]
    options: trajectory_reader.FilterOptions
    fixture_total: int

    @property
    def total(self) -> int:
        return len(self.runs)
