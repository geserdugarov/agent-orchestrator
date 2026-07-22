# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Top-level skill-adoption matrix rendering."""
from __future__ import annotations

import html
from typing import Optional, Sequence

from orchestrator.analytics.read import SkillAdoptionRow
from orchestrator import _dashboard_adoption_headers as headers
from orchestrator import _dashboard_adoption_rows as row_rendering
from orchestrator import _dashboard_adoption_sort as sorting
from orchestrator.dashboard_html import _table_css, _table_html


SKILL_ADOPTION_EMPTY_MESSAGE = (
    "No per-session skill adoption for this window. The table counts, per "
    "logical agent session, how many had each skill available and how many "
    "loaded it; it fills in once `TRACK_SKILL_TRIGGERS` (default off) has "
    "recorded at least one session's available and loaded skills."
)
SKILL_ADOPTION_EXTRA_CSS = """
  .orch-skilladopt td.strong { font-weight: 600; color: var(--orch-ink); }
  .orch-skilladopt-zero { color: var(--orch-muted-soft); }
  .orch-skilladopt thead th a.orch-skilladopt-h { color: inherit;
    text-decoration: none; cursor: pointer; }
  .orch-skilladopt thead th a.orch-skilladopt-h:hover {
    color: var(--orch-ink); text-decoration: underline; }
  .orch-skilladopt-sort { margin-left: 3px; color: var(--orch-accent); }
"""


def _skill_adoption_html(
    rows: Sequence[SkillAdoptionRow],
    *,
    sort_key: Optional[str] = None,
    descending: bool = False,
) -> str:
    """Render the per-skill session-adoption matrix to inline HTML."""
    if len(rows) == 0:
        return (
            '<div class="orch-skilladopt-empty" '
            'style="color:var(--orch-muted);font-size:12.5px;padding:8px 2px">'
            f"{html.escape(SKILL_ADOPTION_EMPTY_MESSAGE)}</div>"
        )
    if sort_key is None:
        rows = sorting._default_sort_skill_adoption_rows(rows)
    else:
        rows = sorting._sort_skill_adoption_rows(rows, sort_key, descending)
    return _table_html(
        table_class="orch-skilladopt",
        css=_table_css("orch-skilladopt", extra_rules=SKILL_ADOPTION_EXTRA_CSS),
        head=headers._skill_adoption_header_html(sort_key, descending),
        rows=[row_rendering._skill_adoption_row_html(row) for row in rows],
    )
