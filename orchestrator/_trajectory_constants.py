# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory record kinds and operator-facing messages."""

TRAJECTORY_EVENT = "agent_trajectory"
TIMELINE_PROMPT = "prompt"
TIMELINE_OUTPUT = "output"
FIXTURE_PROMPT = "ignored"
FIXTURE_SESSION_PREFIX = "sess-"
FIXTURE_SKILL_TOOL = "Skill"
UNCONFIGURED_LOG_MESSAGE = (
    "`TRAJECTORY_LOG_PATH` is not configured. The trajectory sink is "
    "opt-in and default-off, so no trajectories have been recorded. Set "
    "`TRAJECTORY_LOG_PATH=/path/to/trajectories.jsonl` in the environment "
    "and **relaunch** the orchestrator so `record_agent_exit` starts "
    "appending records, then relaunch this viewer."
)
