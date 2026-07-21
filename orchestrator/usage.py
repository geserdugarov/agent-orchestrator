# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Reconstruct agent run usage, skills, and trajectories from JSONL stdout.

This module is the stable compatibility import site for three focused private
parsers, each the private home of one cohesive surface:

* ``_usage_metrics`` -- the ``UsageMetrics`` dataclass and the claude / codex
  token, model, turn, pricing, and cost parsing reached through
  ``parse_agent_usage`` (``parse_claude_usage`` / ``parse_codex_usage``). It
  also defines the shared event iterator, token decoders, and price path the
  other two reuse, so the resilience contract and cost precedence stay in one
  place.
* ``_usage_skills`` -- the ``SkillTriggers`` dataclass and the
  ``parse_claude_skills`` / ``parse_codex_skills`` / ``parse_agent_skills``
  trio, plus the offered-set init-frame helpers and shared skill/trajectory
  JSONL vocabulary the trajectory classifier reuses.
* ``_usage_trajectory`` -- the ``TrajectoryStep`` / ``TurnUsage`` /
  ``AgentTrajectory`` dataclasses and the ``parse_claude_trajectory`` /
  ``parse_codex_trajectory`` / ``parse_agent_trajectory`` classifier, which
  reconstructs a run's offered tools, triggered skills, ordered timeline, final
  output, and claude-only per-turn usage.

``orchestrator.usage`` re-exports all three public surfaces unchanged so
``agents``, ``workflow``, and ``analytics`` keep importing from the same site.
"""
from __future__ import annotations

# The usage-metric parsing lives in a focused private module; re-export its
# public surface so ``orchestrator.usage`` stays the compatibility import site.
from orchestrator._usage_metrics import (
    UsageMetrics as UsageMetrics,
    parse_agent_usage as parse_agent_usage,
    parse_claude_usage as parse_claude_usage,
    parse_codex_usage as parse_codex_usage,
)
# The skill-trigger parsing lives in a focused private module; re-export its
# public surface so ``orchestrator.usage`` stays the compatibility import site.
from orchestrator._usage_skills import (
    SkillTriggers as SkillTriggers,
    parse_agent_skills as parse_agent_skills,
    parse_claude_skills as parse_claude_skills,
    parse_codex_skills as parse_codex_skills,
)
# The trajectory parsing lives in a focused private module; re-export its
# public surface so ``orchestrator.usage`` stays the compatibility import site.
from orchestrator._usage_trajectory import (
    AgentTrajectory as AgentTrajectory,
    TrajectoryStep as TrajectoryStep,
    TurnUsage as TurnUsage,
    parse_agent_trajectory as parse_agent_trajectory,
    parse_claude_trajectory as parse_claude_trajectory,
    parse_codex_trajectory as parse_codex_trajectory,
)


# This module is the stable attribute-level facade for all three parser leaves;
# the inventory makes its indirect compatibility exports explicit.
_COMPATIBILITY_EXPORTS = (
    UsageMetrics,
    parse_agent_usage,
    parse_claude_usage,
    parse_codex_usage,
    SkillTriggers,
    parse_agent_skills,
    parse_claude_skills,
    parse_codex_skills,
    AgentTrajectory,
    TrajectoryStep,
    TurnUsage,
    parse_agent_trajectory,
    parse_claude_trajectory,
    parse_codex_trajectory,
)
