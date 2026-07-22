# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Cost and breakdown analytics read result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ReviewRoundBucketRow:
    """Per-review-round development and review cost of agent runs.

    `bucket` is the categorical round string
    (`0`/`1`/`2`/`3`/`4`/`5`/`6+`, plus `unknown` for NULL rounds);
    `get_review_round_breakdown` derives it from the raw
    `review_round` so rounds 3-5 stay separate and only 6+ is grouped.
    It is exposed verbatim so the dashboard chart's labels can map
    each bucket directly. `developer_*` and `reviewer_*` split the
    round's cost into implementation/fix work and automated review
    work; `total_cost_usd` remains their sum for KPI callers. Each
    role's cost is further split into `*_cache_cost_usd` (the portion
    attributable to cached / cache-read / cache-write tokens) and
    `*_no_cache_cost_usd` (the portion attributable to input + output
    tokens). The split is prorated per run by token share so cache +
    no-cache sums back to the role's total cost, letting the dashboard
    chart stack cache vs no-cache spend per round. Rows with
    `review_round IS NULL` surface under the `"unknown"` bucket when
    they are still development/review runs. Historical implementation
    rows that predate fresh-spawn `review_round=0` logging are
    bucketed as `0` so the dashboard does not strand first-pass
    development cost under "unknown".
    """

    bucket: str
    runs: int
    failed: int = 0
    total_cost_usd: float = field(default_factory=float)
    developer_runs: int = 0
    reviewer_runs: int = 0
    developer_cost_usd: float = field(default_factory=float)
    reviewer_cost_usd: float = field(default_factory=float)
    developer_cache_cost_usd: float = field(default_factory=float)
    developer_no_cache_cost_usd: float = field(default_factory=float)
    reviewer_cache_cost_usd: float = field(default_factory=float)
    reviewer_no_cache_cost_usd: float = field(default_factory=float)


@dataclass(frozen=True)
class BackendEfficiencyRow:
    """Per-`backend` aggregate of agent runs.

    Powers the dashboard's "backend efficiency" panel: total runs,
    how many failed, the average wall-clock duration (None when no
    row in the window carried a duration), and the total cost /
    token spend. `total_cache_read_tokens` / `total_cache_write_tokens`
    surface alongside input / output so the "cost / 1M tok" tile
    can divide by the same `input + output + cache` total the rest
    of the redesigned page uses (matching the standalone mock's
    accounting). Rows whose `backend` is NULL bucket under
    `"unknown"` so the chart still surfaces them rather than
    silently dropping a category.
    """

    backend: str
    runs: int
    failed: int = 0
    avg_duration_s: Optional[float] = None
    total_cost_usd: float = field(default_factory=float)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0


@dataclass(frozen=True)
class RepoBreakdownRow:
    """Per-`repo` rollup over the filter window.

    The dashboard's "activity by repo" chart plots issue and event
    counts side-by-side; `agent_exits` and `total_cost_usd` are the
    cost-focused companions. Distinct issue counts use
    `COUNT(DISTINCT issue)` because rows are already scoped to one
    repo per bucket, so the `(repo, issue)` row-constructor used by
    `get_summary` is unnecessary here.
    """

    repo: str
    issues: int
    events: int
    agent_exits: int = 0
    total_cost_usd: float = field(default_factory=float)


@dataclass(frozen=True)
class CostCoverageRow:
    """Per-`cost_source` count and token rollup of agent runs.

    Powers the dashboard's "cost attribution coverage" bar.
    `total_tokens` rolls up the per-`cost_source` token volume so
    the redesigned bar can be sized by token share -- matching the
    standalone mock, which treats coverage as "what fraction of
    token volume the parser could attribute a price to" rather than
    "what fraction of runs". A small number of high-token runs can
    dominate the cost picture, so a run-count share would
    misrepresent how exposed an operator is to pricing-table gaps.
    The `unknown-price` cohort is the maintenance signal for the
    pricing table baked into `orchestrator.usage` -- it is NEVER
    collapsed into a generic "unknown" bucket here so an operator
    can see at a glance how much volume the parser could not price.
    Rows whose `cost_source` is NULL surface under `"unknown"` so
    they remain visible (this is distinct from the `unknown-price`
    string the parser writes -- a NULL is "field absent", not
    "field present with the value 'unknown-price'").
    """

    cost_source: str
    runs: int
    total_tokens: int = 0
