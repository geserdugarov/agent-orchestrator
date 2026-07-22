# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory run metadata, overview table, and picker labels."""
from __future__ import annotations

import html
from typing import Sequence

from orchestrator.trajectory_reader import TrajectoryRun


REPO_LABEL = "Repo"
FIXTURE_LABEL_PREFIX = "[fixture] "


def _meta_html(run: TrajectoryRun) -> str:
    fields: list[tuple[str, str]] = [
        (REPO_LABEL, run.repo),
        ("Issue", f"#{run.issue}" if run.issue else ""),
        ("Stage", run.stage),
        ("Agent role", run.agent_role),
        ("Backend", run.backend),
        ("Review round", "" if run.review_round is None else str(run.review_round)),
        ("Retry count", "" if run.retry_count is None else str(run.retry_count)),
        ("Session", run.session_id),
        ("Recorded", run.ts),
    ]
    cells = [
        '<div class="orch-traj-meta-item">'
        f'<div class="k">{html.escape(label)}</div>'
        f'<div class="v">{html.escape(cell)}</div></div>'
        for label, cell in fields
        if cell
    ]
    return '<div class="orch-traj-meta">{0}</div>'.format("".join(cells))


def _labeled_chips_html(label: str, names: Sequence[str]) -> str:
    if not names:
        return ""
    chips = "".join(
        f'<span class="orch-traj-chip">{html.escape(name)}</span>'
        for name in names
    )
    return (
        '<div class="orch-traj-chips">'
        f'<span class="lbl">{html.escape(label)}</span>{chips}</div>'
    )


def _run_table_row_html(run: TrajectoryRun) -> str:
    round_cell = "" if run.review_round is None else str(run.review_round)
    row_class = ' class="fixture"' if run.is_fixture else ""
    fixture_tag = (
        '<span class="orch-traj-fixture-tag">fixture</span>'
        if run.is_fixture
        else ""
    )
    return (
        f"<tr{row_class}>"
        f'<td class="num">#{html.escape(str(run.issue))}</td>'
        f"<td>{html.escape(run.repo)}{fixture_tag}</td>"
        f"<td>{html.escape(run.stage)}</td>"
        f"<td>{html.escape(run.agent_role)}</td>"
        f"<td>{html.escape(run.backend)}</td>"
        f'<td class="num">{html.escape(round_cell)}</td>'
        f'<td class="num">{html.escape(str(run.step_count))}</td>'
        f'<td class="num">{html.escape(str(run.tool_calls))}</td>'
        f"<td>{html.escape(run.ts)}</td></tr>"
    )


def _runs_table_html(runs: Sequence[TrajectoryRun]) -> str:
    headers = (
        "Issue",
        REPO_LABEL,
        "Stage",
        "Role",
        "Backend",
        "Round",
        "Steps",
        "Tool calls",
        "Recorded",
    )
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    rows_html = "".join(_run_table_row_html(run) for run in runs)
    return (
        '<table class="orch-traj-table"><thead><tr>'
        f"{head}</tr></thead><tbody>{rows_html}</tbody></table>"
    )


def _run_picker_label(run: TrajectoryRun) -> str:
    label = run.detail_label()
    return f"{FIXTURE_LABEL_PREFIX}{label}" if run.is_fixture else label
