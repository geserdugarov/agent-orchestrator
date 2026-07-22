# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable trajectory record models and JSONL read entry points."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Optional

from orchestrator import analytics
from orchestrator import _trajectory_constants as constants
from orchestrator import _trajectory_file_read as file_read
from orchestrator import _trajectory_record_parse as record_parse
from orchestrator._trajectory_run_model import TrajectoryRun as TrajectoryRun
from orchestrator import _trajectory_view_models as view_models


TRAJECTORY_EVENT = constants.TRAJECTORY_EVENT
TIMELINE_PROMPT = constants.TIMELINE_PROMPT
TIMELINE_OUTPUT = constants.TIMELINE_OUTPUT
UNCONFIGURED_LOG_MESSAGE = constants.UNCONFIGURED_LOG_MESSAGE
RunUsageView = view_models.RunUsageView
TimelineEntry = view_models.TimelineEntry
TrajectoryStepView = view_models.TrajectoryStepView
TurnUsageView = view_models.TurnUsageView
RECORD_SIGNATURE = inspect.Signature(
    parameters=(
        inspect.Parameter("obj", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("seq", inspect.Parameter.KEYWORD_ONLY),
    )
)


def resolve_log_path() -> Optional[Path]:
    """Return the trajectory log path configured for this reader world."""
    return analytics.TRAJECTORY_LOG_PATH


def log_unconfigured_message() -> Optional[str]:
    """Return the opt-in banner when the trajectory sink is disabled."""
    if resolve_log_path() is None:
        return UNCONFIGURED_LOG_MESSAGE
    return None


def parse_record(*args: Any, **kwargs: Any) -> Optional[TrajectoryRun]:
    """Parse one decoded JSONL object through the historical call shape."""
    bound = RECORD_SIGNATURE.bind(*args, **kwargs)
    return record_parse.parse_record(
        bound.arguments["obj"],
        sequence=bound.arguments["seq"],
    )


def read_trajectories(path: Optional[Path] = None) -> list[TrajectoryRun]:
    """Read agent-trajectory records newest first, skipping malformed lines."""
    log_path = resolve_log_path() if path is None else path
    return file_read.read_trajectories(log_path, parse_record)


parse_record.__signature__ = RECORD_SIGNATURE
