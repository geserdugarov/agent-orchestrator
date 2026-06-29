# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Streamlit viewer for the opt-in trajectory sink (`TRAJECTORY_LOG_PATH`).

A deliberately separate web page from the analytics dashboard
(`orchestrator/dashboard.py`), launched the same way:

    uv sync --group dashboard
    uv run streamlit run orchestrator/trajectory_dashboard.py

The two pages are independent on purpose. The analytics dashboard reads
the numeric usage / cost rollup from Postgres; this page reads the local
JSONL trajectory file directly, because the trajectory sink's large
free-text bodies are never replayed into Postgres (see
`docs/observability.md`). Keeping them apart means an operator can run
the trajectory viewer with nothing but the JSONL file on disk -- no
database, no sync -- and the cost dashboard never has to carry the
trajectory bodies.

The page is intentionally minimal-but-useful: a filterable list of the
recorded runs and a per-run detail view that walks the redacted prompt,
offered tools, triggered skills, the ordered tool-call / tool-result
timeline, and the final output. The pure parsing / filtering / summary
logic lives in the import-light `orchestrator.trajectory_reader`; this
module owns only the Streamlit rendering.

Streamlit is imported *lazily* inside `main()` so importing
`orchestrator.trajectory_dashboard` from a test (or any non-dashboard
caller) does not require the optional `dashboard` dependency group --
the same lazy-import invariant `orchestrator.dashboard` holds, asserted
by `tests/test_trajectory_dashboard.py`. The plotly-free
`orchestrator.dashboard_theme` tokens and the import-light reader /
state helpers are imported at module top so the inline-HTML builders can
reuse the dashboard's chrome (CSS variables, fonts, formatters) for a
consistent look across the two pages.
"""
from __future__ import annotations

import html
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

# `streamlit run orchestrator/trajectory_dashboard.py` executes this file as
# a top-level script via `runpy` with no parent package, prepending the
# script's own directory (`orchestrator/`) to `sys.path` rather than the
# repo root -- so `from . import ...` raises and a bare `from orchestrator
# import ...` would fail too. Adding the repo root makes the absolute
# imports below work in both the script-launched and package-imported
# (`import orchestrator.trajectory_dashboard`) contexts. The insert is
# idempotent. This mirrors the identical shim in `orchestrator/dashboard.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator import dashboard_state as dashboard_state  # noqa: E402
from orchestrator import dashboard_theme as theme  # noqa: E402
from orchestrator import trajectory_reader as trajectory_reader  # noqa: E402
from orchestrator.trajectory_reader import TrajectoryRun  # noqa: E402

log = logging.getLogger(__name__)

# Cap the overview table so a large file does not build a multi-thousand-row
# DOM. The run picker still lists every matching run, so nothing is
# unreachable -- the table is the at-a-glance overview, the selectbox is the
# exhaustive index.
RUN_TABLE_LIMIT = 200

NO_TRAJECTORIES_MESSAGE = (
    "No `agent_trajectory` records were found. The trajectory sink writes "
    "one record per tracked agent run once `TRAJECTORY_LOG_PATH` is set and "
    "the orchestrator has run at least one agent. Confirm the path below and "
    "that some workflow activity has happened since the sink was enabled."
)
EMPTY_FILTER_MESSAGE = (
    "No trajectories match the current filters. Clear a filter or broaden "
    "the search to see recorded runs."
)

# Page-specific chrome layered on top of `theme.PAGE_CSS`. References the
# `--orch-*` CSS custom properties that `PAGE_CSS` defines on `:root`, so the
# colors, radii, and fonts stay in lockstep with the analytics dashboard
# instead of being re-hardcoded here. Injected once after `PAGE_CSS`.
EXTRA_CSS = f"""
<style>
  .orch-traj-meta {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 10px; margin: 4px 0 14px;
  }}
  .orch-traj-meta-item {{
    border: 1px solid var(--orch-border); border-radius: 10px;
    padding: 9px 12px; background: var(--orch-card);
  }}
  .orch-traj-meta-item .k {{
    color: var(--orch-muted-soft); font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.05em;
  }}
  .orch-traj-meta-item .v {{
    color: var(--orch-ink); font-size: 14px; margin-top: 2px;
    font-family: {theme.MONO_FONT_FAMILY}; word-break: break-word;
  }}
  .orch-traj-chips {{
    display: flex; flex-wrap: wrap; gap: 6px; margin: 2px 0 12px;
  }}
  .orch-traj-chips .lbl {{
    color: var(--orch-muted); font-size: 12px; font-weight: 500;
    margin-right: 4px; align-self: center;
  }}
  .orch-traj-chip {{
    background: var(--orch-chip); color: var(--orch-ink);
    border: 1px solid var(--orch-border); border-radius: 999px;
    padding: 2px 10px; font-size: 12px;
    font-family: {theme.MONO_FONT_FAMILY};
  }}
  .orch-traj-table {{
    width: 100%; border-collapse: collapse; font-size: 12.5px;
    font-family: {theme.FONT_FAMILY};
  }}
  .orch-traj-table th {{
    text-align: left; color: var(--orch-muted); font-weight: 500;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 6px 10px; border-bottom: 1px solid var(--orch-border);
  }}
  .orch-traj-table td {{
    padding: 6px 10px; border-bottom: 1px solid var(--orch-grid);
    color: var(--orch-ink);
  }}
  .orch-traj-table td.num {{
    text-align: right; font-family: {theme.MONO_FONT_FAMILY};
  }}
  .orch-traj-step {{
    display: flex; align-items: center; gap: 10px;
    margin: 10px 0 4px;
  }}
  .orch-traj-step-idx {{
    color: var(--orch-muted-soft); font-size: 12px;
    font-family: {theme.MONO_FONT_FAMILY}; min-width: 24px;
  }}
  .orch-traj-badge {{
    font-size: 11px; font-weight: 600; padding: 2px 9px;
    border-radius: 6px; white-space: nowrap;
    font-family: {theme.MONO_FONT_FAMILY};
  }}
  .orch-traj-badge.call {{
    background: rgba(91,84,224,.12); color: var(--orch-accent);
  }}
  .orch-traj-badge.result {{
    background: rgba(26,163,154,.14); color: var(--orch-cache);
  }}
  .orch-traj-step-name {{
    color: var(--orch-ink); font-weight: 600; font-size: 13px;
    font-family: {theme.MONO_FONT_FAMILY};
  }}
  .orch-traj-step-id {{
    color: var(--orch-muted-soft); font-size: 11px;
    font-family: {theme.MONO_FONT_FAMILY}; margin-left: auto;
  }}
</style>
"""


def _card_header_html(title: str, sub: str) -> str:
    """Card title + subtitle, reusing the dashboard's `.orch-card-*` styles."""
    return (
        f'<p class="orch-card-title">{html.escape(title)}</p>'
        f'<p class="orch-card-sub">{html.escape(sub)}</p>'
    )


def _topbar_html(total_runs: int, shown_runs: int) -> str:
    """Sticky topbar mirroring the analytics dashboard's brand bar.

    The right-hand pill reports how many runs the active filters surface
    out of the file's total, the trajectory analogue of the dashboard's
    in-range spend pill.
    """
    return (
        '<div class="orch-topbar">'
        '<div class="orch-brand">'
        '<span class="orch-brand-mark">TR</span>'
        '<div>'
        '<h1>Orchestrator Trajectories</h1>'
        '<p class="orch-sub">agent reasoning traces · '
        f'{theme.fmt_num(total_runs)} recorded</p>'
        '</div>'
        '</div>'
        '<div class="orch-spend">'
        '<span class="label">In view</span>'
        f'<span class="value">{theme.fmt_num(shown_runs)} / '
        f'{theme.fmt_num(total_runs)}</span>'
        '</div>'
        '</div>'
    )


def _kpi_strip_html(summary: trajectory_reader.TrajectorySummary) -> str:
    """Four-tile KPI strip reusing the dashboard's `.orch-kpi` markup."""
    truncated_foot = (
        f"{theme.fmt_num(summary.truncated_runs)} truncated"
        if summary.truncated_runs
        else "none truncated"
    )
    tiles = [
        ("Runs", theme.fmt_num(summary.total_runs), truncated_foot),
        ("Issues", theme.fmt_num(summary.distinct_issues), ""),
        ("Repos", theme.fmt_num(summary.distinct_repos), ""),
        ("Tool calls", theme.fmt_num(summary.total_tool_calls), ""),
    ]
    cells = []
    for label, value, foot in tiles:
        foot_html = (
            f'<div class="kpi-foot"><span>{html.escape(foot)}</span></div>'
            if foot
            else '<div class="kpi-foot"></div>'
        )
        cells.append(
            '<div class="orch-kpi">'
            f'<div class="kpi-top"><span class="kpi-label">'
            f'{html.escape(label)}</span></div>'
            f'<div class="kpi-value">{html.escape(value)}</div>'
            f'{foot_html}'
            '</div>'
        )
    return f'<div class="orch-kpis">{"".join(cells)}</div>'


def _meta_html(run: TrajectoryRun) -> str:
    """Per-run metadata grid. Only non-empty fields render a tile."""
    items: list[tuple[str, str]] = [
        ("Repo", run.repo),
        ("Issue", f"#{run.issue}" if run.issue else ""),
        ("Stage", run.stage),
        ("Agent role", run.agent_role),
        ("Backend", run.backend),
        (
            "Review round",
            str(run.review_round) if run.review_round is not None else "",
        ),
        (
            "Retry count",
            str(run.retry_count) if run.retry_count is not None else "",
        ),
        ("Session", run.session_id),
        ("Recorded", run.ts),
    ]
    cells = [
        '<div class="orch-traj-meta-item">'
        f'<div class="k">{html.escape(k)}</div>'
        f'<div class="v">{html.escape(v)}</div>'
        '</div>'
        for k, v in items
        if v
    ]
    return f'<div class="orch-traj-meta">{"".join(cells)}</div>'


def _labeled_chips_html(label: str, names: Sequence[str]) -> str:
    """A label followed by a pill per name; empty `names` yields ''."""
    if not names:
        return ""
    chips = "".join(
        f'<span class="orch-traj-chip">{html.escape(n)}</span>' for n in names
    )
    return (
        '<div class="orch-traj-chips">'
        f'<span class="lbl">{html.escape(label)}</span>{chips}'
        '</div>'
    )


def _runs_table_html(runs: Sequence[TrajectoryRun]) -> str:
    """Compact overview table of the (already-sliced) run list."""
    headers = (
        "Issue", "Repo", "Stage", "Role", "Backend",
        "Round", "Steps", "Tool calls", "Recorded",
    )
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    rows = []
    for r in runs:
        round_cell = "" if r.review_round is None else str(r.review_round)
        rows.append(
            "<tr>"
            f'<td class="num">#{html.escape(str(r.issue))}</td>'
            f"<td>{html.escape(r.repo)}</td>"
            f"<td>{html.escape(r.stage)}</td>"
            f"<td>{html.escape(r.agent_role)}</td>"
            f"<td>{html.escape(r.backend)}</td>"
            f'<td class="num">{html.escape(round_cell)}</td>'
            f'<td class="num">{html.escape(str(r.step_count))}</td>'
            f'<td class="num">{html.escape(str(r.tool_calls))}</td>'
            f"<td>{html.escape(r.ts)}</td>"
            "</tr>"
        )
    return (
        '<table class="orch-traj-table">'
        f"<thead><tr>{head}</tr></thead>"
        f'<tbody>{"".join(rows)}</tbody>'
        "</table>"
    )


def _step_header_html(step: trajectory_reader.TrajectoryStepView, index: int) -> str:
    """One timeline row: index, a call/result badge, the tool, the id."""
    if step.is_call:
        badge_class, badge_text = "call", "tool call"
    elif step.is_result:
        badge_class, badge_text = "result", "tool result"
    else:
        badge_class, badge_text = "result", html.escape(step.kind or "step")
    name_html = (
        f'<span class="orch-traj-step-name">{html.escape(step.name)}</span>'
        if step.name
        else ""
    )
    id_html = (
        f'<span class="orch-traj-step-id">{html.escape(step.tool_id)}</span>'
        if step.tool_id
        else ""
    )
    return (
        '<div class="orch-traj-step">'
        f'<span class="orch-traj-step-idx">{index + 1}</span>'
        f'<span class="orch-traj-badge {badge_class}">{badge_text}</span>'
        f'{name_html}{id_html}'
        '</div>'
    )


def _render_run(*, st: Any, run: TrajectoryRun) -> None:
    """Render the detail card for one selected run."""
    with st.container(border=True):
        st.markdown('<div class="orch-cardmark"></div>', unsafe_allow_html=True)
        st.markdown(
            _card_header_html(
                f"Run #{run.issue} · {run.repo or 'unknown repo'}",
                "Redacted prompt, tool-call timeline, and final output",
            ),
            unsafe_allow_html=True,
        )
        if run.truncated:
            st.warning(
                "This trajectory was truncated by the sink's record budget; "
                "later steps were dropped before the run finished."
            )
        st.markdown(_meta_html(run), unsafe_allow_html=True)

        for label, names in (
            ("Tools offered", run.tools),
            ("Skills triggered", run.skills_triggered),
            ("Skills available", run.skills_available),
        ):
            chips = _labeled_chips_html(label, names)
            if chips:
                st.markdown(chips, unsafe_allow_html=True)

        if run.user_input:
            with st.expander("User input (prompt)", expanded=False):
                st.code(run.user_input)
        if run.system_prompt:
            with st.expander("System prompt", expanded=False):
                st.code(run.system_prompt)

        st.markdown(
            '<p class="orch-card-sub" style="margin-top:14px">'
            f'Step timeline · {run.step_count} steps · '
            f'{run.tool_calls} tool calls</p>',
            unsafe_allow_html=True,
        )
        if run.steps:
            for i, step in enumerate(run.steps):
                st.markdown(
                    _step_header_html(step, i), unsafe_allow_html=True
                )
                if step.content:
                    st.code(step.content)
        else:
            st.caption("No tool calls were recorded for this run.")

        if run.output:
            st.markdown(
                '<p class="orch-card-sub" style="margin-top:14px">'
                'Final output</p>',
                unsafe_allow_html=True,
            )
            st.markdown(run.output)


def main() -> None:
    """Streamlit entrypoint.

    Imports Streamlit lazily so the orchestrator polling path (and tests
    that just import this module) never pull the optional `dashboard`
    group in. Run via `streamlit run orchestrator/trajectory_dashboard.py`;
    Streamlit invokes the script with `__name__ == "__main__"`, which
    falls through to the sentinel at the bottom of this file.
    """
    import streamlit as st

    st.set_page_config(
        page_title="Orchestrator Trajectories",
        layout="wide",
    )
    st.markdown(theme.PAGE_CSS, unsafe_allow_html=True)
    st.markdown(EXTRA_CSS, unsafe_allow_html=True)

    unset = trajectory_reader.log_unconfigured_message()
    if unset:
        st.markdown(_topbar_html(0, 0), unsafe_allow_html=True)
        st.warning(unset)
        st.stop()

    log_path = trajectory_reader.resolve_log_path()
    runs = trajectory_reader.read_trajectories()
    total = len(runs)
    options = trajectory_reader.filter_options(runs)

    with st.sidebar:
        st.header("Filters")
        repo_options = ("All", *options.repos)
        repo_choice = st.selectbox("Repo", repo_options, index=0)
        backend_choice = st.multiselect(
            "Backend",
            list(options.backends),
            help="Leave empty to include every backend.",
        )
        role_choice = st.multiselect(
            "Agent role",
            list(options.agent_roles),
            help="Leave empty to include every role.",
        )
        stage_choice = st.multiselect(
            "Stage",
            list(options.stages),
            help="Leave empty to include every stage.",
        )
        issue_input = st.text_input(
            "Issue number",
            value="",
            help="Enter `123` or `#123` to narrow to one issue.",
        )
        query_input = st.text_input(
            "Search",
            value="",
            help=(
                "Case-insensitive substring matched across the prompt, "
                "system prompt, output, tool names, tool payloads, and "
                "skill names."
            ),
        )

    shown = trajectory_reader.filter_runs(
        runs,
        repo=None if repo_choice == "All" else repo_choice,
        backends=backend_choice or None,
        agent_roles=role_choice or None,
        stages=stage_choice or None,
        issue=dashboard_state.parse_issue_number(issue_input),
        query=query_input,
    )
    summary = trajectory_reader.summarize(shown)

    st.markdown(_topbar_html(total, len(shown)), unsafe_allow_html=True)

    if total == 0:
        st.info(NO_TRAJECTORIES_MESSAGE)
        if log_path is not None:
            st.caption(f"Reading `{log_path}`.")
        return

    st.markdown(_kpi_strip_html(summary), unsafe_allow_html=True)

    if not shown:
        st.info(EMPTY_FILTER_MESSAGE)
        return

    # ── Run list ─────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown('<div class="orch-cardmark"></div>', unsafe_allow_html=True)
        st.markdown(
            _card_header_html(
                "Recorded runs",
                "Most recent first · pick a run below to inspect it",
            ),
            unsafe_allow_html=True,
        )
        table_runs = shown[:RUN_TABLE_LIMIT]
        st.markdown(_runs_table_html(table_runs), unsafe_allow_html=True)
        if len(shown) > RUN_TABLE_LIMIT:
            st.caption(
                f"Table shows the {RUN_TABLE_LIMIT} most recent of "
                f"{len(shown)} matching runs; the picker below lists all of "
                "them. Narrow the filters to shorten the list."
            )

    # ── Selected-run detail ──────────────────────────────────────
    labels = [r.label() for r in shown]
    selected = st.selectbox(
        "Inspect run",
        range(len(shown)),
        format_func=lambda i: labels[i],
    )
    _render_run(st=st, run=shown[selected])

    st.markdown(
        '<div class="orch-foot">'
        f'{theme.fmt_num(len(shown))} of {theme.fmt_num(total)} recorded '
        f'trajectories · reading {html.escape(str(log_path))}'
        '</div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
