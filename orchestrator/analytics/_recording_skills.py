# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Skill-field normalization for analytics agent records."""

from __future__ import annotations

from orchestrator import usage
from orchestrator.analytics._recording_models import (
    _AgentExitContext,
    _AgentExitSkillFields,
    _CodexCatalog,
)


def _normalize_agent_exit_skills(
    parsed_skills: usage.SkillTriggers,
    codex_catalog: _CodexCatalog,
) -> _AgentExitSkillFields:
    """Convert parser output into optional event fields.

    Incidental references stay out of `skills_triggered` / the count (and thus
    the `skill_triggered` audit events) -- they ride the separate
    `skills_incidental` / `skills_incidental_count` keys. `skills_evidence`
    persists the per-load tier the parser assigned.
    """
    skills_triggered = list(parsed_skills.triggered) or None
    skills_triggered_count = sum(parsed_skills.trigger_counts.values()) if skills_triggered else None
    skills_available = list(parsed_skills.available) or codex_catalog.available_skills
    skills_incidental = list(parsed_skills.incidental) or None
    skills_incidental_count = sum(parsed_skills.incidental_counts.values()) if skills_incidental else None
    return _AgentExitSkillFields(
        skills_triggered=skills_triggered,
        skills_triggered_count=skills_triggered_count,
        skills_available=skills_available,
        skills_evidence=dict(parsed_skills.evidence) or None,
        skills_incidental=skills_incidental,
        skills_incidental_count=skills_incidental_count,
    )


def _read_agent_exit_skills(
    context: _AgentExitContext,
    codex_catalog: _CodexCatalog,
) -> _AgentExitSkillFields:
    """Parse and normalize skill fields for an enabled run."""
    parsed_skills = usage.parse_agent_skills(
        context.backend,
        context.agent_result.stdout,
    )
    return _normalize_agent_exit_skills(parsed_skills, codex_catalog)


def _parse_agent_exit_skills(
    context: _AgentExitContext,
    codex_catalog: _CodexCatalog,
) -> _AgentExitSkillFields:
    """Parse opt-in skill fields without risking the baseline event."""
    analytics_package = context.analytics_package
    if not analytics_package.TRACK_SKILL_TRIGGERS:
        return _AgentExitSkillFields()
    try:
        return _read_agent_exit_skills(context, codex_catalog)
    except Exception:
        analytics_package.log.exception(
            "issue=#%d analytics: parse_agent_skills(%s) failed; emitting record without skill fields",
            context.issue,
            context.backend,
        )
        return _AgentExitSkillFields()
