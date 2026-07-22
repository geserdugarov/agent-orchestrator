# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Static declarations for the lazy analytics package facade."""

from pathlib import Path
from typing import Any, Optional

ANALYTICS_DB_URL: Optional[str]
ANALYTICS_LOG_PATH: Optional[Path]
ANALYTICS_RETENTION_DAYS: int
TRACK_SKILL_TRIGGERS: bool
TRAJECTORY_LOG_PATH: Optional[Path]
TRAJECTORY_RETENTION_DAYS: int
append_record: Any
append_trajectory_record: Any
build_record: Any
config: Any
prune_old_records: Any
prune_trajectory_records: Any
prune_with_retention_logging: Any
record_agent_exit: Any
record_repo_skill_catalog: Any
record_stage_enter: Any
record_stage_evaluation: Any
__all__: tuple[str, ...]
