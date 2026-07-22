# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fresh per-package analytics implementations and export values."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from orchestrator.analytics._package_manifest import (
    EXPORTED_NAMES,
    IMPLEMENTATION_MODULES,
)


@dataclass(frozen=True)
class _AnalyticsModules:
    recording: ModuleType
    trajectories: ModuleType
    retention: ModuleType


def _evict_implementations() -> None:
    for module_name in IMPLEMENTATION_MODULES:
        sys.modules.pop(module_name, None)


def _load_modules() -> _AnalyticsModules:
    _evict_implementations()
    return _AnalyticsModules(
        recording=importlib.import_module("orchestrator.analytics._recording"),
        trajectories=importlib.import_module(
            "orchestrator.analytics._trajectories",
        ),
        retention=importlib.import_module("orchestrator.analytics._retention"),
    )


def _setting_exports(modules: _AnalyticsModules) -> dict[str, Any]:
    recording = modules.recording
    return {
        "ANALYTICS_LOG_PATH": recording._parse_log_path(),
        "ANALYTICS_RETENTION_DAYS": recording._parse_retention_days(),
        "ANALYTICS_DB_URL": recording._parse_db_url(),
        "TRACK_SKILL_TRIGGERS": recording._parse_track_skill_triggers(),
        "TRAJECTORY_LOG_PATH": recording._parse_trajectory_log_path(),
        "TRAJECTORY_RETENTION_DAYS": (recording._parse_trajectory_retention_days()),
    }


def _public_exports(modules: _AnalyticsModules) -> dict[str, Any]:
    from orchestrator import config

    recording = modules.recording
    trajectories = modules.trajectories
    retention = modules.retention
    return {
        "append_record": recording.append_record,
        "append_trajectory_record": trajectories.append_trajectory_record,
        "build_record": recording.build_record,
        "config": config,
        "prune_old_records": retention.prune_old_records,
        "prune_trajectory_records": retention.prune_trajectory_records,
        "prune_with_retention_logging": retention.prune_with_retention_logging,
        "record_agent_exit": recording.record_agent_exit,
        "record_repo_skill_catalog": recording.record_repo_skill_catalog,
        "record_stage_enter": recording.record_stage_enter,
        "record_stage_evaluation": recording.record_stage_evaluation,
    }


def _compatibility_exports(modules: _AnalyticsModules) -> dict[str, Any]:
    recording = modules.recording
    trajectories = modules.trajectories
    return {
        "AgentResult": recording.AgentResult,
        "usage": recording.usage,
        "_FILE_LOCK": recording._FILE_LOCK,
        "log": recording.log,
        "os": recording.os,
        "_TRAJECTORY_FIELD_HEAD": trajectories._TRAJECTORY_FIELD_HEAD,
        "_TRAJECTORY_FIELD_TAIL": trajectories._TRAJECTORY_FIELD_TAIL,
        "_TRAJECTORY_FILE_LOCK": trajectories._TRAJECTORY_FILE_LOCK,
        "_TRAJECTORY_RECORD_BUDGET": trajectories._TRAJECTORY_RECORD_BUDGET,
    }


def initialize_package(package: ModuleType) -> None:
    """Populate one analytics package instance with a coherent module set."""
    modules = _load_modules()
    exported_values = _setting_exports(modules)
    exported_values.update(_public_exports(modules))
    exported_values.update(_compatibility_exports(modules))
    exported_values["__all__"] = EXPORTED_NAMES
    exported_values["_ANALYTICS_EXPORTS_INITIALIZED"] = True
    package.__dict__.update(exported_values)
