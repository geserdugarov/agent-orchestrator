# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Normalize and apply trajectory-run filters."""

from __future__ import annotations

from orchestrator import _trajectory_filter_models as models
from orchestrator import _trajectory_filter_values as filter_values
from orchestrator._trajectory_run_model import TrajectoryRun


def resolve_run_filter_options(
    options: object,
    option_fields: models.RunFilterOptionFields,
    options_type: type,
) -> object:
    if options is not None and option_fields:
        raise TypeError("pass either options or keyword option fields, not both")
    if options is not None:
        return options
    return options_type(**option_fields)


def normalize_run_filters(options: object) -> models.RunFilters:
    return models.RunFilters(
        repo=options.repo,
        backends=filter_values.normalize_filter_values(options.backends),
        agent_roles=filter_values.normalize_filter_values(options.agent_roles),
        stages=filter_values.normalize_filter_values(options.stages),
        issue=options.issue,
        query=filter_values.normalize_filter_query(options.query),
        exclude_fixtures=options.exclude_fixtures,
    )


def matches_scalar_filters(
    run: TrajectoryRun,
    run_filters: models.RunFilters,
) -> bool:
    return (run_filters.repo is None or run.repo == run_filters.repo) and (
        run_filters.issue is None or run.issue == run_filters.issue
    )


def matches_dimension_filters(
    run: TrajectoryRun,
    run_filters: models.RunFilters,
) -> bool:
    return (
        (run_filters.backends is None or run.backend in run_filters.backends)
        and (run_filters.agent_roles is None or run.agent_role in run_filters.agent_roles)
        and (run_filters.stages is None or run.stage in run_filters.stages)
    )


def matches_run_filters(
    run: TrajectoryRun,
    run_filters: models.RunFilters,
) -> bool:
    if run_filters.exclude_fixtures and run.is_fixture:
        return False
    if not matches_scalar_filters(run, run_filters):
        return False
    if not matches_dimension_filters(run, run_filters):
        return False
    return run_filters.query is None or filter_values.matches_query(run, run_filters.query)
