# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Sortable header rendering for the invocation-level skill matrix."""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Optional

from orchestrator import _dashboard_matrix_columns as columns


@dataclass(frozen=True)
class SkillMatrixHeaderState:
    direction: str
    arrow: str


def _skill_matrix_header_state(
    column: columns.SkillMatrixColumn,
    active_key: Optional[str],
    descending: bool,
) -> SkillMatrixHeaderState:
    if column.key == active_key:
        direction = "asc" if descending else "desc"
        arrow = "▼" if descending else "▲"
        return SkillMatrixHeaderState(direction=direction, arrow=arrow)
    if column.key in columns.SKILL_MATRIX_NUMERIC_KEYS:
        return SkillMatrixHeaderState(direction="desc", arrow="")
    return SkillMatrixHeaderState(direction="asc", arrow="")


def _skill_matrix_header_cell(
    column: columns.SkillMatrixColumn,
    active_key: Optional[str],
    descending: bool,
) -> str:
    state = _skill_matrix_header_state(column, active_key, descending)
    cell_class = ' class="r"' if column.right_aligned else ""
    arrow_html = ""
    if state.arrow:
        arrow_html = f'<span class="orch-skillmatrix-sort">{state.arrow}</span>'
    return (
        f"<th{cell_class}>"
        '<a class="orch-skillmatrix-h" '
        f'href="?{columns.SKILL_MATRIX_SORT_PARAM}={column.key}'
        f'&{columns.SKILL_MATRIX_DIR_PARAM}={state.direction}" target="_self">'
        f"{html.escape(column.label)}</a>{arrow_html}</th>"
    )


def _skill_matrix_header_html(
    active_key: Optional[str],
    descending: bool,
) -> str:
    cells = (
        _skill_matrix_header_cell(column, active_key, descending)
        for column in columns.SKILL_MATRIX_COLUMNS
    )
    return "<thead><tr>{0}</tr></thead>".format("".join(cells))
