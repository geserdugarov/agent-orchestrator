# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Agent-exit record construction and persistence flow."""

from __future__ import annotations

from typing import Optional

from orchestrator import usage
from orchestrator.analytics._recording_catalog import _discover_codex_catalog
from orchestrator.analytics._recording_models import (
    _AgentExitContext,
    _AgentExitSkillFields,
    _CodexCatalog,
)
from orchestrator.analytics._recording_skills import _parse_agent_exit_skills
from orchestrator.analytics._recording_usage import _parse_agent_exit_usage


def _build_agent_exit_record(
    context: _AgentExitContext,
    metrics: usage.UsageMetrics,
    skill_fields: _AgentExitSkillFields,
) -> dict:
    """Build the allowlisted baseline event without raw run content."""
    return context.analytics_package.build_record(
        repo=context.repo,
        issue=context.issue,
        event="agent_exit",
        stage=context.stage,
        agent_role=context.agent_role,
        backend=context.backend,
        agent_spec=context.agent_spec,
        resume_session_id=context.resume_session_id,
        session_id=context.agent_result.session_id,
        review_round=context.review_round,
        retry_count=context.retry_count,
        duration_s=context.duration_s,
        exit_code=context.agent_result.exit_code,
        timed_out=context.agent_result.timed_out,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        cached_tokens=metrics.cached_tokens,
        cache_read_tokens=metrics.cache_read_tokens,
        cache_write_tokens=metrics.cache_write_tokens,
        models=list(metrics.models),
        turns=metrics.turns,
        cost_usd=metrics.cost_usd,
        cost_source=metrics.cost_source,
        skills_triggered=skill_fields.skills_triggered,
        skills_triggered_count=skill_fields.skills_triggered_count,
        skills_available=skill_fields.skills_available,
        skills_evidence=skill_fields.skills_evidence,
        skills_incidental=skill_fields.skills_incidental,
        skills_incidental_count=skill_fields.skills_incidental_count,
    )


def _persist_agent_exit(
    context: _AgentExitContext,
    metrics: usage.UsageMetrics,
    skill_fields: _AgentExitSkillFields,
    codex_catalog: _CodexCatalog,
) -> None:
    """Write the baseline event, then the independently guarded trajectory.

    The trajectory sink is reached through the facade's `_trajectories`
    submodule rather than a direct import so a `_recording` instance always
    dispatches to the same package instance's trajectory recorder -- keeping
    the `_reload` A/B isolation `_live_settings` documents.
    """
    analytics_package = context.analytics_package
    analytics_package.append_record(
        _build_agent_exit_record(context, metrics, skill_fields),
    )
    analytics_package._trajectories._maybe_record_trajectory(
        context,
        metrics,
        codex_catalog,
    )


def _record_agent_exit(context: _AgentExitContext) -> Optional[list[str]]:
    metrics = _parse_agent_exit_usage(context)
    if metrics is None:
        return None
    codex_catalog = _discover_codex_catalog(context)
    skill_fields = _parse_agent_exit_skills(context, codex_catalog)
    _persist_agent_exit(context, metrics, skill_fields, codex_catalog)
    return skill_fields.skills_triggered
