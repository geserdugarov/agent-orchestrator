# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""KPI and insight calculations for the analytics dashboard.

The pure numeric core behind the redesigned page: the computed
insight banners (`compute_insights`), the KPI delta math
(`kpi_delta`), the reliability-tile triples (`reliability_tile_data`),
the top-cost issue ordering (`top_expensive_issues`), and the rework-
share aggregation (`rework_totals`). These take read-model rows /
`Summary` aggregates and return plain numbers, strings, and small
dataclasses so they stay testable without a live Streamlit run and
free of any rendering or Streamlit dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from orchestrator.analytics.read import (
    CostCoverageRow,
    IssueSummaryRow,
    Summary,
)

DEFAULT_EXPENSIVE_LIMIT = 8

# Insight thresholds.
FAILURE_RATE_BANNER_THRESHOLD = 0.10
UNPRICED_COVERAGE_THRESHOLD = 0.10
UNPRICED_COST_SOURCES: frozenset[str] = frozenset({"unknown-price", "unknown"})
# Bucket strings the review-round breakdown emits whose runs are
# "rework" (i.e. happened after the initial pass). Used to compute the
# rework share KPI. `get_review_round_breakdown` keeps rounds 3, 4 and
# 5 separate (only 6+ is grouped), so every post-initial round is
# listed explicitly here.
REWORK_BUCKETS: frozenset[str] = frozenset(
    {"1", "2", "3", "4", "5", "6+"}
)


@dataclass(frozen=True)
class InsightBanner:
    """A single banner line displayed at the top of the page.

    `severity` is one of `success` / `info` / `warning` / `error`;
    the dashboard renders each through the matching coloured insight
    block. Keeping severity a plain string (rather than an Enum)
    means the helpers stay importable without Streamlit and the
    tests can compare against string literals.
    """

    severity: str
    message: str


def kpi_delta(
    current: float, previous: float
) -> Optional[float]:
    """Relative change vs the previous window.

    Returns `(current - previous) / previous` (e.g. `0.25` = +25%) or
    `None` when `previous` is zero / negative so the dashboard hides
    the delta indicator rather than rendering an infinity. Negative
    `previous` values are not expected in this column set (counts,
    spend, tokens are all non-negative) but the guard keeps the
    helper safe to call from anywhere.
    """
    if previous <= 0:
        return None
    return (current - previous) / previous


def compute_insights(
    summary: Summary,
    *,
    cost_coverage_rows: Sequence[CostCoverageRow] = (),
) -> list[InsightBanner]:
    """Banner lines surfaced at the top of the redesigned page.

    Each banner is a single observation the operator should act on:

    - Failure rate exceeds `FAILURE_RATE_BANNER_THRESHOLD`: agent
      runs are exiting non-zero more than 10 % of the time.
    - Unpriced cost coverage exceeds `UNPRICED_COVERAGE_THRESHOLD`:
      the pricing table in `orchestrator.usage` is missing SKUs the
      parser is seeing in the wild.

    The helper returns an empty list when nothing crosses a
    threshold, so the caller can branch on `if banners:` for the
    section header.
    """
    banners: list[InsightBanner] = []
    if summary.total_agent_runs > 0:
        rate = summary.failed_agent_runs / summary.total_agent_runs
        if rate >= FAILURE_RATE_BANNER_THRESHOLD:
            banners.append(
                InsightBanner(
                    severity="error",
                    message=(
                        f"{summary.failed_agent_runs} of "
                        f"{summary.total_agent_runs} agent runs failed "
                        f"({rate * 100:.0f}%)."
                    ),
                )
            )
    if cost_coverage_rows:
        total_runs = sum(r.runs for r in cost_coverage_rows)
        unpriced = sum(
            r.runs
            for r in cost_coverage_rows
            if r.cost_source in UNPRICED_COST_SOURCES
        )
        if total_runs > 0:
            ratio = unpriced / total_runs
            if ratio >= UNPRICED_COVERAGE_THRESHOLD:
                banners.append(
                    InsightBanner(
                        severity="warning",
                        message=(
                            f"{unpriced} of {total_runs} agent runs lack "
                            f"a priced cost ({ratio * 100:.0f}%) -- check "
                            "the pricing table in `orchestrator.usage` "
                            "for missing SKUs."
                        ),
                    )
                )
    return banners


def reliability_tile_data(
    summary: Summary,
    *,
    resolved: int = 0,
    rejected: int = 0,
) -> list[tuple[int, str, str]]:
    """`(value, label, tone)` triples for the six reliability tiles.

    Extracted from `main()` so the wiring stays testable without a
    live Streamlit run: every tile sources its number from a
    full-window aggregate on `Summary` (`total_agent_runs`,
    `failed_agent_runs`, `timed_out_agent_runs`) so a long window
    with more than `DEFAULT_RECENT_AGENT_EXITS` rows never silently
    undercounts the tile -- earlier drafts read timeouts off
    `get_recent_agent_exits` and missed any timeout outside the
    latest 100 rows.

    `resolved` / `rejected` are the per-day rollups summed by the
    caller from `get_throughput_breakdown`; they default to zero so
    callers that only care about the agent-run tiles can ignore the
    throughput axis.

    Tones (`"good"` / `"warn"` / `"bad"` / `""`) drive the CSS class
    applied to the tile; the caller never has to recompute them.
    """
    total_runs = int(summary.total_agent_runs or 0)
    failed = int(summary.failed_agent_runs or 0)
    timed_out = int(summary.timed_out_agent_runs or 0)
    success_pct = (
        (1.0 - failed / total_runs) * 100
        if total_runs > 0 else 0.0
    )
    return [
        (total_runs, "Agent runs", ""),
        (f"{success_pct:.0f}%", "Success rate", "good"),
        (int(resolved), "Resolved", "good"),
        (int(rejected), "Rejected", "warn" if rejected else ""),
        (failed, "Failures", "warn" if failed else ""),
        (timed_out, "Timeouts", "bad" if timed_out else ""),
    ]


def top_expensive_issues(
    rows: Sequence[IssueSummaryRow],
    *,
    limit: int = DEFAULT_EXPENSIVE_LIMIT,
) -> list[IssueSummaryRow]:
    """Issues sorted by total cost desc for the "where did spend go" table."""
    if limit <= 0:
        return []

    def _key(r: IssueSummaryRow) -> tuple:
        cost = r.total_cost_usd if r.total_cost_usd is not None else -1.0
        return (-cost, -int(r.event_count), r.repo, int(r.issue))

    return sorted(rows, key=_key)[:limit]


def rework_totals(
    rows: Sequence[Any],
) -> tuple[float, float]:
    """Return `(total_cost, rework_cost)` across review-round buckets.

    `rework_cost` sums the cost of every row whose `bucket` is in
    `REWORK_BUCKETS` (i.e. review round >= 1). `total_cost` sums
    every row, including the initial pass. Cost defaults to `0.0`
    when the row predates the `total_cost_usd` column.
    """
    total = sum(
        float(getattr(r, "total_cost_usd", 0.0) or 0.0) for r in rows
    )
    rework = sum(
        float(getattr(r, "total_cost_usd", 0.0) or 0.0)
        for r in rows
        if r.bucket in REWORK_BUCKETS
    )
    return total, rework
