# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Normalized and compatibility filter shapes for trajectory reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, TypedDict


class RunFilterOptionFields(TypedDict, total=False):
    repo: Optional[str]
    backends: Optional[Sequence[str]]
    agent_roles: Optional[Sequence[str]]
    stages: Optional[Sequence[str]]
    issue: Optional[int]
    query: Optional[str]
    exclude_fixtures: bool


@dataclass(frozen=True)
class RunFilters:
    repo: Optional[str]
    backends: Optional[frozenset[str]]
    agent_roles: Optional[frozenset[str]]
    stages: Optional[frozenset[str]]
    issue: Optional[int]
    query: Optional[str]
    exclude_fixtures: bool
