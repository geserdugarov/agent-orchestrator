# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Sorting rules for the invocation-level skill matrix."""
from __future__ import annotations

from inspect import Parameter, Signature
from typing import Any, Optional, Sequence

from orchestrator.analytics.read import SkillTriggerMatrixRow
from orchestrator import _dashboard_matrix_columns as columns


def parse_skill_matrix_sort(
    *args: Any,
    **kwargs: Any,
) -> tuple[Optional[str], bool]:
    """Resolve the matrix sort key and direction from query parameters."""
    bound = _SORT_SIGNATURE.bind(*args, **kwargs)
    query_params = bound.arguments["params"]
    sort_key = query_params.get(columns.SKILL_MATRIX_SORT_PARAM)
    if sort_key not in columns.SKILL_MATRIX_SORT_KEYS:
        return None, False
    return sort_key, query_params.get(columns.SKILL_MATRIX_DIR_PARAM) == "desc"


_SORT_SIGNATURE = Signature(
    (Parameter("params", Parameter.POSITIONAL_OR_KEYWORD),),
)
parse_skill_matrix_sort.__signature__ = _SORT_SIGNATURE


def _sort_skill_matrix_rows(
    rows: Sequence[SkillTriggerMatrixRow],
    sort_key: Optional[str],
    descending: bool,
) -> list[SkillTriggerMatrixRow]:
    key_function = columns.SKILL_MATRIX_SORT_KEYS.get(sort_key)
    if key_function is None:
        return list(rows)
    return sorted(rows, key=key_function, reverse=descending)


def _default_sort_skill_matrix_rows(
    rows: Sequence[SkillTriggerMatrixRow],
) -> list[SkillTriggerMatrixRow]:
    return sorted(rows, key=_skill_matrix_default_sort_key)


def _skill_matrix_default_sort_key(
    row: SkillTriggerMatrixRow,
) -> tuple[str, float]:
    repo = (row.repo or "").lower()
    rate = -row.rate
    return repo, rate
