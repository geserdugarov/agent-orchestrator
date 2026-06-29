# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Frozen read-model dataclasses returned by `analytics.read`.

These are the plain-Python shapes the dashboard renders -- filter
dropdown options, the data extent, date-bounded summary counts, daily
time-series cells, the stage / event / repo / backend / cost-source
breakdowns, the recent agent-exit rows, per-issue traces, review-round
buckets, the per-backend daily token series, the weekday x hour
heatmap, and the throughput-per-day counts. They carry no query logic:
`analytics.read` builds the SQL and constructs these, and
`orchestrator/dashboard.py` consumes them. Fields default so the
"DB unset" / empty-window paths can return a zero-valued instance
without the caller branching on missing columns, and so fixtures that
emit shorter tuples still round-trip.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass(frozen=True)
class FilterOptions:
    """Distinct values for the dashboard filter dropdowns.

    Tuples (not lists) so the result is hashable and obviously
    immutable to callers that cache it. Empty tuples are the
    documented "DB unset" and "empty table" result -- the dashboard
    should render a disabled filter rather than crash.
    """

    repos: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    agent_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataExtent:
    """Earliest and latest event timestamps in the table.

    The dashboard uses this to default the sidebar date picker to a
    window that actually contains data -- a freshly-deployed database
    has no rows, so picking today's date returns nothing. Both fields
    are `None` when the table is empty or when `ANALYTICS_DB_URL` is
    unset; the dashboard branches on that to render a "no data yet"
    state.
    """

    min_ts: Optional[datetime] = None
    max_ts: Optional[datetime] = None


@dataclass(frozen=True)
class Summary:
    """Aggregate counts for a date-bounded window.

    Zero-valued by default so the "DB unset" path can return
    ``Summary()`` and the dashboard renders a still-meaningful page.
    `by_event` and `by_stage` use plain dicts because Streamlit-style
    rendering iterates them; ordering follows the SQL `GROUP BY` so
    the dashboard sees stable counts even if the rows reshuffle
    between queries. `total_agent_runs` / `failed_agent_runs` count
    `event = 'agent_exit'` rows (and the failing subset where
    `exit_code <> 0`) inside the same filtered window so the
    dashboard's "agent success rate" reads off the same query as the
    rest of the overview. `total_cache_read_tokens` /
    `total_cache_write_tokens` carry the cache-band tokens the
    redesigned dashboard's "Total tokens" KPI and sparkline include
    in the headline figure (the standalone mock's total is
    ``input + output + cache_read + cache_write``).
    """

    total_events: int = 0
    distinct_issues: int = 0
    distinct_repos: int = 0
    by_event: dict[str, int] = field(default_factory=dict)
    by_stage: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_agent_runs: int = 0
    failed_agent_runs: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    # Window-wide timeout count -- agent_exit rows whose `timed_out`
    # flag is true. Sourced from the totals query so the redesigned
    # reliability "Timeouts" tile sees every timed-out run in the
    # window, not just the latest N from `get_recent_agent_exits`.
    timed_out_agent_runs: int = 0


@dataclass(frozen=True)
class TimeSeriesPoint:
    """One (day, event, count) cell of the daily time-series.

    `day` is a `date`, not a `datetime`, because the SQL aggregates
    over `date_trunc('day', ts)` and a date matches a Plotly chart's
    axis directly. The cell carries the per-event cost / token
    aggregates as well so a "spend over time" chart can pivot off the
    same query the activity chart uses -- avoids a second round trip
    for what is already grouped by `(day, event)`. Cache-band tokens
    surface alongside input / output so the redesigned hero chart's
    `mode="type"` stack can render an Input / Output / Cache stack
    instead of dropping cache tokens on the floor. Fields default to
    zero so a fake-cursor fixture that returns just `(day, event,
    count)` rows still validates the no-aggregate path.
    """

    day: date
    event: str
    count: int
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class StageBreakdown:
    """Per-`stage` aggregate row for the stage breakdown table.

    `count` is `COUNT(*)` over every `analytics_events` row that
    carries the stage (so it includes `stage_enter` and
    `stage_evaluation` rows alongside `agent_exit`); `runs` narrows
    to the `event = 'agent_exit'` subset so the redesigned
    dashboard's "Cost by workflow stage" panel can label its
    sub-line as "runs" -- the standalone mock aggregates from
    per-agent-run records, not per-event rows.

    `avg_duration_s` is None when no row in the window had a
    non-null `duration_s` for that stage; the SQL `AVG(...)` returns
    NULL in that case rather than 0 so the dashboard can hide the
    column instead of showing a misleading zero. `total_cost_usd` /
    `total_input_tokens` / `total_output_tokens` roll up the cost /
    token figures across the stage so the breakdown table can plot
    "where the spend went". `cache_cost_usd` and `no_cache_cost_usd`
    split `total_cost_usd` into the portion attributable to cached /
    cache-read / cache-write tokens vs the portion attributable to
    input + output tokens. The split is prorated per rollup row by
    token share so cache + no-cache sums back to the stage's total
    cost, letting the dashboard chart stack cache vs no-cache spend
    per stage. Zero-defaulted so a fake fixture without the run /
    cost / token / cache-split columns still round-trips.
    """

    stage: str
    count: int
    avg_duration_s: Optional[float] = None
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    runs: int = 0
    cache_cost_usd: float = 0.0
    no_cache_cost_usd: float = 0.0


@dataclass(frozen=True)
class EventBreakdown:
    """Per-`event` aggregate row for the event breakdown table."""

    event: str
    count: int


@dataclass(frozen=True)
class AgentExitRow:
    """One row of the recent-agent-exits overview table.

    Mirrors the columns the dashboard table renders -- intentionally a
    subset of the table, not every column. Adding a column should
    happen in lockstep with the SELECT list in `get_recent_agent_exits`
    so the positional unpack stays aligned.
    """

    ts: datetime
    repo: str
    issue: int
    stage: Optional[str]
    agent_role: Optional[str]
    backend: Optional[str]
    duration_s: Optional[float]
    exit_code: Optional[int]
    timed_out: Optional[bool]
    review_round: Optional[int]
    retry_count: Optional[int]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cost_usd: Optional[float]
    cost_source: Optional[str]


@dataclass(frozen=True)
class IssueSummaryRow:
    """One row of the date/repo-bounded issues overview table.

    The dashboard's "issues" view shows one row per `(repo, issue)`
    pair seen in the window with light aggregates: how many events
    fired, when the issue was first / last touched, the most recent
    non-null `stage` (useful as a "current status" column even though
    pinned GitHub state remains authoritative), how many `agent_exit`
    events were recorded, the rolled-up cost / token totals, the
    highest review round any agent run for the issue reached, how
    many of those runs exited non-zero so the table can surface
    issues that needed multiple attempts, and the highest
    `retry_count` any agent run for the issue reached so the
    redesigned "Most expensive issues" table can carry a "Retries"
    column matching the standalone mock. Stable column order across
    the SELECT list, the dataclass, and the positional unpack in
    `get_issues` keeps the schema obvious when a future column is
    added.
    """

    repo: str
    issue: int
    event_count: int
    first_seen: datetime
    last_seen: datetime
    latest_stage: Optional[str]
    agent_exits: int
    total_cost_usd: Optional[float]
    total_input_tokens: int
    total_output_tokens: int
    max_review_round: Optional[int] = None
    failed_agent_runs: int = 0
    max_retry_count: Optional[int] = None


@dataclass(frozen=True)
class IssueEventRow:
    """One row of the per-issue event trace.

    Slim: only the columns useful for the per-issue drill-down view.
    The dashboard can join back to `analytics_events` for the
    forensic columns (`source_path`, `source_line`, `extras`) if a
    debug view needs them later.
    """

    ts: datetime
    event: str
    stage: Optional[str]
    duration_s: Optional[float]
    result: Optional[str]
    agent_role: Optional[str]
    backend: Optional[str]
    exit_code: Optional[int]
    cost_usd: Optional[float]


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
    total_cost_usd: float = 0.0
    developer_runs: int = 0
    reviewer_runs: int = 0
    developer_cost_usd: float = 0.0
    reviewer_cost_usd: float = 0.0
    developer_cache_cost_usd: float = 0.0
    developer_no_cache_cost_usd: float = 0.0
    reviewer_cache_cost_usd: float = 0.0
    reviewer_no_cache_cost_usd: float = 0.0


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
    total_cost_usd: float = 0.0
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
    total_cost_usd: float = 0.0


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


@dataclass(frozen=True)
class SkillTriggerRateRow:
    """Per-`(agent_role, backend)` skill-trigger aggregate over agent runs.

    Powers the dashboard's opt-in "Skill trigger rates" panel. The
    skill fields live in `analytics_events.extras` JSONB -- they are
    not promoted columns and the daily rollup does not carry them --
    so this reader scans the base table directly (no DDL, no view
    change). `runs` is every `agent_exit` row in the group; `skill_runs`
    is how many of those carried a `skills_triggered` key (the firm
    "the stream surfaced at least one skill" signal); `total_triggers`
    sums `skills_triggered_count` so a run that pulled `develop` three
    times weighs more than one clean trigger.

    `record_agent_exit` only writes the skill keys when
    `TRACK_SKILL_TRIGGERS` is on *and* a skill fired (an empty field is
    dropped, not written), so `skill_runs` is a *floor* on observed
    skill use: a `0` rate conflates a run that triggered nothing with
    one whose tracking was off. The dashboard captions the panel
    accordingly. NULL `agent_role` / `backend` bucket under `"unknown"`
    so a category is never silently dropped.
    """

    agent_role: str
    backend: str
    runs: int
    skill_runs: int = 0
    total_triggers: int = 0

    @property
    def rate(self) -> float:
        """Share of runs in the group that triggered >=1 skill (0.0-1.0).

        Returns `0.0` for a zero-run group so callers never divide by
        zero; the reader only emits rows for groups with at least one
        `agent_exit` run, so the guard is defensive.
        """
        return self.skill_runs / self.runs if self.runs else 0.0


@dataclass(frozen=True)
class SkillTriggerMatrixRow:
    """One `(repo, skill, agent_role, backend)` cell of the trigger matrix.

    Powers the dashboard's opt-in per-skill trigger matrix.
    `get_skill_trigger_matrix` combines the repo's `repo_skill_catalog`
    records (the universe of skills a repo offers, from the
    `skills_available` array) with the filtered `agent_exit` rows (the
    runs that actually fired a skill, from the `skills_triggered` array)
    -- both live in `analytics_events.extras` JSONB, so the reader scans
    the base table with no DDL and no rollup change.

    `skill_runs` counts how many runs in the cell *contained* the skill
    (one per run per distinct name in its `skills_triggered` list), not
    the total number of invocations -- a run that pulled `develop` three
    times still weighs one here. A cell with `skill_runs == 0` is a real
    "offered but never triggered" signal: the skill is in the repo's
    catalog and the `(agent_role, backend)` cohort ran in the window,
    but no such run reached for it (e.g. `developer / claude / review =
    0`). When the catalog records are missing the matrix degrades to
    just the observed-trigger cells -- no zero rows are invented.

    `runs` is the total number of `agent_exit` runs in the cell's
    `(repo, agent_role, backend)` cohort (every run, whether or not it
    fired this skill), so a low `skill_runs` reads against the cohort
    size rather than in a vacuum. It is always `>= skill_runs` and,
    because a cell only exists for a cohort that actually ran, always
    `>= 1`. This mirrors `SkillTriggerRateRow.runs` / `.skill_runs`.

    `agent_role` / `backend` bucket NULLs under `"unknown"` so a cohort
    is never silently dropped. The same `TRACK_SKILL_TRIGGERS`-off
    caveat as `SkillTriggerRateRow` applies: a `0` cannot distinguish a
    tracked-but-quiet run from one whose tracking was off.
    """

    repo: str
    skill: str
    agent_role: str
    backend: str
    runs: int = 0
    skill_runs: int = 0


@dataclass(frozen=True)
class BackendDailyTokensRow:
    """One `(day, backend, total_tokens)` cell of the per-backend daily
    token series.

    Powers the redesigned dashboard's "By backend" toggle on the hero
    spend & token usage chart. Reading off `analytics_agent_runs` (a
    view over `event = 'agent_exit'` rows) means the chart never
    silently caps at the `get_recent_agent_exits` `LIMIT` -- every
    backend's tokens get counted across the full window, in lockstep
    with the cost line and KPI aggregates.
    """

    day: date
    backend: str
    total_tokens: int


@dataclass(frozen=True)
class HourlyHeatmapPoint:
    """One (weekday, hour, count, total_tokens) cell of the 7x24
    activity matrix.

    `weekday` follows Postgres `EXTRACT(DOW)` which is 0=Sunday;
    the dashboard chart re-orders to a Monday-first layout if the
    operator prefers (we expose the raw value so the chart layer
    owns the presentation choice). `hour` is the hour of day in
    the same timezone the database stores `ts` in (the orchestrator
    writes UTC). `count` is the per-cell event count; `total_tokens`
    is the matching `input + output + cache_read + cache_write`
    token volume so the redesigned dashboard's "When agents run"
    heatmap can render token intensity (matching the standalone
    mock) rather than event intensity, which would over-weight the
    cheap `stage_enter` / `stage_evaluation` cells against the
    `agent_exit` rows that actually drive spend.
    """

    weekday: int
    hour: int
    count: int
    total_tokens: int = 0


@dataclass(frozen=True)
class ThroughputDayRow:
    """One day's resolved / rejected throughput count.

    Powers the dashboard's "issues resolved per day" chart: counts
    `stage_enter` rows whose `stage` is `done` (resolved) or
    `rejected` (closed without merge), grouped by day. The two
    columns are reported side by side so the chart can stack /
    group them without a second query.
    """

    day: date
    resolved: int = 0
    rejected: int = 0
