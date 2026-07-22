# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Opt-in trajectory recorder backed by focused serialization leaves."""

from __future__ import annotations

import importlib
import threading

_deps = importlib.import_module("orchestrator.analytics._trajectory_dependencies")


usage = _deps.usage
_AgentExitContext = _deps._recording._AgentExitContext
_CodexCatalog = _deps._recording._CodexCatalog
_append_jsonl_record = _deps._recording._append_jsonl_record
_live_settings = _deps._recording._live_settings
build_record = _deps._recording.build_record
log = _deps._recording.log
_TrajectoryHeadline = _deps._trajectory_models._TrajectoryHeadline
_TrajectoryBudget = _deps._trajectory_models._TrajectoryBudget
_Redactor = _deps._trajectory_sanitize._Redactor
_truncate_head_tail = _deps._trajectory_sanitize._truncate_head_tail
_redact_tree = _deps._trajectory_sanitize._redact_tree
_redact_and_truncate = _deps._trajectory_sanitize._redact_and_truncate
_trajectory_usage = _deps._trajectory_serialize._trajectory_usage
_trajectory_headline = _deps._trajectory_serialize._trajectory_headline
_bounded_trajectory_turns = _deps._trajectory_serialize._bounded_trajectory_turns
_trajectory_step = _deps._trajectory_serialize._trajectory_step
_bounded_trajectory_steps = _deps._trajectory_serialize._bounded_trajectory_steps
_build_trajectory_record = _deps._trajectory_serialize._build_trajectory_record
_codex_trajectory_changes = _deps._trajectory_persistence._codex_trajectory_changes
_agent_trajectory = _deps._trajectory_persistence._agent_trajectory
_persist_trajectory_record = _deps._trajectory_persistence._persist_trajectory_record
_maybe_record_trajectory = _deps._trajectory_persistence._maybe_record_trajectory

_TRAJECTORY_FIELD_HEAD = 2000
_TRAJECTORY_FIELD_TAIL = 2000
_TRAJECTORY_RECORD_BUDGET = 200_000
_TRAJECTORY_FILE_LOCK = threading.Lock()


def append_trajectory_record(record: dict) -> None:
    """Append one JSONL line to the configured trajectory sink."""
    _append_jsonl_record(
        _live_settings().TRAJECTORY_LOG_PATH,
        _TRAJECTORY_FILE_LOCK,
        record,
    )
