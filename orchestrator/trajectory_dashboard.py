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

The page is intentionally minimal-but-useful: a foldable list of the
recorded runs, a cascading repo -> issue -> run picker, and a per-run
detail view that walks the run's normalised `timeline` -- the redacted
prompt, then the interleaved assistant / user text turns and tool calls
/ results, then the final output, as one ordered sequence -- alongside
the offered tools and triggered skills. A sidebar toggle hides the
synthetic test-suite fixtures the reader's `is_fixture` marker flags
(off by default; when shown they are tagged in the overview table and
the run picker). The pure parsing / filtering /
summary / timeline logic lives in the import-light
`orchestrator.trajectory_reader`; this module owns only the Streamlit
rendering.

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
  .orch-traj-badge.prompt {{
    background: rgba(86,93,114,.12); color: var(--orch-muted);
  }}
  .orch-traj-badge.assistant {{
    background: rgba(224,145,58,.14); color: var(--orch-output);
  }}
  .orch-traj-badge.user {{
    background: rgba(91,108,240,.12); color: var(--orch-input);
  }}
  .orch-traj-badge.output {{
    background: rgba(47,158,107,.14); color: var(--orch-success);
  }}
  .orch-traj-fixture-tag {{
    display: inline-block; margin-left: 6px;
    background: rgba(224,145,58,.14); color: var(--orch-warn);
    border: 1px solid rgba(224,145,58,.30); border-radius: 999px;
    padding: 0 7px; font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.04em;
    font-family: {theme.MONO_FONT_FAMILY};
  }}
  .orch-traj-table tr.fixture td {{ color: var(--orch-muted-soft); }}
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
        row_class = ' class="fixture"' if r.is_fixture else ""
        fixture_tag = (
            '<span class="orch-traj-fixture-tag">fixture</span>'
            if r.is_fixture
            else ""
        )
        rows.append(
            f"<tr{row_class}>"
            f'<td class="num">#{html.escape(str(r.issue))}</td>'
            f"<td>{html.escape(r.repo)}{fixture_tag}</td>"
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


# Maps a timeline entry's `kind` to its (CSS modifier, badge label).
# `tool_call` / `tool_result` keep the call / result badges the
# steps-only timeline used; the prompt / output brackets and the
# assistant / user text turns each get their own so the operator can
# tell the conversation's voices apart at a glance. Any unknown kind
# falls through to the neutral result badge carrying the raw kind.
_BADGE_BY_KIND: dict[str, tuple[str, str]] = {
    trajectory_reader.TIMELINE_PROMPT: ("prompt", "prompt"),
    trajectory_reader.TIMELINE_OUTPUT: ("output", "final output"),
    "tool_call": ("call", "tool call"),
    "tool_result": ("result", "tool result"),
    "assistant_message": ("assistant", "assistant"),
    "user_message": ("user", "user turn"),
}

# Picker-label prefix flagging a synthetic test fixture, so the operator
# can tell the inherited test-suite records from real runs in the run
# selector the same way the overview table's `fixture` tag does.
_FIXTURE_LABEL_PREFIX = "[fixture] "


def _timeline_entry_html(
    entry: trajectory_reader.TimelineEntry, index: int
) -> str:
    """One timeline row: index, a per-kind badge, the tool name, the id.

    Renders any `TimelineEntry` -- the prompt / output brackets, the
    assistant / user text turns, and the tool calls / results -- by its
    `kind`, so `_render_run` can walk a run's whole ordered timeline with
    one builder instead of bracketing the steps by hand.
    """
    badge_class, badge_text = _BADGE_BY_KIND.get(
        entry.kind, ("result", entry.kind or "step")
    )
    name_html = (
        f'<span class="orch-traj-step-name">{html.escape(entry.name)}</span>'
        if entry.name
        else ""
    )
    id_html = (
        f'<span class="orch-traj-step-id">{html.escape(entry.tool_id)}</span>'
        if entry.tool_id
        else ""
    )
    return (
        '<div class="orch-traj-step">'
        f'<span class="orch-traj-step-idx">{index + 1}</span>'
        f'<span class="orch-traj-badge {badge_class}">'
        f'{html.escape(badge_text)}</span>'
        f'{name_html}{id_html}'
        '</div>'
    )


def _run_picker_label(run: TrajectoryRun) -> str:
    """The run's per-run picker label (`detail_label`), prefixed when it
    is a synthetic fixture.

    The repo and issue live in their own cascading selectors above this
    one, so the per-run picker shows only the `detail_label` cohort
    (stage/role · backend · round · ts), not the full `label`.
    """
    label = run.detail_label()
    return f"{_FIXTURE_LABEL_PREFIX}{label}" if run.is_fixture else label


def _render_run(*, st: Any, run: TrajectoryRun) -> None:
    """Render the detail card for one selected run."""
    with st.container(border=True):
        st.markdown('<div class="orch-cardmark"></div>', unsafe_allow_html=True)
        st.markdown(
            _card_header_html(
                f"Run #{run.issue} · {run.repo or 'unknown repo'}",
                "Ordered timeline: prompt, text turns, tool calls, output",
            ),
            unsafe_allow_html=True,
        )
        if run.is_fixture:
            st.info(
                "This run is flagged as a likely synthetic test fixture "
                "(a sentinel `ignored` prompt, a `sess-*` session id, or a "
                "Skill-only run). Such records can appear in a trajectory "
                "file inherited from a run with the sink enabled during the "
                "test suite."
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

        if run.system_prompt:
            with st.expander("System prompt", expanded=False):
                st.code(run.system_prompt)

        st.markdown(
            '<p class="orch-card-sub" style="margin-top:14px">'
            f'Trajectory timeline · {run.step_count} steps · '
            f'{run.tool_calls} tool calls</p>',
            unsafe_allow_html=True,
        )
        timeline = run.timeline
        if timeline:
            for i, entry in enumerate(timeline):
                st.markdown(
                    _timeline_entry_html(entry, i), unsafe_allow_html=True
                )
                if entry.content:
                    # The final answer is markdown the agent authored;
                    # render it rich. Every other entry -- the orchestrator
                    # prompt, tool payloads, text turns -- is raw text shown
                    # verbatim in a code block.
                    if entry.is_output:
                        st.markdown(entry.content)
                    else:
                        st.code(entry.content)
        else:
            st.caption("No timeline entries were recorded for this run.")


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
        hide_fixtures = st.checkbox(
            "Hide synthetic fixtures",
            value=False,
            help=(
                "Drop records that look like test-suite fixtures -- a "
                "sentinel `ignored` prompt, a `sess-*` session id, or a "
                "Skill-only run. Leave off to keep them, flagged with a "
                "`fixture` tag in the table and run picker."
            ),
        )

    fixture_total = sum(1 for r in runs if r.is_fixture)

    shown = trajectory_reader.filter_runs(
        runs,
        repo=None if repo_choice == "All" else repo_choice,
        backends=backend_choice or None,
        agent_roles=role_choice or None,
        stages=stage_choice or None,
        issue=dashboard_state.parse_issue_number(issue_input),
        query=query_input,
        exclude_fixtures=hide_fixtures,
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
    # A native expander so the operator can fold the overview table away
    # and focus on the inspected run; expanded by default to preserve the
    # at-a-glance view.
    with st.expander("Recorded runs", expanded=True):
        st.caption("Most recent first · pick a run below to inspect it")
        table_runs = shown[:RUN_TABLE_LIMIT]
        st.markdown(_runs_table_html(table_runs), unsafe_allow_html=True)
        if len(shown) > RUN_TABLE_LIMIT:
            st.caption(
                f"Table shows the {RUN_TABLE_LIMIT} most recent of "
                f"{len(shown)} matching runs; the picker below lists all of "
                "them. Narrow the filters to shorten the list."
            )
        if fixture_total:
            st.caption(
                f"{fixture_total} synthetic fixture "
                f"{'run' if fixture_total == 1 else 'runs'} hidden."
                if hide_fixtures
                else f"{fixture_total} synthetic fixture "
                f"{'run' if fixture_total == 1 else 'runs'} flagged; "
                "tick *Hide synthetic fixtures* in the sidebar to drop them."
            )

    # ── Selected-run detail ──────────────────────────────────────
    # Three cascading pickers narrow `shown` to one run: repo, then the
    # issue within that repo, then the specific run (by `detail_label`).
    # Streamlit resets a downstream selectbox to its first option when an
    # upstream pick makes its prior value no longer offered.
    st.markdown(
        '<p class="orch-card-sub" style="margin:14px 0 4px">'
        'Inspect run</p>',
        unsafe_allow_html=True,
    )
    repo_col, issue_col, run_col = st.columns(3)
    with repo_col:
        inspect_repos = sorted({r.repo for r in shown})
        picked_repo = st.selectbox("Repo", inspect_repos)
    with issue_col:
        inspect_issues = sorted(
            {r.issue for r in shown if r.repo == picked_repo}
        )
        picked_issue = st.selectbox(
            "Issue", inspect_issues, format_func=lambda i: f"#{i}"
        )
    with run_col:
        candidates = [
            r
            for r in shown
            if r.repo == picked_repo and r.issue == picked_issue
        ]
        selected = st.selectbox(
            "Run",
            range(len(candidates)),
            format_func=lambda i: _run_picker_label(candidates[i]),
        )
    _render_run(st=st, run=candidates[selected])

    st.markdown(
        '<div class="orch-foot">'
        f'{theme.fmt_num(len(shown))} of {theme.fmt_num(total)} recorded '
        f'trajectories · reading {html.escape(str(log_path))}'
        '</div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
