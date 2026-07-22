# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Column model and query vocabulary for the skill matrix."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable

from orchestrator.analytics.read import SkillTriggerMatrixRow


@dataclass(frozen=True)
class SkillMatrixColumn:
    key: str
    label: str
    right_aligned: bool
    sort_value: Callable[[SkillTriggerMatrixRow], object]


SKILL_MATRIX_COLUMNS = (
    SkillMatrixColumn("repo", "Repo", False, lambda row: (row.repo or "").lower()),
    SkillMatrixColumn("role", "Role", False, lambda row: (row.agent_role or "").lower()),
    SkillMatrixColumn("backend", "Backend", False, lambda row: (row.backend or "").lower()),
    SkillMatrixColumn("skill", "Skill", False, lambda row: (row.skill or "").lower()),
    SkillMatrixColumn("runs", "Runs", True, lambda row: int(row.runs)),
    SkillMatrixColumn("skill_runs", "Runs with skill", True, lambda row: int(row.skill_runs)),
    SkillMatrixColumn("rate", "Trigger rate", True, lambda row: row.rate),
)
SKILL_MATRIX_NUMERIC_KEYS = frozenset(("runs", "skill_runs", "rate"))
SKILL_MATRIX_SORT_KEYS = MappingProxyType(
    {column.key: column.sort_value for column in SKILL_MATRIX_COLUMNS},
)
SKILL_MATRIX_SORT_PARAM = "mtx_sort"
SKILL_MATRIX_DIR_PARAM = "mtx_dir"
