# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Sorting rules for the skill-adoption matrix."""
from __future__ import annotations

from inspect import Parameter, Signature
from typing import Any, Optional, Sequence

from orchestrator.analytics.read import SkillAdoptionRow
from orchestrator import _dashboard_adoption_columns as columns


def parse_skill_adoption_sort(
    *args: Any,
    **kwargs: Any,
) -> tuple[Optional[str], bool]:
    """Resolve the adoption sort key and direction from query parameters."""
    bound = _SORT_SIGNATURE.bind(*args, **kwargs)
    query_params = bound.arguments["params"]
    sort_key = query_params.get(columns.SKILL_ADOPTION_SORT_PARAM)
    if sort_key not in columns.SKILL_ADOPTION_SORT_KEYS:
        return None, False
    return sort_key, query_params.get(columns.SKILL_ADOPTION_DIR_PARAM) == "desc"


_SORT_SIGNATURE = Signature(
    (Parameter("params", Parameter.POSITIONAL_OR_KEYWORD),),
)
parse_skill_adoption_sort.__signature__ = _SORT_SIGNATURE


def _sort_skill_adoption_rows(
    rows: Sequence[SkillAdoptionRow],
    sort_key: Optional[str],
    descending: bool,
) -> list[SkillAdoptionRow]:
    key_function = columns.SKILL_ADOPTION_SORT_KEYS.get(sort_key)
    if key_function is None:
        return list(rows)
    return sorted(rows, key=key_function, reverse=descending)


def _default_sort_skill_adoption_rows(
    rows: Sequence[SkillAdoptionRow],
) -> list[SkillAdoptionRow]:
    return sorted(rows, key=_skill_adoption_default_sort_key)


def _skill_adoption_default_sort_key(
    row: SkillAdoptionRow,
) -> tuple[str, float]:
    repo = (row.repo or "").lower()
    rate = -row.adoption_rate
    return repo, rate
