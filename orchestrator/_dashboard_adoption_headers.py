# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Sortable header rendering for the skill-adoption matrix."""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Optional

from orchestrator import _dashboard_adoption_columns as columns


@dataclass(frozen=True)
class SkillAdoptionHeaderState:
    direction: str
    arrow: str


def _skill_adoption_header_state(
    column: columns.SkillAdoptionColumn,
    active_key: Optional[str],
    descending: bool,
) -> SkillAdoptionHeaderState:
    if column.key == active_key:
        direction = "asc" if descending else "desc"
        arrow = "▼" if descending else "▲"
        return SkillAdoptionHeaderState(direction=direction, arrow=arrow)
    if column.key in columns.SKILL_ADOPTION_NUMERIC_KEYS:
        return SkillAdoptionHeaderState(direction="desc", arrow="")
    return SkillAdoptionHeaderState(direction="asc", arrow="")


def _skill_adoption_header_cell(
    column: columns.SkillAdoptionColumn,
    active_key: Optional[str],
    descending: bool,
) -> str:
    state = _skill_adoption_header_state(column, active_key, descending)
    cell_class = ' class="r"' if column.right_aligned else ""
    arrow_html = ""
    if state.arrow:
        arrow_html = f'<span class="orch-skilladopt-sort">{state.arrow}</span>'
    return (
        f"<th{cell_class}>"
        '<a class="orch-skilladopt-h" '
        f'href="?{columns.SKILL_ADOPTION_SORT_PARAM}={column.key}'
        f'&{columns.SKILL_ADOPTION_DIR_PARAM}={state.direction}" target="_self">'
        f"{html.escape(column.label)}</a>{arrow_html}</th>"
    )


def _skill_adoption_header_html(
    active_key: Optional[str],
    descending: bool,
) -> str:
    cells = (
        _skill_adoption_header_cell(column, active_key, descending)
        for column in columns.SKILL_ADOPTION_COLUMNS
    )
    return "<thead><tr>{0}</tr></thead>".format("".join(cells))
