# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable skill-matrix surface backed by focused table leaves."""
from __future__ import annotations

from orchestrator import _dashboard_matrix_columns as columns
from orchestrator import _dashboard_matrix_headers as headers
from orchestrator import _dashboard_matrix_render as rendering
from orchestrator import _dashboard_matrix_rows as rows
from orchestrator import _dashboard_matrix_sort as sorting


SKILL_MATRIX_EMPTY_MESSAGE = rendering.SKILL_MATRIX_EMPTY_MESSAGE
_SKILL_MATRIX_EXTRA_CSS = rendering.SKILL_MATRIX_EXTRA_CSS
_SkillMatrixColumn = columns.SkillMatrixColumn
_SKILL_MATRIX_COLUMNS = columns.SKILL_MATRIX_COLUMNS
_SKILL_MATRIX_NUMERIC_KEYS = columns.SKILL_MATRIX_NUMERIC_KEYS
_SKILL_MATRIX_SORT_KEYS = columns.SKILL_MATRIX_SORT_KEYS
SKILL_MATRIX_SORT_PARAM = columns.SKILL_MATRIX_SORT_PARAM
SKILL_MATRIX_DIR_PARAM = columns.SKILL_MATRIX_DIR_PARAM
parse_skill_matrix_sort = sorting.parse_skill_matrix_sort
_sort_skill_matrix_rows = sorting._sort_skill_matrix_rows
_default_sort_skill_matrix_rows = sorting._default_sort_skill_matrix_rows
_skill_matrix_default_sort_key = sorting._skill_matrix_default_sort_key
_SkillMatrixHeaderState = headers.SkillMatrixHeaderState
_skill_matrix_header_state = headers._skill_matrix_header_state
_skill_matrix_header_cell = headers._skill_matrix_header_cell
_skill_matrix_header_html = headers._skill_matrix_header_html
_muted_zero_html = rows._muted_zero_html
_SkillMatrixRowView = rows.SkillMatrixRowView
_skill_matrix_row_view = rows._skill_matrix_row_view
_skill_matrix_row_html = rows._skill_matrix_row_html
_skill_matrix_html = rendering._skill_matrix_html
