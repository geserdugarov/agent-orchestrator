# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics package export and reload inventory."""

from __future__ import annotations

EXPORTED_NAMES = (
    "ANALYTICS_DB_URL",
    "ANALYTICS_LOG_PATH",
    "ANALYTICS_RETENTION_DAYS",
    "TRACK_SKILL_TRIGGERS",
    "TRAJECTORY_LOG_PATH",
    "TRAJECTORY_RETENTION_DAYS",
    "append_record",
    "append_trajectory_record",
    "build_record",
    "config",
    "prune_old_records",
    "prune_trajectory_records",
    "prune_with_retention_logging",
    "record_agent_exit",
    "record_repo_skill_catalog",
    "record_stage_enter",
    "record_stage_evaluation",
)

IMPLEMENTATION_MODULES = (
    "orchestrator.analytics._recording",
    "orchestrator.analytics._recording_agent_exit",
    "orchestrator.analytics._recording_catalog",
    "orchestrator.analytics._recording_dependencies",
    "orchestrator.analytics._recording_io",
    "orchestrator.analytics._recording_models",
    "orchestrator.analytics._recording_settings",
    "orchestrator.analytics._recording_skills",
    "orchestrator.analytics._recording_usage",
    "orchestrator.analytics._retention",
    "orchestrator.analytics._retention_rewrite",
    "orchestrator.analytics._retention_scan",
    "orchestrator.analytics._trajectories",
    "orchestrator.analytics._trajectory_dependencies",
    "orchestrator.analytics._trajectory_models",
    "orchestrator.analytics._trajectory_persistence",
    "orchestrator.analytics._trajectory_sanitize",
    "orchestrator.analytics._trajectory_serialize",
)
