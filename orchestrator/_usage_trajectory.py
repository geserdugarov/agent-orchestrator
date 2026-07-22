# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable trajectory models and provider parser entry points."""

from __future__ import annotations

from orchestrator import _usage_event_stream as event_stream
from orchestrator import _usage_metric_protocol as protocol
from orchestrator import _usage_trajectory_claude_stream as claude_stream
from orchestrator import _usage_trajectory_claude_turns as claude_turns
from orchestrator import _usage_trajectory_codex as codex_trajectory
from orchestrator._usage_skills import parse_claude_skills, parse_codex_skills
from orchestrator import _usage_trajectory_models as trajectory_models


AgentTrajectory = trajectory_models.AgentTrajectory
TrajectoryStep = trajectory_models.TrajectoryStep
TurnUsage = trajectory_models.TurnUsage


def parse_claude_trajectory(stdout: str) -> AgentTrajectory:
    """Classify a Claude stream-json run's trajectory."""
    events = event_stream.iter_events(stdout)
    return AgentTrajectory(
        backend=protocol.CLAUDE,
        tools=claude_stream.offered_tools(events),
        skills=parse_claude_skills(stdout),
        steps=claude_stream.trajectory_steps(events),
        final_output=claude_stream.final_output(events),
        turns=claude_turns.claude_turn_usage(events),
    )


def parse_codex_trajectory(stdout: str) -> AgentTrajectory:
    """Classify a Codex JSON run's trajectory."""
    events = event_stream.iter_events(stdout)
    return AgentTrajectory(
        backend=protocol.CODEX,
        skills=parse_codex_skills(stdout),
        steps=codex_trajectory.trajectory_steps(events),
        final_output=codex_trajectory.final_output(events),
    )


def parse_agent_trajectory(backend: str, stdout: str) -> AgentTrajectory:
    """Dispatch trajectory parsing by agent backend."""
    if backend == protocol.CLAUDE:
        return parse_claude_trajectory(stdout)
    if backend == protocol.CODEX:
        return parse_codex_trajectory(stdout)
    raise ValueError(
        f"unknown agent backend {backend!r}; expected 'claude' or 'codex'",
    )
