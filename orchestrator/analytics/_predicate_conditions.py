# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Required-condition and event-filter predicate decisions."""

from __future__ import annotations

from typing import Optional, Sequence


def _append_where_condition(where: str, condition: str) -> str:
    """Add a required condition after an optional generated predicate."""
    if where:
        return f"{where} AND {condition}"
    return f" WHERE {condition}"


def _prepend_where_condition(where: str, condition: str) -> str:
    """Add a required condition before an optional generated predicate."""
    if where:
        return f" WHERE {condition} AND {where.removeprefix(' WHERE ')}"
    return f" WHERE {condition}"


def _agent_event_excluded(events: Optional[Sequence[str]]) -> bool:
    """True when the active event filter excludes `agent_exit` rows.

    Functions that query `analytics_agent_runs` cannot push an
    `event IN (...)` clause down into the SQL (the view has no
    `event` column -- it filters internally to `event='agent_exit'`).
    They preserve the dashboard's event-filter contract by calling
    this helper up front and short-circuiting to an empty result:

    - ``None`` -> not excluded (no event filter at all).
    - non-empty sequence that lacks ``"agent_exit"`` -> excluded.
    - empty sequence (the cleared-multiselect signal) -> excluded.

    Keeps the agent-run aggregates in lockstep with `get_summary`
    et al. when the operator clears or narrows the events filter.
    """
    if events is None:
        return False
    if not events:
        return True
    return "agent_exit" not in events
