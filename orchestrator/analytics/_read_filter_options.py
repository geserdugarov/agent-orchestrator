# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Filter-option query and row projection."""

from __future__ import annotations

from typing import Sequence

from orchestrator.analytics.read_models import FilterOptions

_FILTER_OPTION_COLUMNS: tuple[str, ...] = (
    "repo",
    "event",
    "stage",
    "backend",
    "agent_role",
)


def _filter_options_sql() -> str:
    return " UNION ".join(
        f"SELECT '{column}' AS dim, {column} AS value FROM analytics_events WHERE {column} IS NOT NULL"
        for column in _FILTER_OPTION_COLUMNS
    )


def _filter_options_from_rows(rows: Sequence[tuple]) -> FilterOptions:
    buckets: dict[str, list[str]] = {column: [] for column in _FILTER_OPTION_COLUMNS}
    for row in rows:
        if not row or row[1] is None:
            continue
        dimension = row[0]
        bucket = buckets.get(dimension)
        if bucket is not None:
            bucket.append(row[1])
    for option_names in buckets.values():
        option_names.sort()
    return FilterOptions(
        repos=tuple(buckets["repo"]),
        events=tuple(buckets["event"]),
        stages=tuple(buckets["stage"]),
        backends=tuple(buckets["backend"]),
        agent_roles=tuple(buckets["agent_role"]),
    )
