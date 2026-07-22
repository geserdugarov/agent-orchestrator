# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Row projection and rendering for the skill-adoption matrix."""
from __future__ import annotations

import html
from dataclasses import dataclass

from orchestrator.analytics.read import SkillAdoptionRow
from orchestrator.dashboard_html import _UNKNOWN


def _muted_zero_html(text: str) -> str:
    return f'<span class="orch-skilladopt-zero">{text}</span>'


def _adoption_count_html(count: int) -> str:
    if count == 0:
        return _muted_zero_html("0")
    return str(count)


def _adoption_rate_html(row: SkillAdoptionRow) -> str:
    if row.sessions == 0:
        return _muted_zero_html("—")
    rate_percentage = row.adoption_rate * 100
    if row.adopted == 0:
        return _muted_zero_html("0%")
    return f"{rate_percentage:.0f}%"


@dataclass(frozen=True)
class SkillAdoptionRowView:
    repo: str
    role: str
    backend: str
    skill: str
    sessions_html: str
    adopted_html: str
    rate_html: str
    loads_html: str
    incidental_html: str


def _skill_adoption_row_view(row: SkillAdoptionRow) -> SkillAdoptionRowView:
    return SkillAdoptionRowView(
        repo=row.repo or _UNKNOWN,
        role=row.agent_role or _UNKNOWN,
        backend=row.backend or _UNKNOWN,
        skill=row.skill or _UNKNOWN,
        sessions_html=_adoption_count_html(int(row.sessions)),
        adopted_html=_adoption_count_html(int(row.adopted)),
        rate_html=_adoption_rate_html(row),
        loads_html=_adoption_count_html(int(row.load_rows)),
        incidental_html=_adoption_count_html(int(row.incidental)),
    )


def _skill_adoption_row_html(row: SkillAdoptionRow) -> str:
    row_view = _skill_adoption_row_view(row)
    return (
        "<tr>"
        f'<td class="strong">{html.escape(row_view.repo)}</td>'
        f"<td>{html.escape(row_view.role)}</td>"
        f"<td>{html.escape(row_view.backend)}</td>"
        f"<td>{html.escape(row_view.skill)}</td>"
        f'<td class="r">{row_view.sessions_html}</td>'
        f'<td class="r">{row_view.adopted_html}</td>'
        f'<td class="r">{row_view.rate_html}</td>'
        f'<td class="r">{row_view.loads_html}</td>'
        f'<td class="r">{row_view.incidental_html}</td>'
        "</tr>"
    )
