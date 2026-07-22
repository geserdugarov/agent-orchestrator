# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Aggregate skill-trigger table projection and HTML."""
from __future__ import annotations

import html
from typing import Sequence

from orchestrator.analytics.read import SkillTriggerRateRow
from orchestrator import _dashboard_table_html as tables


UNKNOWN = "unknown"
SKILL_TRIGGERS_TABLE_COLUMNS = (
    ("Role", False),
    ("Backend", False),
    ("Runs", True),
    ("Skill runs", True),
    ("Trigger rate", True),
    ("Triggers", True),
)
SKILL_TRIGGERS_EXTRA_CSS = """
  .orch-skills td.strong { font-weight: 600; color: var(--orch-ink); }
  .orch-skill-rate { display: flex; align-items: center; gap: 8px;
    justify-content: flex-end; }
  .orch-skill-bar { display: block; height: 4px; width: 64px;
    border-radius: 2px; background: var(--orch-grid); overflow: hidden; }
  .orch-skill-bar > span { display: block; height: 100%;
    background: var(--orch-accent); border-radius: 2px; }
  .orch-skill-pct { min-width: 34px; color: var(--orch-ink); }
"""


def _skill_trigger_row_html(
    row: SkillTriggerRateRow,
    *,
    max_rate: float,
) -> str:
    role = row.agent_role or UNKNOWN
    backend = row.backend or UNKNOWN
    rate_percentage = row.rate * 100
    bar_percentage = tables._relative_width_pct(row.rate, max_rate)
    return (
        "<tr>"
        f'<td class="strong">{html.escape(role)}</td>'
        f"<td>{html.escape(backend)}</td>"
        f'<td class="r">{int(row.runs)}</td>'
        f'<td class="r">{int(row.skill_runs)}</td>'
        '<td class="r"><span class="orch-skill-rate">'
        '<span class="orch-skill-bar">'
        f'<span style="width:{bar_percentage:.1f}%"></span></span>'
        f'<span class="orch-skill-pct">{rate_percentage:.0f}%</span>'
        "</span></td>"
        f'<td class="r">{int(row.total_triggers)}</td></tr>'
    )


def _skill_triggers_html(rows: Sequence[SkillTriggerRateRow]) -> str:
    """Render aggregate skill-trigger rates to inline HTML."""
    max_rate = max((row.rate for row in rows), default=0) or 1.0
    return tables._table_html(
        table_class="orch-skills",
        css=tables._table_css(
            "orch-skills",
            extra_rules=SKILL_TRIGGERS_EXTRA_CSS,
        ),
        head=tables._table_head_html(SKILL_TRIGGERS_TABLE_COLUMNS),
        rows=[
            _skill_trigger_row_html(row, max_rate=max_rate)
            for row in rows
        ],
    )
