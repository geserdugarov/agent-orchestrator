# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Column model and query vocabulary for skill adoption."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable

from orchestrator.analytics.read import SkillAdoptionRow


@dataclass(frozen=True)
class SkillAdoptionColumn:
    key: str
    label: str
    right_aligned: bool
    sort_value: Callable[[SkillAdoptionRow], object]


SKILL_ADOPTION_COLUMNS = (
    SkillAdoptionColumn("repo", "Repo", False, lambda row: (row.repo or "").lower()),
    SkillAdoptionColumn("role", "Role", False, lambda row: (row.agent_role or "").lower()),
    SkillAdoptionColumn("backend", "Backend", False, lambda row: (row.backend or "").lower()),
    SkillAdoptionColumn("skill", "Skill", False, lambda row: (row.skill or "").lower()),
    SkillAdoptionColumn("sessions", "Sessions", True, lambda row: int(row.sessions)),
    SkillAdoptionColumn("adopted", "Sessions using skill", True, lambda row: int(row.adopted)),
    SkillAdoptionColumn("rate", "Adoption rate", True, lambda row: row.adoption_rate),
    SkillAdoptionColumn("loads", "Invocation loads", True, lambda row: int(row.load_rows)),
    SkillAdoptionColumn("incidental", "Incidental references", True, lambda row: int(row.incidental)),
)
SKILL_ADOPTION_NUMERIC_KEYS = frozenset(
    ("sessions", "adopted", "rate", "loads", "incidental"),
)
SKILL_ADOPTION_SORT_KEYS = MappingProxyType(
    {column.key: column.sort_value for column in SKILL_ADOPTION_COLUMNS},
)
SKILL_ADOPTION_SORT_PARAM = "adopt_sort"
SKILL_ADOPTION_DIR_PARAM = "adopt_dir"
