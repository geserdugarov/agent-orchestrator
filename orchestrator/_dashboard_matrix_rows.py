# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Row projection and rendering for the invocation-level skill matrix."""
from __future__ import annotations

import html
from dataclasses import dataclass

from orchestrator.analytics.read import SkillTriggerMatrixRow
from orchestrator.dashboard_html import _UNKNOWN


def _muted_zero_html(text: str) -> str:
    return f'<span class="orch-skillmatrix-zero">{text}</span>'


@dataclass(frozen=True)
class SkillMatrixRowView:
    repo: str
    role: str
    backend: str
    skill: str
    runs: int
    skill_runs_html: str
    rate_html: str


def _skill_matrix_row_view(row: SkillTriggerMatrixRow) -> SkillMatrixRowView:
    skill_runs = int(row.skill_runs)
    if skill_runs == 0:
        skill_runs_html = _muted_zero_html("0")
        rate_html = _muted_zero_html("0%")
    else:
        skill_runs_html = str(skill_runs)
        rate_percentage = row.rate * 100
        rate_html = f"{rate_percentage:.0f}%"
    return SkillMatrixRowView(
        repo=row.repo or _UNKNOWN,
        role=row.agent_role or _UNKNOWN,
        backend=row.backend or _UNKNOWN,
        skill=row.skill or _UNKNOWN,
        runs=int(row.runs),
        skill_runs_html=skill_runs_html,
        rate_html=rate_html,
    )


def _skill_matrix_row_html(row: SkillTriggerMatrixRow) -> str:
    row_view = _skill_matrix_row_view(row)
    return (
        "<tr>"
        f'<td class="strong">{html.escape(row_view.repo)}</td>'
        f"<td>{html.escape(row_view.role)}</td>"
        f"<td>{html.escape(row_view.backend)}</td>"
        f"<td>{html.escape(row_view.skill)}</td>"
        f'<td class="r">{row_view.runs}</td>'
        f'<td class="r">{row_view.skill_runs_html}</td>'
        f'<td class="r">{row_view.rate_html}</td>'
        "</tr>"
    )
