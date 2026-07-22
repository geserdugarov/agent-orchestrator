# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed state used while building analytics predicates."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class _WindowFilters:
    """The common window and selection filters accepted by readers."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None
    repo: Optional[str] = None
    events: Optional[Sequence[str]] = None
    stages: Optional[Sequence[str]] = None
    issue: Optional[int] = None

    def without_events(self) -> _WindowFilters:
        """Return filters suitable for a view with no `event` column."""
        return replace(self, events=None)

    def catalog_scope(self) -> _WindowFilters:
        """Return the date/repo subset valid for repo-level catalog rows."""
        return replace(self, events=None, stages=None, issue=None)

    def historical_scope(self) -> _WindowFilters:
        """Return filters for a session's evidence before the window end.

        Drops the ``start`` bound and the ``stages`` / ``events``
        selections while keeping ``end`` / ``repo`` / ``issue``, so a
        logical session's loads from a prior stage or from before the
        reporting window stay visible, yet the ``end`` bound still stops
        later evidence from leaking backward into the aggregate.
        """
        return replace(self, start=None, events=None, stages=None)


@dataclass
class _WhereBuilder:
    """Accumulate one parameterized SQL predicate and its values."""

    conditions: list[str] = field(default_factory=list)
    bindings: list[Any] = field(default_factory=list)

    def add_scalar(
        self,
        column: str,
        operand: Any,
        *,
        operator: str = "=",
    ) -> None:
        if operand is None:
            return
        self.conditions.append(f"{column} {operator} %s")
        self.bindings.append(operand)

    def add_selection(
        self,
        column: str,
        selection: Optional[Sequence[str]],
    ) -> None:
        if selection is None:
            return
        if not selection:
            self.conditions.append("FALSE")
            return
        placeholders = ", ".join("%s" for _ in selection)
        self.conditions.append(f"{column} IN ({placeholders})")
        self.bindings.extend(selection)

    def render(self) -> tuple[str, list[Any]]:
        if not self.conditions:
            return "", self.bindings
        where_clause = " AND ".join(self.conditions)
        return f" WHERE {where_clause}", self.bindings
