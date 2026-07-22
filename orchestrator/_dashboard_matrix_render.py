# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Top-level invocation-level skill-matrix rendering."""
from __future__ import annotations

import html
from typing import Optional, Sequence

from orchestrator.analytics.read import SkillTriggerMatrixRow
from orchestrator import _dashboard_matrix_headers as headers
from orchestrator import _dashboard_matrix_rows as row_rendering
from orchestrator import _dashboard_matrix_sort as sorting
from orchestrator.dashboard_html import _table_css, _table_html


SKILL_MATRIX_EMPTY_MESSAGE = (
    "No catalog-backed skill matrix for this window. The matrix pairs "
    "each repo's offered-skill catalog with the skills its runs "
    "triggered; it fills in once `TRACK_SKILL_TRIGGERS` (default off) "
    "has recorded a repo skill catalog and at least one run's triggered "
    "skills."
)
SKILL_MATRIX_EXTRA_CSS = """
  .orch-skillmatrix td.strong { font-weight: 600; color: var(--orch-ink); }
  .orch-skillmatrix-zero { color: var(--orch-muted-soft); }
  .orch-skillmatrix thead th a.orch-skillmatrix-h { color: inherit;
    text-decoration: none; cursor: pointer; }
  .orch-skillmatrix thead th a.orch-skillmatrix-h:hover {
    color: var(--orch-ink); text-decoration: underline; }
  .orch-skillmatrix-sort { margin-left: 3px; color: var(--orch-accent); }
"""


def _skill_matrix_html(
    rows: Sequence[SkillTriggerMatrixRow],
    *,
    sort_key: Optional[str] = None,
    descending: bool = False,
) -> str:
    """Render the invocation-level per-skill matrix to inline HTML."""
    if len(rows) == 0:
        return (
            '<div class="orch-skillmatrix-empty" '
            'style="color:var(--orch-muted);font-size:12.5px;padding:8px 2px">'
            f"{html.escape(SKILL_MATRIX_EMPTY_MESSAGE)}</div>"
        )
    if sort_key is None:
        rows = sorting._default_sort_skill_matrix_rows(rows)
    else:
        rows = sorting._sort_skill_matrix_rows(rows, sort_key, descending)
    return _table_html(
        table_class="orch-skillmatrix",
        css=_table_css("orch-skillmatrix", extra_rules=SKILL_MATRIX_EXTRA_CSS),
        head=headers._skill_matrix_header_html(sort_key, descending),
        rows=[row_rendering._skill_matrix_row_html(row) for row in rows],
    )
