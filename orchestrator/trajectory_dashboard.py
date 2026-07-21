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
the offered tools and triggered skills. It also surfaces the run's token
usage and cost: a run-level usage / cost summary in the detail card, a
*Total cost* KPI tile, and -- for claude -- a compact per-turn usage
strip (with cache-hit / read / write indicators) at each assistant-turn
boundary in the timeline. The copy states the usage model's two honesty
points: per-turn figures are claude-only estimates that need not sum to
the run total, and the run cost is authoritative only when reported
(codex has no per-turn detail, so it shows the run summary and a note).
A sidebar toggle hides the
synthetic test-suite fixtures the reader's `is_fixture` marker flags
(off by default; when shown they are tagged in the overview table and
the run picker). The pure parsing / filtering /
summary / timeline logic lives in the import-light
`orchestrator.trajectory_reader`; the pure inline-HTML builders live in
`orchestrator._trajectory_dashboard_html`; this module owns only the
Streamlit rendering.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

# `streamlit run orchestrator/trajectory_dashboard.py` launches this file
# as a top-level script with only `orchestrator/` on `sys.path`, so the
# repo root has to be added before the absolute imports below resolve;
# `orchestrator/script_launch.py` documents why and holds the shared shim
# `orchestrator/dashboard.py` also uses. `__package__` selects the import
# per launch mode: a package import sets it to `"orchestrator"` and takes
# the qualified import, so a stray top-level `script_launch` on `sys.path`
# cannot shadow the helper; a script launch leaves it empty/absent and takes
# the bare `import script_launch`, which loads the helper from the script's
# own directory WITHOUT importing the `orchestrator` package before the repo
# root is on the path.
if __package__:
    from orchestrator.script_launch import ensure_repo_root_on_path
else:  # script-launched: only `orchestrator/` is on sys.path
    from script_launch import ensure_repo_root_on_path

ensure_repo_root_on_path(__file__)

from orchestrator import (  # noqa: E402
    dashboard_state as dashboard_state,
    dashboard_theme as theme,
    trajectory_reader as trajectory_reader,
)
from orchestrator.trajectory_reader import TrajectoryRun  # noqa: E402
from orchestrator._trajectory_dashboard_html import (  # noqa: E402
    EXTRA_CSS,
    _REPO_LABEL,
    _card_header_html,
    _kpi_strip_html,
    _labeled_chips_html,
    _meta_html,
    _topbar_html,
)
from orchestrator._trajectory_dashboard_html import (  # noqa: E402
    _run_picker_label,
    _run_usage_html,
    _runs_table_html,
    _timeline_entry_html,
    _timeline_with_usage,
    _turn_usage_html,
)

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


def _render_run_notices(st: Any, run: TrajectoryRun) -> None:
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


def _render_run_usage_and_chips(st: Any, run: TrajectoryRun) -> None:
    usage_html = _run_usage_html(run)
    if usage_html:
        st.markdown(usage_html, unsafe_allow_html=True)
    for label, names in (
        ("Tools offered", run.tools),
        ("Skills triggered", run.skills_triggered),
        ("Skills available", run.skills_available),
    ):
        chips = _labeled_chips_html(label, names)
        if chips:
            st.markdown(chips, unsafe_allow_html=True)


def _render_system_prompt(st: Any, run: TrajectoryRun) -> None:
    if not run.system_prompt:
        return
    with st.expander("System prompt", expanded=False):
        st.code(run.system_prompt)


def _render_timeline_entry(
    st: Any,
    index: int,
    strip: Optional[trajectory_reader.TurnUsageView],
    entry: trajectory_reader.TimelineEntry,
) -> None:
    if strip is not None:
        st.markdown(_turn_usage_html(strip), unsafe_allow_html=True)
    st.markdown(_timeline_entry_html(entry, index), unsafe_allow_html=True)
    if not entry.content:
        return
    if entry.is_output:
        st.markdown(entry.content)
    else:
        st.code(entry.content)


def _render_timeline(st: Any, run: TrajectoryRun) -> None:
    st.markdown(
        '<p class="orch-card-sub" style="margin-top:14px">'
        f'Trajectory timeline · {run.step_count} steps · '
        f'{run.tool_calls} tool calls</p>',
        unsafe_allow_html=True,
    )
    if not run.timeline:
        st.caption("No timeline entries were recorded for this run.")
        return
    for index, (strip, entry) in enumerate(_timeline_with_usage(run)):
        _render_timeline_entry(st, index, strip, entry)


def _render_run_card(st: Any, run: TrajectoryRun) -> None:
    st.markdown('<div class="orch-cardmark"></div>', unsafe_allow_html=True)
    repo_label = run.repo or "unknown repo"
    st.markdown(
        _card_header_html(
            f"Run #{run.issue} · {repo_label}",
            "Ordered timeline: prompt, text turns, tool calls, output",
        ),
        unsafe_allow_html=True,
    )
    _render_run_notices(st, run)
    st.markdown(_meta_html(run), unsafe_allow_html=True)
    _render_run_usage_and_chips(st, run)
    _render_system_prompt(st, run)
    _render_timeline(st, run)


def _render_run(*, st: Any, run: TrajectoryRun) -> None:
    """Render the detail card for one selected run."""
    with st.container(border=True):
        _render_run_card(st, run)


@dataclass(frozen=True)
class _TrajectoryFilters:
    repo: Optional[str]
    backends: Optional[Sequence[str]]
    agent_roles: Optional[Sequence[str]]
    stages: Optional[Sequence[str]]
    issue: Optional[int]
    query: str
    hide_fixtures: bool


@dataclass(frozen=True)
class _TrajectoryPage:
    log_path: Optional[Path]
    runs: Sequence[TrajectoryRun]
    options: trajectory_reader.FilterOptions
    fixture_total: int

    @property
    def total(self) -> int:
        return len(self.runs)


def _configure_page(st: Any) -> None:
    st.set_page_config(
        page_title="Orchestrator Trajectories",
        layout="wide",
    )
    st.markdown(theme.PAGE_CSS, unsafe_allow_html=True)
    st.markdown(EXTRA_CSS, unsafe_allow_html=True)


def _stop_if_unconfigured(st: Any) -> None:
    message = trajectory_reader.log_unconfigured_message()
    if not message:
        return
    st.markdown(_topbar_html(0, 0), unsafe_allow_html=True)
    st.warning(message)
    st.stop()


def _load_trajectory_page() -> _TrajectoryPage:
    log_path = trajectory_reader.resolve_log_path()
    runs = trajectory_reader.read_trajectories()
    return _TrajectoryPage(
        log_path=log_path,
        runs=runs,
        options=trajectory_reader.filter_options(runs),
        fixture_total=sum(1 for run in runs if run.is_fixture),
    )


def _render_categorical_filters(
    st: Any,
    options: trajectory_reader.FilterOptions,
) -> tuple[Sequence[str], Sequence[str], Sequence[str]]:
    backends = st.multiselect(
        "Backend",
        list(options.backends),
        help="Leave empty to include every backend.",
    )
    roles = st.multiselect(
        "Agent role",
        list(options.agent_roles),
        help="Leave empty to include every role.",
    )
    stages = st.multiselect(
        "Stage",
        list(options.stages),
        help="Leave empty to include every stage.",
    )
    return backends, roles, stages


def _render_text_filters(st: Any) -> tuple[str, str]:
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
    return issue_input, query_input


def _render_trajectory_sidebar(
    st: Any,
    options: trajectory_reader.FilterOptions,
) -> _TrajectoryFilters:
    with st.sidebar:
        st.header("Filters")
        repo_choice = st.selectbox(_REPO_LABEL, ("All", *options.repos), index=0)
        categorical = _render_categorical_filters(st, options)
        text_filters = _render_text_filters(st)
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
    return _TrajectoryFilters(
        repo=None if repo_choice == "All" else repo_choice,
        backends=categorical[0] or None,
        agent_roles=categorical[1] or None,
        stages=categorical[2] or None,
        issue=dashboard_state.parse_issue_number(text_filters[0]),
        query=text_filters[1],
        hide_fixtures=hide_fixtures,
    )


def _filter_page_runs(
    page: _TrajectoryPage,
    filters: _TrajectoryFilters,
) -> list[TrajectoryRun]:
    return trajectory_reader.filter_runs(
        page.runs,
        repo=filters.repo,
        backends=filters.backends,
        agent_roles=filters.agent_roles,
        stages=filters.stages,
        issue=filters.issue,
        query=filters.query,
        exclude_fixtures=filters.hide_fixtures,
    )


def _render_no_trajectories(st: Any, log_path: Optional[Path]) -> None:
    st.info(NO_TRAJECTORIES_MESSAGE)
    if log_path is not None:
        st.caption(f"Reading `{log_path}`.")


def _fixture_caption(fixture_total: int, hide_fixtures: bool) -> str:
    noun = "run" if fixture_total == 1 else "runs"
    if hide_fixtures:
        return f"{fixture_total} synthetic fixture {noun} hidden."
    return (
        f"{fixture_total} synthetic fixture {noun} flagged; "
        "tick *Hide synthetic fixtures* in the sidebar to drop them."
    )


def _render_run_list(
    st: Any,
    shown: Sequence[TrajectoryRun],
    fixture_total: int,
    hide_fixtures: bool,
) -> None:
    with st.expander("Recorded runs", expanded=True):
        st.caption("Most recent first · pick a run below to inspect it")
        st.markdown(
            _runs_table_html(shown[:RUN_TABLE_LIMIT]),
            unsafe_allow_html=True,
        )
        if len(shown) > RUN_TABLE_LIMIT:
            st.caption(
                f"Table shows the {RUN_TABLE_LIMIT} most recent of "
                f"{len(shown)} matching runs; the picker below lists all of "
                "them. Narrow the filters to shorten the list."
            )
        if fixture_total:
            st.caption(_fixture_caption(fixture_total, hide_fixtures))


def _pick_repo(st: Any, shown: Sequence[TrajectoryRun]) -> str:
    repos = sorted({run.repo for run in shown})
    return st.selectbox(_REPO_LABEL, repos)


def _pick_issue(
    st: Any,
    shown: Sequence[TrajectoryRun],
    repo: str,
) -> int:
    issues = sorted({run.issue for run in shown if run.repo == repo})
    return st.selectbox("Issue", issues, format_func=lambda issue: f"#{issue}")


def _pick_run(
    st: Any,
    shown: Sequence[TrajectoryRun],
    repo: str,
    issue: int,
) -> TrajectoryRun:
    candidates = [
        run for run in shown
        if run.repo == repo and run.issue == issue
    ]
    selected = st.selectbox(
        "Run",
        range(len(candidates)),
        format_func=lambda index: _run_picker_label(candidates[index]),
    )
    return candidates[selected]


def _render_run_picker(st: Any, shown: Sequence[TrajectoryRun]) -> None:
    st.markdown(
        '<p class="orch-card-sub" style="margin:14px 0 4px">'
        'Inspect run</p>',
        unsafe_allow_html=True,
    )
    columns = st.columns(3)
    with columns[0]:
        repo = _pick_repo(st, shown)
    with columns[1]:
        issue = _pick_issue(st, shown, repo)
    with columns[2]:
        run = _pick_run(st, shown, repo, issue)
    _render_run(st=st, run=run)


def _render_trajectory_footer(
    st: Any,
    shown_count: int,
    page: _TrajectoryPage,
) -> None:
    st.markdown(
        '<div class="orch-foot">'
        f'{theme.fmt_num(shown_count)} of {theme.fmt_num(page.total)} recorded '
        f'trajectories · reading {html.escape(str(page.log_path))}'
        '</div>',
        unsafe_allow_html=True,
    )


def _render_trajectory_page(
    st: Any,
    page: _TrajectoryPage,
    filters: _TrajectoryFilters,
    shown: Sequence[TrajectoryRun],
) -> None:
    st.markdown(_topbar_html(page.total, len(shown)), unsafe_allow_html=True)
    if page.total == 0:
        _render_no_trajectories(st, page.log_path)
        return
    st.markdown(
        _kpi_strip_html(trajectory_reader.summarize(shown)),
        unsafe_allow_html=True,
    )
    if not shown:
        st.info(EMPTY_FILTER_MESSAGE)
        return
    _render_run_list(st, shown, page.fixture_total, filters.hide_fixtures)
    _render_run_picker(st, shown)
    _render_trajectory_footer(st, len(shown), page)


def main() -> None:
    """Streamlit entrypoint.

    Imports Streamlit lazily so the orchestrator polling path (and tests
    that just import this module) never pull the optional `dashboard`
    group in. Run via `streamlit run orchestrator/trajectory_dashboard.py`;
    Streamlit invokes the script with `__name__ == "__main__"`, which
    falls through to the sentinel at the bottom of this file.
    """
    import streamlit as st

    _configure_page(st)
    _stop_if_unconfigured(st)
    page = _load_trajectory_page()
    filters = _render_trajectory_sidebar(st, page.options)
    shown = _filter_page_runs(page, filters)
    _render_trajectory_page(st, page, filters, shown)


if __name__ == "__main__":
    main()
