# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Named query-boundary rows for wide analytics SELECT lists."""
from __future__ import annotations

from typing import Any, NamedTuple, Sequence


AgentExitQueryRow = NamedTuple("AgentExitQueryRow", (
    ("ts", Any),
    ("repo", Any),
    ("issue", Any),
    ("stage", Any),
    ("agent_role", Any),
    ("backend", Any),
    ("duration_s", Any),
    ("exit_code", Any),
    ("timed_out", Any),
    ("review_round", Any),
    ("retry_count", Any),
    ("input_tokens", Any),
    ("output_tokens", Any),
    ("cost_usd", Any),
    ("cost_source", Any),
))
IssueSummaryQueryRow = NamedTuple("IssueSummaryQueryRow", (
    ("repo", Any),
    ("issue", Any),
    ("event_count", Any),
    ("first_seen", Any),
    ("last_seen", Any),
    ("latest_stage", Any),
    ("agent_exits", Any),
    ("total_cost_usd", Any),
    ("total_input_tokens", Any),
    ("total_output_tokens", Any),
    ("max_review_round", Any),
    ("failed_agent_runs", Any),
    ("max_retry_count", Any),
))
ReviewRoundQueryRow = NamedTuple("ReviewRoundQueryRow", (
    ("bucket", Any),
    ("runs", Any),
    ("failed", Any),
    ("total_cost_usd", Any),
    ("developer_runs", Any),
    ("reviewer_runs", Any),
    ("developer_cost_usd", Any),
    ("reviewer_cost_usd", Any),
    ("developer_cache_cost_usd", Any),
    ("developer_no_cache_cost_usd", Any),
    ("reviewer_cache_cost_usd", Any),
    ("reviewer_no_cache_cost_usd", Any),
))


def agent_exit_row(row: Sequence[Any]) -> AgentExitQueryRow:
    return AgentExitQueryRow(*row)


def issue_summary_row(row: Sequence[Any]) -> IssueSummaryQueryRow:
    missing = len(IssueSummaryQueryRow._fields) - len(row)
    padded_row = (*row, *((None,) * max(missing, 0)))
    return IssueSummaryQueryRow(*padded_row[:len(IssueSummaryQueryRow._fields)])


def review_round_row(row: Sequence[Any]) -> ReviewRoundQueryRow:
    missing = len(ReviewRoundQueryRow._fields) - len(row)
    padded_row = (*row, *((None,) * max(missing, 0)))
    return ReviewRoundQueryRow(*padded_row[:len(ReviewRoundQueryRow._fields)])
