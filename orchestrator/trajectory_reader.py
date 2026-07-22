# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pure filtering and summary read model over trajectory JSONL records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence, Unpack

from orchestrator import _trajectory_filter_match as filter_match
from orchestrator import _trajectory_filter_models as filter_models
from orchestrator import _trajectory_filter_values as filter_values
from orchestrator import _trajectory_reader_bootstrap as bootstrap


records = bootstrap.load_fresh_records()
TIMELINE_OUTPUT = records.TIMELINE_OUTPUT
TIMELINE_PROMPT = records.TIMELINE_PROMPT
TRAJECTORY_EVENT = records.TRAJECTORY_EVENT
TimelineEntry = records.TimelineEntry
TrajectoryRun = records.TrajectoryRun
TrajectoryStepView = records.TrajectoryStepView
RunUsageView = records.RunUsageView
TurnUsageView = records.TurnUsageView
UNCONFIGURED_LOG_MESSAGE = records.UNCONFIGURED_LOG_MESSAGE
log_unconfigured_message = records.log_unconfigured_message
parse_record = records.parse_record
read_trajectories = records.read_trajectories
resolve_log_path = records.resolve_log_path


_COMPATIBILITY_EXPORTS = (
    TIMELINE_OUTPUT,
    TIMELINE_PROMPT,
    TRAJECTORY_EVENT,
    TimelineEntry,
    TrajectoryRun,
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
    """Raw optional constraints accepted by :func:`filter_runs`."""

    repo: Optional[str] = None
    backends: Optional[Sequence[str]] = None
    agent_roles: Optional[Sequence[str]] = None
    stages: Optional[Sequence[str]] = None
    issue: Optional[int] = None
    query: Optional[str] = None
    exclude_fixtures: bool = False


@dataclass(frozen=True)
class TrajectorySummary:
    """Headline counts for the filtered run set."""

    total_runs: int = 0
    distinct_issues: int = 0
    distinct_repos: int = 0
    total_tool_calls: int = 0
    truncated_runs: int = 0
    total_cost_usd: float = field(default_factory=float)


def filter_options(runs: Sequence[TrajectoryRun]) -> FilterOptions:
    """Collect distinct, sorted, non-empty filter values."""
    return FilterOptions(
        repos=filter_values.distinct_sorted(runs, lambda run: run.repo),
        backends=filter_values.distinct_sorted(runs, lambda run: run.backend),
        agent_roles=filter_values.distinct_sorted(runs, lambda run: run.agent_role),
        stages=filter_values.distinct_sorted(runs, lambda run: run.stage),
    )


def filter_runs(
    runs: Sequence[TrajectoryRun],
    options: Optional[RunFilterOptions] = None,
    **option_fields: Unpack[filter_models.RunFilterOptionFields],
) -> list[TrajectoryRun]:
    """Return runs matching every supplied filter while preserving order."""
    resolved = filter_match.resolve_run_filter_options(
        options,
        option_fields,
        RunFilterOptions,
    )
    run_filters = filter_match.normalize_run_filters(resolved)
    return [run for run in runs if filter_match.matches_run_filters(run, run_filters)]


def summarize(runs: Sequence[TrajectoryRun]) -> TrajectorySummary:
    """Build headline counts for a filtered run set."""
    return TrajectorySummary(
        total_runs=len(runs),
        distinct_issues=len({(run.repo, run.issue) for run in runs}),
        distinct_repos=len({run.repo for run in runs if run.repo}),
        total_tool_calls=sum(run.tool_calls for run in runs),
        truncated_runs=sum(1 for run in runs if run.truncated),
        total_cost_usd=sum(run.cost_usd for run in runs if run.cost_usd is not None),
    )
