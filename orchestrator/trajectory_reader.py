# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pure read model over the opt-in trajectory sink (`TRAJECTORY_LOG_PATH`).

Streamlit-free, import-light counterpart to `orchestrator.analytics.read`:
where that module queries the analytics Postgres, this one reads the local
JSONL file the trajectory sink appends to and shapes its `agent_trajectory`
records for the viewer page in `orchestrator.trajectory_dashboard`. The
trajectory sink is deliberately never replayed into Postgres (see
`docs/observability.md`), so the JSONL file is the only source for this data.

The record and view dataclasses (`TrajectoryRun` and its `TrajectoryStepView`
/ `TimelineEntry` / `TurnUsageView` / `RunUsageView` sub-views), the log-path
resolution, and the defensive JSONL parsing / reading pipeline live in the
private `orchestrator._trajectory_records` leaf and are re-exported here under
their original names; this module owns the free-text filtering and the
filter-option / summary aggregation over the parsed runs. Both halves are
import-light -- only stdlib plus `orchestrator.analytics` (for the
`TRAJECTORY_LOG_PATH` module attribute) -- so importing either never pulls
Streamlit into the polling tick's import surface. The page module owns the
Streamlit rendering.

Each run also carries the normalized per-run timeline (which folds an old
steps-only record and a new record with interleaved text turns into one
ordered prompt -> steps -> output sequence), the run- and per-turn usage views
(the denormalized `run_usage` summary plus claude's per-turn `turns`, with
`usage_for_turn` and the `cost_usd` / `total_tokens` / `model` convenience
accessors), and the synthetic-fixture predicate (which flags the test-suite
records an inherited file may carry) -- all of which are pure and unit-tested.

Resilience contract mirrors the rest of the codebase: a missing file, a
malformed line, a record that is not an `agent_trajectory`, or a renamed /
absent field yields a smaller result, never an exception. Records the sink
already redacted and truncated are surfaced verbatim -- the viewer is a
read-only window onto an already-sanitised file.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import (
    Callable,
    Optional,
    Sequence,
    TypedDict,
    Unpack,
)

# Evict any cached read leaf before importing it so every (re)import of this
# facade builds a fresh `_trajectory_records` bound to the `orchestrator.analytics`
# instance current at import time. A caller that reloads `orchestrator.analytics`
# and `orchestrator.trajectory_reader` together (the A/B env-isolation pattern)
# does not know to also pop this private leaf; without the eviction the fresh
# reader would re-export the stale leaf whose `resolve_log_path` still reads the
# previous world's `TRAJECTORY_LOG_PATH`. Mirrors the eviction the analytics
# package `__init__` performs for its own private `_recording` leaf.
sys.modules.pop("orchestrator._trajectory_records", None)

from orchestrator._trajectory_records import (  # noqa: E402
    TIMELINE_OUTPUT as TIMELINE_OUTPUT,
    TIMELINE_PROMPT as TIMELINE_PROMPT,
    TRAJECTORY_EVENT as TRAJECTORY_EVENT,
    TimelineEntry as TimelineEntry,
    TrajectoryRun as TrajectoryRun,
    TrajectoryStepView as TrajectoryStepView,
    RunUsageView as RunUsageView,
    TurnUsageView as TurnUsageView,
)
from orchestrator._trajectory_records import (  # noqa: E402
    UNCONFIGURED_LOG_MESSAGE as UNCONFIGURED_LOG_MESSAGE,
    log_unconfigured_message as log_unconfigured_message,
    parse_record as parse_record,
    read_trajectories as read_trajectories,
    resolve_log_path as resolve_log_path,
)


# The facade intentionally keeps the record leaf's original attribute surface;
# the inventory makes those indirect compatibility exports explicit.
_COMPATIBILITY_EXPORTS = (
    TIMELINE_OUTPUT,
    TIMELINE_PROMPT,
    TRAJECTORY_EVENT,
    TimelineEntry,
    TrajectoryStepView,
    RunUsageView,
    TurnUsageView,
    UNCONFIGURED_LOG_MESSAGE,
    log_unconfigured_message,
    parse_record,
    read_trajectories,
    resolve_log_path,
)


@dataclass(frozen=True)
class FilterOptions:
    """Distinct filter values across a set of runs, each sorted."""

    repos: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    agent_roles: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunFilterOptions:
    """Raw optional constraints accepted by `filter_runs`."""

    repo: Optional[str] = None
    backends: Optional[Sequence[str]] = None
    agent_roles: Optional[Sequence[str]] = None
    stages: Optional[Sequence[str]] = None
    issue: Optional[int] = None
    query: Optional[str] = None
    exclude_fixtures: bool = False


class _RunFilterOptionFields(TypedDict, total=False):
    repo: Optional[str]
    backends: Optional[Sequence[str]]
    agent_roles: Optional[Sequence[str]]
    stages: Optional[Sequence[str]]
    issue: Optional[int]
    query: Optional[str]
    exclude_fixtures: bool


@dataclass(frozen=True)
class _RunFilters:
    """Normalized constraints for one trajectory-filtering pass."""

    repo: Optional[str]
    backends: Optional[frozenset[str]]
    agent_roles: Optional[frozenset[str]]
    stages: Optional[frozenset[str]]
    issue: Optional[int]
    query: Optional[str]
    exclude_fixtures: bool


@dataclass(frozen=True)
class TrajectorySummary:
    """Headline counts for the filtered run set (the KPI strip)."""

    total_runs: int = 0
    distinct_issues: int = 0
    distinct_repos: int = 0
    total_tool_calls: int = 0
    truncated_runs: int = 0
    total_cost_usd: float = 0.0


def _distinct_sorted(
    runs: Sequence[TrajectoryRun], key: Callable[[TrajectoryRun], str]
) -> tuple[str, ...]:
    """Distinct, sorted, non-empty values of `key` across `runs`.

    The shared collector behind every `FilterOptions` dimension: an empty
    value -- a run that omitted that field -- is dropped so the sidebar never
    offers a blank choice, and the result is sorted for a stable dropdown.
    """
    collected: set[str] = set()
    for run in runs:
        field = key(run)
        if field:
            collected.add(field)
    return tuple(sorted(collected))


def filter_options(runs: Sequence[TrajectoryRun]) -> FilterOptions:
    """Collect the distinct, sorted filter values across `runs`.

    Empty dimension values are dropped so the sidebar never offers a
    blank choice for a record that omitted (e.g.) its stage.
    """
    return FilterOptions(
        repos=_distinct_sorted(runs, lambda run: run.repo),
        backends=_distinct_sorted(runs, lambda run: run.backend),
        agent_roles=_distinct_sorted(runs, lambda run: run.agent_role),
        stages=_distinct_sorted(runs, lambda run: run.stage),
    )


def _matches_query(run: TrajectoryRun, needle: str) -> bool:
    """Case-insensitive substring match across every free-text field.

    Spans the prompt, system prompt, final output, each step's tool
    name and content, and the skill / tool name sets so the operator
    can find a run by anything it carried -- a file path it touched, a
    tool it called, a skill it triggered, or a phrase in its answer.
    """
    haystacks: list[str] = [
        run.repo,
        run.stage,
        run.agent_role,
        run.user_input,
        run.system_prompt,
        run.output,
    ]
    haystacks.extend(run.tools)
    haystacks.extend(run.skills_triggered)
    haystacks.extend(run.skills_available)
    for step in run.steps:
        haystacks.append(step.name)
        haystacks.append(step.content)
    return any(needle in text.lower() for text in haystacks if text)


def _normalize_filter_values(
    selected_values: Optional[Sequence[str]],
) -> Optional[frozenset[str]]:
    """Normalize an optional multi-value filter for membership checks."""
    return frozenset(selected_values) if selected_values else None


def _normalize_filter_query(query: Optional[str]) -> Optional[str]:
    """Normalize free-text search input, dropping an empty query."""
    if query is None:
        return None
    normalized_query = query.strip().lower()
    return normalized_query or None


def _resolve_run_filter_options(
    options: Optional[RunFilterOptions],
    option_fields: _RunFilterOptionFields,
) -> RunFilterOptions:
    if options is not None and option_fields:
        raise TypeError("pass either options or keyword option fields, not both")
    if options is not None:
        return options
    return RunFilterOptions(**option_fields)


def _normalize_run_filters(options: RunFilterOptions) -> _RunFilters:
    return _RunFilters(
        repo=options.repo,
        backends=_normalize_filter_values(options.backends),
        agent_roles=_normalize_filter_values(options.agent_roles),
        stages=_normalize_filter_values(options.stages),
        issue=options.issue,
        query=_normalize_filter_query(options.query),
        exclude_fixtures=options.exclude_fixtures,
    )


def _matches_scalar_filters(
    run: TrajectoryRun,
    run_filters: _RunFilters,
) -> bool:
    """Match exact repository and issue constraints."""
    return (
        (run_filters.repo is None or run.repo == run_filters.repo)
        and (run_filters.issue is None or run.issue == run_filters.issue)
    )


def _matches_dimension_filters(
    run: TrajectoryRun,
    run_filters: _RunFilters,
) -> bool:
    """Match the optional backend, role, and stage selections."""
    return (
        (run_filters.backends is None or run.backend in run_filters.backends)
        and (
            run_filters.agent_roles is None
            or run.agent_role in run_filters.agent_roles
        )
        and (run_filters.stages is None or run.stage in run_filters.stages)
    )


def _matches_run_filters(
    run: TrajectoryRun,
    run_filters: _RunFilters,
) -> bool:
    """Match one run against every normalized filter constraint."""
    if run_filters.exclude_fixtures and run.is_fixture:
        return False
    if not _matches_scalar_filters(run, run_filters):
        return False
    if not _matches_dimension_filters(run, run_filters):
        return False
    return (
        run_filters.query is None
        or _matches_query(run, run_filters.query)
    )


def filter_runs(
    runs: Sequence[TrajectoryRun],
    options: Optional[RunFilterOptions] = None,
    **option_fields: Unpack[_RunFilterOptionFields],
) -> list[TrajectoryRun]:
    """Return the subset of `runs` matching every supplied filter.

    A `None` or empty multi-value filter (`backends` / `agent_roles` /
    `stages`) means "no constraint on this dimension" -- the friendlier
    viewer default, distinct from the analytics dashboard's tri-state
    multiselect. `repo` / `issue` are exact-match scalars; `query` is a
    case-insensitive substring matched across every free-text field.
    `exclude_fixtures` (default off, so existing callers are unaffected)
    drops the synthetic test-suite records `TrajectoryRun.is_fixture`
    flags. Relative order is preserved.
    """
    run_filters = _normalize_run_filters(
        _resolve_run_filter_options(options, option_fields)
    )
    return [run for run in runs if _matches_run_filters(run, run_filters)]


def summarize(runs: Sequence[TrajectoryRun]) -> TrajectorySummary:
    """Headline counts for the (filtered) run set.

    `total_cost_usd` sums the authoritative run cost over runs that carry
    one -- a run with no `run_usage` (pre-usage record) or an unpriced cost
    (`None`) contributes nothing rather than a spurious 0, so the KPI reads
    the spend of the runs that actually recorded it.
    """
    return TrajectorySummary(
        total_runs=len(runs),
        distinct_issues=len({(run.repo, run.issue) for run in runs}),
        distinct_repos=len({run.repo for run in runs if run.repo}),
        total_tool_calls=sum(run.tool_calls for run in runs),
        truncated_runs=sum(1 for run in runs if run.truncated),
        total_cost_usd=sum(
            run.cost_usd for run in runs if run.cost_usd is not None
        ),
    )
