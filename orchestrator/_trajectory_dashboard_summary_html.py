# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory topbar and KPI-strip HTML."""
from __future__ import annotations

import html
from dataclasses import dataclass

from orchestrator import dashboard_theme as theme
from orchestrator import trajectory_reader


@dataclass(frozen=True)
class _TrajectoryKpi:
    label: str
    figure: str
    foot: str = ""


def _card_header_html(title: str, sub: str) -> str:
    return (
        f'<p class="orch-card-title">{html.escape(title)}</p>'
        f'<p class="orch-card-sub">{html.escape(sub)}</p>'
    )


def _topbar_html(total_runs: int, shown_runs: int) -> str:
    return (
        '<div class="orch-topbar"><div class="orch-brand">'
        '<span class="orch-brand-mark">TR</span><div>'
        '<h1>Orchestrator Trajectories</h1>'
        '<p class="orch-sub">agent reasoning traces · '
        f"{theme.fmt_num(total_runs)} recorded</p></div></div>"
        '<div class="orch-spend"><span class="label">In view</span>'
        f'<span class="value">{theme.fmt_num(shown_runs)} / '
        f"{theme.fmt_num(total_runs)}</span></div></div>"
    )


def _fmt_cost_usd(amount: float, *, decimals: int = 2) -> str:
    """Render an exact dollar figure with the requested precision."""
    number_format = ",.{0}f".format(decimals)
    return "${0}".format(format(amount, number_format))


def _trajectory_kpis(
    summary: trajectory_reader.TrajectorySummary,
) -> tuple[_TrajectoryKpi, ...]:
    if summary.truncated_runs:
        truncated_foot = f"{theme.fmt_num(summary.truncated_runs)} truncated"
    else:
        truncated_foot = "none truncated"
    return (
        _TrajectoryKpi("Runs", theme.fmt_num(summary.total_runs), truncated_foot),
        _TrajectoryKpi("Issues", theme.fmt_num(summary.distinct_issues)),
        _TrajectoryKpi("Repos", theme.fmt_num(summary.distinct_repos)),
        _TrajectoryKpi("Tool calls", theme.fmt_num(summary.total_tool_calls)),
        _TrajectoryKpi(
            "Total cost",
            _fmt_cost_usd(summary.total_cost_usd),
            "reported + est.",
        ),
    )


def _trajectory_kpi_html(kpi: _TrajectoryKpi) -> str:
    if kpi.foot:
        foot_html = (
            f'<div class="kpi-foot"><span>{html.escape(kpi.foot)}</span></div>'
        )
    else:
        foot_html = '<div class="kpi-foot"></div>'
    return (
        '<div class="orch-kpi"><div class="kpi-top">'
        f'<span class="kpi-label">{html.escape(kpi.label)}</span></div>'
        f'<div class="kpi-value">{html.escape(kpi.figure)}</div>'
        f"{foot_html}</div>"
    )


def _kpi_strip_html(summary: trajectory_reader.TrajectorySummary) -> str:
    """Render the five trajectory KPI tiles."""
    cells = (_trajectory_kpi_html(kpi) for kpi in _trajectory_kpis(summary))
    return '<div class="orch-kpis">{0}</div>'.format("".join(cells))
