# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory parsing, Codex enrichment, and fail-open persistence."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from orchestrator import usage
from orchestrator.analytics._recording import (
    _AgentExitContext,
    _CodexCatalog,
    _live_settings,
    log,
)
from orchestrator.analytics._trajectory_serialize import _build_trajectory_record


def _codex_trajectory_changes(
    trajectory: usage.AgentTrajectory,
    catalog: _CodexCatalog,
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if catalog.available_skills and not trajectory.skills.available:
        changes["skills"] = replace(
            trajectory.skills,
            available=tuple(catalog.available_skills),
        )
    if catalog.tools and not trajectory.tools:
        changes["tools"] = tuple(catalog.tools)
    return changes


def _agent_trajectory(
    context: _AgentExitContext,
    catalog: _CodexCatalog,
) -> usage.AgentTrajectory:
    trajectory = usage.parse_agent_trajectory(
        context.backend,
        context.agent_result.stdout,
    )
    if context.backend != "codex":
        return trajectory
    changes = _codex_trajectory_changes(trajectory, catalog)
    if not changes:
        return trajectory
    return replace(trajectory, **changes)


def _persist_trajectory_record(
    context: _AgentExitContext,
    metrics: usage.UsageMetrics,
    codex_catalog: _CodexCatalog,
) -> None:
    """Build and append the denormalized trajectory record.

    `_redact_secrets` is imported at call time to avoid a
    `github` -> `analytics` -> `workflow_messages` -> `github` import cycle.
    """
    from orchestrator.workflow_messages import _redact_secrets

    trajectory = _agent_trajectory(context, codex_catalog)
    _live_settings().append_trajectory_record(_build_trajectory_record(context, trajectory, metrics, _redact_secrets))


def _maybe_record_trajectory(
    context: _AgentExitContext,
    metrics: usage.UsageMetrics,
    codex_catalog: _CodexCatalog,
) -> None:
    """Parse, redact, truncate, and append one trajectory record -- gated on
    the opt-in `TRAJECTORY_LOG_PATH` and wrapped in its own fail-open guard.

    A no-op when the trajectory sink is disabled (the default), so the
    orchestrator-built prompt (`user_input`) -- and the parse/redact work
    itself -- happens ONLY when an operator turned the sink on. `metrics` is
    the `UsageMetrics` `record_agent_exit` already parsed for the baseline
    `agent_exit` record; it is threaded through (never re-parsed) so the
    trajectory record can carry a denormalized `run_usage` summary. The whole
    block rides a dedicated try/except: a parser bug, an unredactable
    payload, or a sink IO failure logs and is swallowed so it can never drop
    the baseline `agent_exit` usage / cost record or the `skill_triggered`
    audit events, all of which were already produced before this runs.
    `_redact_secrets` is imported at call time to avoid a
    `github` -> `analytics` -> `workflow_messages` -> `github` import cycle.

    `codex_catalog` carries the out-of-band offered-skills and offered-tools
    sets `record_agent_exit` discovered for a codex run (empty for claude,
    whose offered sets already ride its stream). When present they backfill
    the codex trajectory's otherwise-empty
    `skills.available` / `tools` so the trajectory viewer's "Skills available"
    and "Tools offered" chips match a claude run's; a non-empty stream-parsed
    set is never overridden.
    """
    if _live_settings().TRAJECTORY_LOG_PATH is None:
        return
    try:
        _persist_trajectory_record(context, metrics, codex_catalog)
    except Exception:
        log.exception(
            "issue=#%d analytics: trajectory record(%s) failed; baseline agent_exit record is unaffected",
            context.issue,
            context.backend,
        )
