# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Combined rollup summary row projection."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from orchestrator.analytics.read_models import Summary

_SUMMARY_TOTAL_FIELD_CASTS = (
    ("total_events", int),
    ("distinct_issues", int),
    ("distinct_repos", int),
    ("total_cost_usd", float),
    ("total_input_tokens", int),
    ("total_output_tokens", int),
    ("total_agent_runs", int),
    ("failed_agent_runs", int),
    ("total_cache_read_tokens", int),
    ("total_cache_write_tokens", int),
    ("timed_out_agent_runs", int),
)


def _summary_totals_row(rows: Sequence[tuple]) -> Optional[tuple]:
    """Return the totals row emitted by the combined query, if present."""
    totals_row: Optional[tuple] = None
    for row in rows:
        if row and row[0] == "t":
            totals_row = row
    return totals_row


def _ordered_summary_counts(
    rows: Sequence[tuple],
    row_kind: str,
) -> dict[str, int]:
    """Convert one breakdown row kind to count-descending order."""
    counts = [
        (row[1], int(row[2] or 0))
        for row in rows
        if row and row[0] == row_kind and row[1] is not None
    ]
    counts.sort(key=_summary_count_order)
    return dict(counts)


def _summary_count_order(pair: tuple[str, int]) -> tuple[int, str]:
    return -pair[1], pair[0]


def _summary_total_values(totals_row: tuple) -> dict[str, Any]:
    """Map the totals columns to typed Summary field values."""
    return {
        field_name: field_cast(raw_value or 0)
        for (field_name, field_cast), raw_value in zip(
            _SUMMARY_TOTAL_FIELD_CASTS,
            totals_row[2:],
        )
    }


def _summary_from_rows(rows: Sequence[tuple]) -> Summary:
    """Convert combined-query rows into the public Summary model."""
    by_event = _ordered_summary_counts(rows, "e")
    by_stage = _ordered_summary_counts(rows, "s")
    totals_row = _summary_totals_row(rows)
    if totals_row is None:
        return Summary(by_event=by_event, by_stage=by_stage)
    return Summary(
        by_event=by_event,
        by_stage=by_stage,
        **_summary_total_values(totals_row),
    )
