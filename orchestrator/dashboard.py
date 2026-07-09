# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Streamlit analytics dashboard -- page orchestration.

Renders the redesigned `Orchestrator Analytics` page (#341) over the
read model populated by `orchestrator.analytics.sync`. The layout
mirrors the standalone HTML mock the issue ships:

- A top bar with the page title, the data extent / repo / event
  summary, and the in-range spend pill.
- A filter bar carrying the `3D` / `7D` / `All` preset selector and
  the two-date custom range.
- Computed insight banners (failure rate, unpriced cost coverage).
- A four-tile KPI strip (total spend, total tokens, cost / resolved
  issue, rework share) with previous-window deltas.
- A grid of cards: hero spend / token usage stacked-area chart,
  per-stage cost bars, per-review-cycle cost bars, top-cost issues
  table, per-backend efficiency cards + cost-source coverage bar,
  per-repo cost bars, reliability tiles + resolved-per-day chart,
  weekday-by-hour activity heatmap.
- Per-issue drill-down at the bottom when an issue number is
  entered in the sidebar.

The pure helpers behind this page live in focused modules so this
file stays the Streamlit orchestration layer:

- `orchestrator.dashboard_state` -- date / window math, preset and
  timezone vocabulary, stage-filter / cache-key resolution, the
  issue-number parser, the DB-config banner check, and the read
  fan-out switch.
- `orchestrator.dashboard_kpis` -- KPI delta math, the computed
  insight banners, the reliability-tile triples, the top-cost issue
  ordering, and the rework-share aggregation.
- `orchestrator.dashboard_html` -- the inline-HTML builders for the
  topbar, filter meta, KPI strip, insight stack, per-card header,
  sparkline / delta pill, the issues / skill-trigger tables, the
  per-skill trigger matrix, the backend-efficiency card, the
  cost-coverage bar, and the reliability-tile strip.

`main()` is a thin orchestrator: it delegates the static-metadata read
(`_read_static_metadata`), the staged widget fan-out (`_widget_readers`
+ `_dispatch_reads`), the load-timing log (`_log_dashboard_load`), the
empty / error states (`_render_no_data`, `_render_empty_window`), and
every filter / widget section (the `_render_*` helpers) to focused
module-level helpers, so it reads as a top-to-bottom sequence of calls
rather than a single 1000-line function.

Every pure helper from those three modules is re-exported below under
its original name so `streamlit run orchestrator/dashboard.py`, the
historical `orchestrator.dashboard.*` helper surface, and the existing
dashboard tests keep working without touching the extracted modules.
The `main()`-support helpers (the read / dispatch / logging helpers and
the `_render_*` render helpers) are defined in this module, are not
re-exported, and stay module-private.

Reads go through `orchestrator.analytics.read` (which already
handles unset DB, connection errors, and lazy psycopg import) and
are wrapped in `st.cache_data` keyed by `(start, end, repo, events,
stages, issue)` so every widget sees the same window. The data-
extent and filter-option reads have no filter inputs and are cached
under a longer 5-minute TTL (`STATIC_METADATA_TTL_SECONDS`) so the
sidebar / topbar do not re-pay a fresh round-trip on every rerun.

The widget reads are dispatched in two staged waves so the topbar
and KPI strip paint as soon as their inputs are available instead
of blocking on every widget: the first wave covers `summary`,
`prev_summary`, `ts_points`, `throughput_rows`, `review_round_rows`,
and `cost_coverage_rows` (the only reads the topbar / filter meta
/ insight banners / KPI strip consume), and the second wave covers
the nine remaining widget reads (including the skill-trigger
aggregate and the per-skill trigger matrix). Worker threads only
return data back to the main render thread; every `st` / placeholder
write runs on the main thread.

Streamlit (and its transitive pandas), `plotly`, the chart builders
in `orchestrator.dashboard_charts`, and the theme tokens in
`orchestrator.dashboard_theme` are imported *lazily* inside `main()`
so the polling tick's `orchestrator.*` import surface stays free of
the dashboard's dependency footprint. The module loads without
`streamlit` or `plotly` installed -- only `streamlit run
orchestrator/dashboard.py` (or a direct `main()` call) materializes
the imports. The extracted helper modules are import-light (stdlib
plus `orchestrator.analytics`) so they preserve this invariant; it
is asserted by `tests/test_dashboard.py`.

Run:
    uv sync --group dashboard
    uv run streamlit run orchestrator/dashboard.py
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from time import perf_counter
from typing import Any, Callable, Optional, Sequence

# `streamlit run orchestrator/dashboard.py` launches this file as a
# top-level script with only `orchestrator/` on `sys.path`, so the repo
# root has to be added before the absolute imports below resolve;
# `orchestrator/script_launch.py` documents why. `__package__` selects the
# import per launch mode: a package import (`import orchestrator.dashboard`)
# sets it to `"orchestrator"` and takes the qualified import, so a stray
# top-level `script_launch` on `sys.path` cannot shadow the helper; a script
# launch leaves it empty/absent and takes the bare `import script_launch`,
# which loads the helper from the script's own directory WITHOUT importing
# the `orchestrator` package before the repo root is on the path (doing so
# would bind the parent to any stale/installed copy already importable).
if globals().get("__package__"):
    from orchestrator.script_launch import ensure_repo_root_on_path
else:  # script-launched: only `orchestrator/` is on sys.path
    from script_launch import ensure_repo_root_on_path

ensure_repo_root_on_path(__file__)

from orchestrator import analytics as analytics  # noqa: E402
from orchestrator.analytics import read as analytics_read  # noqa: E402
from orchestrator.analytics.read import (  # noqa: E402
    CostCoverageRow as CostCoverageRow,
    DataExtent as DataExtent,
    IssueSummaryRow as IssueSummaryRow,
    SkillTriggerMatrixRow as SkillTriggerMatrixRow,
    SkillTriggerRateRow as SkillTriggerRateRow,
    Summary as Summary,
)

# Compatibility re-exports. The pure helpers moved to the focused
# `dashboard_state` / `dashboard_kpis` / `dashboard_html` modules; we
# import each one back under its original name so `main()` calls them
# as bare names, the historical `orchestrator.dashboard.*` surface
# stays intact, and the existing tests (which reach the helpers via
# `dashboard.<name>` and inspect `main()`'s source) keep working. The
# redundant `as` alias marks each as an intentional re-export so ruff
# does not flag the unused import; the E402 suppression covers the
# post-`sys.path` placement the script-launch fix forces.
from orchestrator.dashboard_state import (  # noqa: E402
    DASHBOARD_PARALLEL_READS as DASHBOARD_PARALLEL_READS,
    DEFAULT_PRESET as DEFAULT_PRESET,
    DEFAULT_TZ_OFFSET_HOURS as DEFAULT_TZ_OFFSET_HOURS,
    DEFAULT_WINDOW_DAYS as DEFAULT_WINDOW_DAYS,
    DateWindow as DateWindow,
    PARALLEL_READS_ENV as PARALLEL_READS_ENV,
    PARALLEL_READS_MAX_WORKERS as PARALLEL_READS_MAX_WORKERS,
    PRESET_3D as PRESET_3D,
    PRESET_7D as PRESET_7D,
    PRESET_ALL as PRESET_ALL,
    PRESET_CUSTOM as PRESET_CUSTOM,
    PRESET_DAYS as PRESET_DAYS,
    PRESET_INLINE_LABELS as PRESET_INLINE_LABELS,
    PRESET_LABELS as PRESET_LABELS,
    PRESET_OPTIONS as PRESET_OPTIONS,
    TZ_OFFSET_OPTIONS as TZ_OFFSET_OPTIONS,
    UNCONFIGURED_DB_MESSAGE as UNCONFIGURED_DB_MESSAGE,
    _TRUTHY as _TRUTHY,
    _extent_dates as _extent_dates,
    _fan_out_reads as _fan_out_reads,
    _parse_parallel_reads_flag as _parse_parallel_reads_flag,
    cache_key as cache_key,
    dashboard_parallel_reads_enabled as dashboard_parallel_reads_enabled,
    db_unconfigured_message as db_unconfigured_message,
    default_date_range as default_date_range,
    format_tz_offset as format_tz_offset,
    parse_issue_number as parse_issue_number,
    preset_window as preset_window,
    previous_window as previous_window,
    resolve_stage_filter as resolve_stage_filter,
    shift_ts as shift_ts,
    to_window as to_window,
)
from orchestrator.dashboard_kpis import (  # noqa: E402
    DEFAULT_EXPENSIVE_LIMIT as DEFAULT_EXPENSIVE_LIMIT,
    FAILURE_RATE_BANNER_THRESHOLD as FAILURE_RATE_BANNER_THRESHOLD,
    REWORK_BUCKETS as REWORK_BUCKETS,
    UNPRICED_COST_SOURCES as UNPRICED_COST_SOURCES,
    UNPRICED_COVERAGE_THRESHOLD as UNPRICED_COVERAGE_THRESHOLD,
    InsightBanner as InsightBanner,
    compute_insights as compute_insights,
    kpi_delta as kpi_delta,
    reliability_tile_data as reliability_tile_data,
    rework_totals as rework_totals,
    top_expensive_issues as top_expensive_issues,
)
from orchestrator.dashboard_html import (  # noqa: E402
    _backend_efficiency_card_html as _backend_efficiency_card_html,
    _card_header_html as _card_header_html,
    _cost_coverage_bar_html as _cost_coverage_bar_html,
    _delta_pill as _delta_pill,
    _filter_meta_html as _filter_meta_html,
    _insights_html as _insights_html,
    _issues_table_html as _issues_table_html,
    _kpi_strip_html as _kpi_strip_html,
    _reliability_tiles_html as _reliability_tiles_html,
    _skill_matrix_html as _skill_matrix_html,
    _skill_triggers_html as _skill_triggers_html,
    _sparkline_svg as _sparkline_svg,
    _topbar_html as _topbar_html,
    parse_skill_matrix_sort as parse_skill_matrix_sort,
)

# Canonical inventory of the `orchestrator.dashboard.*` surface: the page
# entrypoint (`main`) and its drill-down helper, the page-level constants
# defined below, and every pure helper re-exported above from
# `dashboard_state` / `dashboard_kpis` / `dashboard_html` (plus the
# `analytics` / `analytics_read` module handles). Keeping the list explicit
# makes the compatibility surface auditable in one place and governs
# `from orchestrator.dashboard import *`.
__all__ = [
    "CostCoverageRow",
    "DASHBOARD_PARALLEL_READS",
    "DEFAULT_EXPENSIVE_LIMIT",
    "DEFAULT_PRESET",
    "DEFAULT_RECENT_AGENT_EXITS",
    "DEFAULT_TZ_OFFSET_HOURS",
    "DEFAULT_WINDOW_DAYS",
    "DataExtent",
    "DateWindow",
    "EMPTY_WINDOW_MESSAGE",
    "FAILURE_RATE_BANNER_THRESHOLD",
    "InsightBanner",
    "IssueSummaryRow",
    "LOADING_INDICATOR_MESSAGE",
    "NO_DATA_MESSAGE",
    "PARALLEL_READS_ENV",
    "PARALLEL_READS_MAX_WORKERS",
    "PLOTLY_CONFIG",
    "PRESET_3D",
    "PRESET_7D",
    "PRESET_ALL",
    "PRESET_CUSTOM",
    "PRESET_DAYS",
    "PRESET_INLINE_LABELS",
    "PRESET_LABELS",
    "PRESET_OPTIONS",
    "REWORK_BUCKETS",
    "STATIC_METADATA_TTL_SECONDS",
    "SkillTriggerMatrixRow",
    "SkillTriggerRateRow",
    "Summary",
    "TZ_OFFSET_OPTIONS",
    "UNCONFIGURED_DB_MESSAGE",
    "UNPRICED_COST_SOURCES",
    "UNPRICED_COVERAGE_THRESHOLD",
    "_TRUTHY",
    "_backend_efficiency_card_html",
    "_card_header_html",
    "_cost_coverage_bar_html",
    "_delta_pill",
    "_extent_dates",
    "_fan_out_reads",
    "_filter_meta_html",
    "_insights_html",
    "_issues_table_html",
    "_kpi_strip_html",
    "_parse_parallel_reads_flag",
    "_reliability_tiles_html",
    "_render_drilldown",
    "_skill_matrix_html",
    "_skill_triggers_html",
    "_sparkline_svg",
    "_topbar_html",
    "analytics",
    "analytics_read",
    "cache_key",
    "compute_insights",
    "dashboard_parallel_reads_enabled",
    "db_unconfigured_message",
    "default_date_range",
    "format_tz_offset",
    "kpi_delta",
    "main",
    "parse_issue_number",
    "parse_skill_matrix_sort",
    "preset_window",
    "previous_window",
    "reliability_tile_data",
    "resolve_stage_filter",
    "rework_totals",
    "shift_ts",
    "to_window",
    "top_expensive_issues",
]

log = logging.getLogger(__name__)

DEFAULT_RECENT_AGENT_EXITS = 100

# TTL for the data-extent / filter-option reads (`get_data_extent`,
# `get_filter_options`). These reads carry no filter inputs and
# change only as `analytics.sync` ingests fresh events, so they
# tolerate a longer TTL than the 60 s window the per-filter cached
# wrappers use. Five minutes keeps a freshly-synced repo / event
# value reachable within one sync cycle while collapsing the
# topbar / sidebar round-trip on every rerun.
STATIC_METADATA_TTL_SECONDS = 300

LOADING_INDICATOR_MESSAGE = "Loading analytics…"

# Plotly config passed to every `st.plotly_chart` call. Disabling
# the modebar keeps the hover camera/zoom/pan toolbar off the cards
# -- the standalone mock has no chart chrome, and the toolbar pops
# on hover for every chart on the page otherwise.
PLOTLY_CONFIG: dict[str, Any] = {"displayModeBar": False}

NO_DATA_MESSAGE = (
    "No analytics events have been recorded yet. Run "
    "`uv run python -m orchestrator.analytics.sync` after some "
    "workflow activity to populate the dashboard."
)
EMPTY_WINDOW_MESSAGE = (
    "No analytics events match the current filters. Broaden the window "
    "or clear a filter to see activity."
)


def main() -> None:
    """Streamlit entrypoint.

    Imports Streamlit, pandas, plotly, the chart builders, and the
    theme tokens lazily so the orchestrator polling path never pulls
    them in. Run via `streamlit run orchestrator/dashboard.py`;
    Streamlit invokes the script with `__name__ == "__main__"`, which
    falls through to the sentinel at the bottom of this file.
    """
    import pandas as pd
    import streamlit as st

    from orchestrator import dashboard_charts, dashboard_theme as theme

    st.set_page_config(
        page_title="Orchestrator Analytics",
        layout="wide",
    )
    st.markdown(theme.PAGE_CSS, unsafe_allow_html=True)

    unset = db_unconfigured_message()
    if unset:
        st.warning(unset)
        st.stop()

    extent, options = _read_static_metadata(st=st)

    if extent.min_ts is None or extent.max_ts is None:
        _render_no_data(st=st, extent=extent, theme=theme)
        return

    extent_min_d = extent.min_ts.date()
    extent_max_d = extent.max_ts.date()

    repo_choice, event_choice, stage_choice, issue_input = (
        _render_sidebar_filters(st=st, options=options)
    )

    # Timezone selector lives inside the "When agents run" block (see
    # the heatmap card below), but the offset is read here so the
    # second-wave fan-out can bucket the heatmap in the chosen zone.
    # Seeding session_state on first render lets the selectbox default
    # to UTC+7 while subsequent renders read whatever the operator
    # picked. The widget is wired with `key="tz_offset_hours"` further
    # down so it round-trips through this same session_state slot.
    if "tz_offset_hours" not in st.session_state:
        st.session_state.tz_offset_hours = DEFAULT_TZ_OFFSET_HOURS
    tz_offset_choice = int(st.session_state.tz_offset_hours)

    # Topbar: title + spend pill. We render a placeholder spend now
    # so it occupies the right slot; we replace it after the summary
    # query lands below.
    topbar_slot = st.empty()

    # Filter bar: presets + date inputs + range meta inside a bordered
    # container styled as the "Date range" card. `meta_slot` is filled
    # after the summary read lands (the run count is not known yet);
    # `window` feeds every downstream read.
    window, meta_slot = _render_date_filter_bar(
        st=st,
        extent=extent,
        extent_min_d=extent_min_d,
        extent_max_d=extent_max_d,
    )

    repo_filter = None if repo_choice == "All" else repo_choice
    issue_input_parsed = parse_issue_number(issue_input)
    issue_filter = (
        issue_input_parsed if repo_filter is not None else None
    )
    event_filter = list(event_choice)
    stage_filter = resolve_stage_filter(stage_choice, options.stages)

    key, prev_key = _build_read_keys(
        window=window,
        repo_filter=repo_filter,
        event_filter=event_filter,
        stage_filter=stage_filter,
        issue_filter=issue_filter,
    )

    first_wave_readers, second_wave_readers = _widget_readers(
        st=st,
        key=key,
        prev_key=prev_key,
        tz_offset_choice=tz_offset_choice,
    )
    total_reads = len(first_wave_readers) + len(second_wave_readers)
    parallel = dashboard_parallel_reads_enabled()
    load_start = perf_counter()
    # Single inline spinner spans both waves -- the topbar / KPI
    # strip rendered between waves provides progressive feedback
    # while the second wave finishes, and the spinner clears once
    # every widget has its data. UI writes always run on this main
    # thread (the worker threads `_fan_out_reads` spawns only return
    # data back through the futures), so the staged renders below
    # never reach Streamlit from a worker.
    with st.spinner(LOADING_INDICATOR_MESSAGE):
        results = _dispatch_reads(
            first_wave_readers, st=st, parallel=parallel,
        )
        summary = results["summary"]
        prev_summary = results["prev_summary"]
        ts_points = results["ts_points"]
        review_round_rows = results["review_round_rows"]
        throughput_rows = results["throughput_rows"]
        cost_coverage_rows = results["cost_coverage_rows"]

        # Topbar / filter meta paint on the first-wave results so the
        # user sees real content before the second wave fires.
        topbar_slot.markdown(
            _topbar_html(
                extent=extent,
                distinct_repos=summary.distinct_repos,
                total_events=summary.total_events,
                spend_in_range=summary.total_cost_usd,
                fmt_money_exact=theme.fmt_money_exact,
                fmt_num=theme.fmt_num,
            ),
            unsafe_allow_html=True,
        )
        days_in_window = max((window.end - window.start).days, 1)
        meta_slot.markdown(
            _filter_meta_html(
                from_d=window.start.date(),
                to_d=(window.end - timedelta(days=1)).date(),
                days=days_in_window,
                runs=summary.total_agent_runs,
                fmt_num=theme.fmt_num,
            ),
            unsafe_allow_html=True,
        )

        if summary.total_events == 0:
            # Empty window -- skip the second wave entirely; the
            # remaining reads would only paint empty cards.
            _render_empty_window(
                st=st,
                pd=pd,
                load_start=load_start,
                reads=len(first_wave_readers),
                parallel=parallel,
                window=window,
                repo_filter=repo_filter,
                issue_input_parsed=issue_input_parsed,
                event_filter=event_filter,
                stage_filter=stage_filter,
            )
            return

        # Insights + KPI strip ---------------------------------------
        # Token totals include cache_read + cache_write so the
        # headline figure matches the standalone mock's
        # `input + output + cache_read + cache_write` accounting; the
        # `cached_tokens` cumulative column is deliberately excluded
        # so the cache band is not double-counted.
        banners = compute_insights(
            summary,
            cost_coverage_rows=cost_coverage_rows,
        )
        if banners:
            st.markdown(_insights_html(banners), unsafe_allow_html=True)
        total_cost = float(summary.total_cost_usd or 0.0)
        total_tokens = int(
            (summary.total_input_tokens or 0)
            + (summary.total_output_tokens or 0)
            + (summary.total_cache_read_tokens or 0)
            + (summary.total_cache_write_tokens or 0)
        )
        total_cost_prev = float(prev_summary.total_cost_usd or 0.0)
        total_tokens_prev = int(
            (prev_summary.total_input_tokens or 0)
            + (prev_summary.total_output_tokens or 0)
            + (prev_summary.total_cache_read_tokens or 0)
            + (prev_summary.total_cache_write_tokens or 0)
        )
        resolved = sum(int(r.resolved or 0) for r in throughput_rows)
        rejected = sum(int(r.rejected or 0) for r in throughput_rows)
        rr_total_cost, rr_rework_cost = rework_totals(review_round_rows)
        rework_share = (
            (rr_rework_cost / rr_total_cost) if rr_total_cost > 0 else 0.0
        )

        # Sparkline series, one entry per day in the window. Daily
        # tokens mirror the KPI accounting and include the cache band.
        days = sorted({p.day for p in ts_points})
        days_index = {d: i for i, d in enumerate(days)}
        daily_cost = [0.0] * len(days)
        daily_tokens = [0.0] * len(days)
        for p in ts_points:
            i = days_index[p.day]
            daily_cost[i] += float(p.cost_usd or 0.0)
            daily_tokens[i] += float(
                (p.input_tokens or 0)
                + (p.output_tokens or 0)
                + (p.cache_read_tokens or 0)
                + (p.cache_write_tokens or 0)
            )
        done_index = {r.day: int(r.resolved or 0) for r in throughput_rows}
        daily_done = [done_index.get(d, 0) for d in days]

        kpis = [
            {
                "label": "Total spend",
                "value": theme.fmt_money_exact(total_cost),
                "delta": kpi_delta(total_cost, total_cost_prev),
                "sub": (
                    f"{theme.fmt_money(total_cost / days_in_window)}/day"
                ),
                "spark": daily_cost,
                "spark_color": theme.ACCENT,
            },
            {
                "label": "Total tokens",
                "value": theme.fmt_tokens(total_tokens),
                "delta": kpi_delta(total_tokens, total_tokens_prev),
                "sub": f"{theme.fmt_tokens(total_tokens / days_in_window)}/day",
                "spark": daily_tokens,
                "spark_color": theme.TOKEN_TYPE_COLORS["Input"],
            },
            {
                "label": "Cost / resolved issue",
                "value": (
                    f"${total_cost / resolved:,.2f}"
                    if resolved > 0 else "—"
                ),
                "delta": None,
                "sub": f"{resolved} resolved · {rejected} rejected",
                "spark": daily_done,
                "spark_color": theme.TOKEN_TYPE_COLORS["Cache"],
            },
            {
                "label": "Rework share",
                "value": f"{rework_share * 100:.0f}%",
                "delta": None,
                "sub": (
                    f"{theme.fmt_money_exact(rr_rework_cost)} in review "
                    "rounds >= 1"
                ),
                "spark": None,
            },
        ]
        st.markdown(_kpi_strip_html(kpis), unsafe_allow_html=True)

        # Second wave -- the remaining widget reads. The KPI strip
        # already painted above, so the user has real content on
        # screen while the second wave finishes.
        results.update(_dispatch_reads(
            second_wave_readers, st=st, parallel=parallel,
        ))
    _log_dashboard_load(
        load_start=load_start, reads=total_reads, parallel=parallel,
    )
    stage_rows = results["stage_rows"]
    agent_exits = results["agent_exits"]
    issues_rows = results["issues_rows"]
    backend_rows = results["backend_rows"]
    repo_rows = results["repo_rows"]
    heatmap_rows = results["heatmap_rows"]
    backend_daily_rows = results["backend_daily_rows"]
    skill_rows = results["skill_rows"]
    skill_matrix_rows = results["skill_matrix_rows"]

    _render_hero_usage(
        st=st,
        dashboard_charts=dashboard_charts,
        ts_points=ts_points,
        backend_daily_rows=backend_daily_rows,
    )
    _render_stage_review_bars(
        st=st,
        dashboard_charts=dashboard_charts,
        stage_rows=stage_rows,
        review_round_rows=review_round_rows,
    )
    _render_issues_and_backends(
        st=st,
        theme=theme,
        issues_rows=issues_rows,
        backend_rows=backend_rows,
        cost_coverage_rows=cost_coverage_rows,
    )
    _render_repo_and_reliability(
        st=st,
        dashboard_charts=dashboard_charts,
        theme=theme,
        repo_rows=repo_rows,
        summary=summary,
        throughput_rows=throughput_rows,
        window=window,
        resolved=resolved,
        rejected=rejected,
    )
    _render_activity_heatmap(
        st=st,
        dashboard_charts=dashboard_charts,
        heatmap_rows=heatmap_rows,
        tz_offset_choice=tz_offset_choice,
    )

    # ── Skill trigger rates ────────────────────────────
    # Opt-in read-side widget over the `skills_triggered` /
    # `skills_triggered_count` fields `record_agent_exit` folds into
    # `extras` when `TRACK_SKILL_TRIGGERS` is on. A `0%` rate is a real
    # signal ("this role's skill is not firing"), but it cannot tell a
    # tracked-but-quiet run from one whose tracking was off, so the
    # caption names the switch when nothing has triggered yet.
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "Skill trigger rates",
                "Share of agent runs that triggered a skill, by role and "
                "backend (requires TRACK_SKILL_TRIGGERS)",
            ),
            unsafe_allow_html=True,
        )
        if skill_rows:
            st.markdown(
                _skill_triggers_html(skill_rows),
                unsafe_allow_html=True,
            )
            if not any(r.skill_runs for r in skill_rows):
                st.caption(
                    "No skill triggers recorded in this window. Enable "
                    "`TRACK_SKILL_TRIGGERS` (default off) so "
                    "`record_agent_exit` records which skills each run "
                    "pulls."
                )
            # Second table: the per-skill x (repo, role, backend) trigger
            # matrix. Folds each repo's skill catalog into the observed
            # triggers so an offered-but-never-triggered skill surfaces
            # as an explicit `0` cell; `_skill_matrix_html` renders a
            # clear fallback notice in place of the table when the read
            # model returns no catalog-backed matrix (no catalog records
            # matched and no run fired a skill). Folded into an expander
            # (collapsed by default, mirroring "Recent agent runs" below)
            # so the matrix -- capped at 100 rows by the read model
            # (Runs-with-skill DESC then Runs DESC) and shown by default
            # repo-ascending then trigger-rate-descending -- does not
            # dominate the card until the operator opens it.
            with st.expander(
                "Per-skill trigger matrix · which skills each "
                "repo × role × backend cohort reaches for",
                expanded=False,
            ):
                # Clickable column headers re-sort the matrix: each header
                # anchor writes `mtx_sort` / `mtx_dir` query params, which
                # this parses back into a (column, direction) pair the
                # render applies on top of the read model's default order.
                matrix_sort_key, matrix_sort_desc = parse_skill_matrix_sort(
                    st.query_params
                )
                st.markdown(
                    _skill_matrix_html(
                        skill_matrix_rows,
                        sort_key=matrix_sort_key,
                        descending=matrix_sort_desc,
                    ),
                    unsafe_allow_html=True,
                )
        else:
            st.info("No `agent_exit` rows match the current filters.")

    _render_recent_runs(
        st=st,
        pd=pd,
        agent_exits=agent_exits,
        tz_offset_choice=tz_offset_choice,
    )

    _render_drilldown(
        st=st,
        pd=pd,
        window=window,
        repo_filter=repo_filter,
        issue_input_parsed=issue_input_parsed,
        event_filter=event_filter,
        stage_filter=stage_filter,
    )

    st.markdown(
        '<div class="orch-foot">'
        f'Real data · window {window.start.date().isoformat()} → '
        f'{(window.end - timedelta(days=1)).date().isoformat()} · '
        f'{theme.fmt_num(summary.total_agent_runs)} agent runs'
        '</div>',
        unsafe_allow_html=True,
    )


def _filter_list(values_t: Optional[Sequence[str]]) -> Optional[list[str]]:
    """Convert a cached filter tuple back to the read model's list arg.

    `cache_key` stores the event / stage multiselects as hashable
    tuples so they can key `st.cache_data`; the `analytics.read`
    getters take lists. Converting per read keeps the tri-state intact
    -- `None` means "no filter", an empty selection means "show
    nothing", and the two must stay distinct at the read layer.
    """
    return list(values_t) if values_t is not None else None


def _scoped_read(getter: Callable[..., Any], /, **filters: Any) -> Any:
    """Run one windowed read on the per-thread analytics connection.

    Checks out the thread-local connection via `analytics_connection()`
    and forwards it to `getter` alongside the resolved filter kwargs, so
    every cached reader shares one open socket per render pass instead of
    opening (and hashing) a connection per call. The cached wrappers stay
    connection-free: `conn` is supplied here and never lands in their
    `st.cache_data` key (a raw `psycopg.Connection` is unhashable and
    would make every reload look like a cache miss).
    """
    with analytics_read.analytics_connection() as conn:
        return getter(conn=conn, **filters)


def _read_static_metadata(*, st: Any):
    """Read the data extent + filter options through cached wrappers.

    `get_data_extent` / `get_filter_options` carry no filter inputs (the
    cache key is empty) and only change as `analytics.sync` ingests new
    events, so both are cached under the longer `STATIC_METADATA_TTL_SECONDS`
    (5 min) rather than the per-filter 60 s TTL -- collapsing the sidebar /
    topbar round-trip on every rerun. Returns `(extent, options)`; a read
    error is surfaced as one `st.error` and stops the app.
    """
    @st.cache_data(show_spinner=False, ttl=STATIC_METADATA_TTL_SECONDS)
    def _read_data_extent():
        return _scoped_read(analytics_read.get_data_extent)

    @st.cache_data(show_spinner=False, ttl=STATIC_METADATA_TTL_SECONDS)
    def _read_filter_options():
        return _scoped_read(analytics_read.get_filter_options)

    try:
        return _read_data_extent(), _read_filter_options()
    except analytics_read.AnalyticsReadError as e:
        st.error(
            "Could not load analytics filter options: "
            f"{e}. Verify `ANALYTICS_DB_URL` and that the Postgres "
            "service is reachable, then reload."
        )
        st.stop()


def _render_no_data(*, st: Any, extent: DataExtent, theme: Any) -> None:
    """Render the no-data startup state and stop.

    The data extent is empty (`analytics_events` holds zero rows), so paint
    the topbar with zeroed counts and surface `NO_DATA_MESSAGE` below it
    before halting the app.
    """
    st.markdown(
        _topbar_html(
            extent=extent,
            distinct_repos=0,
            total_events=0,
            spend_in_range=0.0,
            fmt_money_exact=theme.fmt_money_exact,
            fmt_num=theme.fmt_num,
        ),
        unsafe_allow_html=True,
    )
    st.info(NO_DATA_MESSAGE)
    st.stop()


def _widget_readers(*, st: Any, key, prev_key, tz_offset_choice: int):
    """Define the cached per-filter read wrappers and stage them.

    Returns `(first_wave_readers, second_wave_readers)` -- each a list of
    `(name, zero-arg callable)` pairs `_fan_out_reads` dispatches.

    Connection scoping: each wrapper delegates its read to `_scoped_read`,
    which checks out the thread-local analytics connection via
    `analytics_connection()` and forwards it to the read helper rather
    than threading a connection through the cache key (a raw
    `psycopg.Connection` is not hashable and would crash the wrapper, and
    every reload would otherwise look like a cache miss). The thread-local
    persists across reads in the same render pass, so the first cache-miss
    pays the psycopg handshake and the rest reuse the open socket. The
    cache key stays the filter tuple `(start, end, repo, events_t,
    stages_t, issue)`.

    Split into two staged waves so the topbar / filter meta / insight
    banners / KPI strip can paint as soon as their inputs are available
    instead of blocking on every widget: the first wave carries the six
    reads those above-the-fold widgets consume, the second the nine
    remaining widget reads. The lambdas close over `key` / `prev_key` so
    the executor never threads filter tuples through the futures, and
    worker threads only return data -- every `st.*` write happens on the
    caller's render thread between the waves.
    """
    @st.cache_data(show_spinner=False, ttl=60)
    def _read_summary(start, end, repo, events_t, stages_t, issue):
        return _scoped_read(
            analytics_read.get_summary,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_prev_kpi(start, end, repo, events_t, stages_t, issue):
        # Previous-window read for the KPI delta pills and cost-trend
        # banner only. The full `get_summary` shape (per-event / per-stage
        # breakdowns, distinct-issue / distinct-repo counts, failure /
        # timeout counters) is never read off `prev_summary`, so a thinner
        # reader saves a `GROUP BY` follow-up plus a couple of
        # `COUNT(DISTINCT)`s on every cold load while leaving the cached
        # wrapper shape (and cache key) identical to `_read_summary`.
        return _scoped_read(
            analytics_read.get_kpi_prev,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_time_series(start, end, repo, events_t, stages_t, issue):
        return _scoped_read(
            analytics_read.get_time_series,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_stage_breakdown(start, end, repo, events_t, stages_t, issue):
        return _scoped_read(
            analytics_read.get_stage_breakdown,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_recent_agent_exits(
        start, end, repo, events_t, stages_t, issue
    ):
        return _scoped_read(
            analytics_read.get_recent_agent_exits,
            limit=DEFAULT_RECENT_AGENT_EXITS,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_top_cost_issues(start, end, repo, events_t, stages_t, issue):
        # Ask the database for the top-cost issues directly. Reading
        # the latest N issues by `last_seen` and re-sorting in Python
        # silently drops older high-cost issues that fall outside the
        # truncated set, so the redesigned "Most expensive issues"
        # panel must be cost-ordered at the SQL layer.
        return _scoped_read(
            analytics_read.get_issues,
            limit=DEFAULT_EXPENSIVE_LIMIT,
            sort_by=analytics_read.SORT_BY_COST,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_review_round(start, end, repo, events_t, stages_t, issue):
        return _scoped_read(
            analytics_read.get_review_round_breakdown,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_backend_efficiency(
        start, end, repo, events_t, stages_t, issue
    ):
        return _scoped_read(
            analytics_read.get_backend_efficiency,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_repo_breakdown(start, end, repo, events_t, stages_t, issue):
        return _scoped_read(
            analytics_read.get_repo_breakdown,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_cost_coverage(start, end, repo, events_t, stages_t, issue):
        return _scoped_read(
            analytics_read.get_cost_coverage,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_hourly_heatmap(
        start, end, repo, events_t, stages_t, issue, tz_offset_hours,
    ):
        return _scoped_read(
            analytics_read.get_hourly_heatmap,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue, tz_offset_hours=tz_offset_hours,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_throughput(start, end, repo, events_t, stages_t, issue):
        return _scoped_read(
            analytics_read.get_throughput_breakdown,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_backend_daily_tokens(
        start, end, repo, events_t, stages_t, issue
    ):
        return _scoped_read(
            analytics_read.get_backend_daily_tokens,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_skill_trigger_rates(
        start, end, repo, events_t, stages_t, issue
    ):
        return _scoped_read(
            analytics_read.get_skill_trigger_rates,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    @st.cache_data(show_spinner=False, ttl=60)
    def _read_skill_trigger_matrix(
        start, end, repo, events_t, stages_t, issue
    ):
        return _scoped_read(
            analytics_read.get_skill_trigger_matrix,
            start=start, end=end, repo=repo,
            events=_filter_list(events_t), stages=_filter_list(stages_t),
            issue=issue,
        )

    # Read fan-out. Each entry is `(name, zero-arg callable)` so
    # `_fan_out_reads` can dispatch them across worker threads when
    # `DASHBOARD_PARALLEL_READS` is set; the sequential path stays in
    # this thread under the existing thread-local `analytics_connection`.
    # Lambdas close over `key` / `prev_key` so the executor never has
    # to thread filter tuples through the futures.
    #
    # Split into two staged waves so the topbar / filter meta /
    # insight banners / KPI strip paint as soon as their inputs are
    # available instead of blocking on every widget. The first wave
    # carries the six reads those above-the-fold widgets consume
    # (`summary`, `prev_summary`, `ts_points`, `review_round_rows`,
    # `throughput_rows`, `cost_coverage_rows`); the second wave runs
    # the nine remaining widget reads. Worker threads only return
    # data back to this render thread -- every `st.*` / placeholder
    # write happens on the main thread between waves.
    first_wave_readers: list[tuple[str, Callable[[], Any]]] = [
        ("summary", lambda: _read_summary(*key)),
        ("prev_summary", lambda: _read_prev_kpi(*prev_key)),
        ("ts_points", lambda: _read_time_series(*key)),
        ("review_round_rows", lambda: _read_review_round(*key)),
        ("throughput_rows", lambda: _read_throughput(*key)),
        ("cost_coverage_rows", lambda: _read_cost_coverage(*key)),
    ]
    second_wave_readers: list[tuple[str, Callable[[], Any]]] = [
        ("stage_rows", lambda: _read_stage_breakdown(*key)),
        ("agent_exits", lambda: _read_recent_agent_exits(*key)),
        ("issues_rows", lambda: _read_top_cost_issues(*key)),
        ("backend_rows", lambda: _read_backend_efficiency(*key)),
        ("repo_rows", lambda: _read_repo_breakdown(*key)),
        ("heatmap_rows", lambda: _read_hourly_heatmap(
            *key, int(tz_offset_choice),
        )),
        ("backend_daily_rows", lambda: _read_backend_daily_tokens(*key)),
        ("skill_rows", lambda: _read_skill_trigger_rates(*key)),
        ("skill_matrix_rows", lambda: _read_skill_trigger_matrix(*key)),
    ]
    return first_wave_readers, second_wave_readers


def _dispatch_reads(readers, *, st: Any, parallel: bool):
    """Dispatch one read wave and surface a read error as one banner.

    Runs the wave through `_fan_out_reads` (sequential, or across a thread
    pool when `parallel`) and returns the name->data dict. An
    `AnalyticsReadError` from any reader is caught, rendered as one
    `st.error`, and stops the app -- the dashboard cannot render without
    database access.
    """
    try:
        return _fan_out_reads(readers, parallel=parallel)
    except analytics_read.AnalyticsReadError as e:
        st.error(
            f"Analytics query failed: {e}. The dashboard cannot render "
            "without database access; check Postgres connectivity and "
            "reload."
        )
        st.stop()


def _log_dashboard_load(*, load_start: float, reads: int, parallel: bool) -> None:
    """Emit the single `dashboard.load:` INFO line for the A/B rollout.

    Carries total wall-clock, the reader count (6 when the empty-window
    short-circuit skips the second wave, else 15), and the parallel flag,
    so the sequential / parallel paths can be A/B'd with one
    `grep dashboard.load streamlit.log`.
    """
    log.info(
        "dashboard.load: total=%.1fs reads=%d parallel=%s",
        perf_counter() - load_start,
        reads,
        "true" if parallel else "false",
    )


def _render_empty_window(
    *,
    st: Any,
    pd: Any,
    load_start: float,
    reads: int,
    parallel: bool,
    window: DateWindow,
    repo_filter: Optional[str],
    issue_input_parsed: Optional[int],
    event_filter: Optional[Sequence[str]],
    stage_filter: Optional[Sequence[str]],
) -> None:
    """Render the empty-window state (no events match the filters).

    The first wave's summary returned zero events, so the second wave is
    skipped entirely -- the remaining widget reads would only paint empty
    cards. Logs the short-circuit (so the A/B line still lands), surfaces
    `EMPTY_WINDOW_MESSAGE`, and still renders the per-issue drill-down
    (which runs its own read).
    """
    _log_dashboard_load(load_start=load_start, reads=reads, parallel=parallel)
    st.info(EMPTY_WINDOW_MESSAGE)
    _render_drilldown(
        st=st,
        pd=pd,
        window=window,
        repo_filter=repo_filter,
        issue_input_parsed=issue_input_parsed,
        event_filter=event_filter,
        stage_filter=stage_filter,
    )


def _render_sidebar_filters(*, st: Any, options: Any):
    """Render the sidebar filter widgets; return the raw selections.

    The repo selector plus the event / stage multiselects and the
    issue-number input. Returns `(repo_choice, event_choice,
    stage_choice, issue_input)`; the caller resolves these into the
    tri-state read filters. An empty multiselect is a deliberate "show
    nothing for these" signal, not "no filter" -- that distinction is
    made downstream, not here.
    """
    with st.sidebar:
        st.header("Filters")
        repo_options = ("All", *options.repos) if options.repos else ("All",)
        repo_choice = st.selectbox("Repo", repo_options, index=0)
        event_choice = st.multiselect(
            "Events",
            list(options.events),
            default=list(options.events),
            help=(
                "Narrows every widget. An empty selection means "
                "'show nothing for these events'."
            ),
        )
        stage_choice = st.multiselect(
            "Stages",
            list(options.stages),
            default=list(options.stages),
            help=(
                "Narrows every widget. An empty selection means "
                "'show nothing for these stages'."
            ),
        )
        issue_input = st.text_input(
            "Issue number",
            value="",
            help=(
                "Enter `123` or `#123` to narrow every widget to one "
                "issue AND render the per-issue event trace at the "
                "bottom. Requires a specific repo above."
            ),
        )
    return repo_choice, event_choice, stage_choice, issue_input


def _render_date_filter_bar(
    *,
    st: Any,
    extent: DataExtent,
    extent_min_d: date,
    extent_max_d: date,
):
    """Render the preset + date-range filter bar.

    Returns `(window, meta_slot)`: the resolved `DateWindow` every
    downstream read is scoped to, and the `st.empty()` placeholder the
    range meta ("… → … · N days · N runs") fills once the summary read
    lands (the run count is not known yet). The selected preset persists
    in `st.session_state` so a custom pick survives a rerun.
    """
    if "preset" not in st.session_state:
        st.session_state.preset = DEFAULT_PRESET
    with st.container(border=True):
        # A hidden `.orch-cardmark` as the bordered container's first
        # child lets the shared white-card rule in
        # `dashboard_theme.PAGE_CSS` (`:has(> stElementContainer
        # .orch-cardmark)`) paint this filter bar like every other card --
        # Streamlit 1.58 dropped the stable border-wrapper testid the old
        # per-card selector relied on. The `.orch-filterbar-anchor` below
        # stays in the left column purely as the hidden label sentinel.
        st.markdown(
            '<div class="orch-cardmark"></div>', unsafe_allow_html=True
        )
        # Single-line filter bar: label · preset switch · From · To ·
        # range meta, all bottom-aligned so the short controls (label,
        # radio, meta) sit on the same baseline as the taller date
        # inputs.
        (
            fb_label,
            fb_preset,
            fb_from,
            fb_to,
            fb_meta,
        ) = st.columns(
            [1.0, 1.7, 1.4, 1.4, 3.0], vertical_alignment="bottom"
        )
        with fb_label:
            st.markdown(
                '<div class="orch-filterbar-anchor"></div>'
                '<span class="orch-filter-label">Date range</span>',
                unsafe_allow_html=True,
            )
        with fb_preset:
            preset_choice = st.radio(
                "Range preset",
                options=(PRESET_3D, PRESET_7D, PRESET_ALL),
                format_func=lambda p: PRESET_INLINE_LABELS[p],
                index=(
                    (PRESET_3D, PRESET_7D, PRESET_ALL).index(
                        st.session_state.preset
                    )
                    if st.session_state.preset
                    in (PRESET_3D, PRESET_7D, PRESET_ALL)
                    else 2
                ),
                horizontal=True,
                label_visibility="collapsed",
                key="_preset_radio",
            )
        initial_window = (
            preset_window(preset_choice, extent)
            or to_window(extent_min_d, extent_max_d)
        )
        with fb_from:
            start_date = st.date_input(
                "From",
                value=initial_window.start.date(),
                min_value=extent_min_d,
                max_value=extent_max_d,
            )
        with fb_to:
            end_default = (initial_window.end - timedelta(days=1)).date()
            end_date = st.date_input(
                "To",
                value=end_default,
                min_value=extent_min_d,
                max_value=extent_max_d,
            )
        # The run count is not known until the summary read lands, so
        # capture the meta slot now and fill it between fan-out waves.
        with fb_meta:
            meta_slot = st.empty()
    window = to_window(start_date, end_date)
    st.session_state.preset = preset_choice
    return window, meta_slot


def _build_read_keys(
    *,
    window: DateWindow,
    repo_filter: Optional[str],
    event_filter: Optional[Sequence[str]],
    stage_filter: Optional[Sequence[str]],
    issue_filter: Optional[int],
):
    """Build the current + previous-window cache-key tuples.

    Returns `(key, prev_key)`: the `(start, end, repo, events, stages,
    issue)` tuple the fan-out reads are cached under, and the same
    tuple shifted to the immediately-preceding equal-length window for
    the KPI delta pills. The reader lambdas splat these with `*key` /
    `*prev_key`, so the tuple order is the read helpers' positional
    contract.
    """
    key = cache_key(
        window, repo_filter, event_filter, stage_filter, issue_filter
    )
    prev_key = cache_key(
        previous_window(window),
        repo_filter,
        event_filter,
        stage_filter,
        issue_filter,
    )
    return key, prev_key


def _render_hero_usage(
    *,
    st: Any,
    dashboard_charts: Any,
    ts_points: Any,
    backend_daily_rows: Any,
) -> None:
    """Render the hero spend / token-usage stacked-area card.

    Carries the "By token type / By backend" toggle. The backend stack
    is built off `get_backend_daily_tokens` (not the LIMIT-capped
    recent-runs table) so a busy window's stack matches the full-window
    cost line and KPI tiles instead of silently undercounting.
    """
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "Spend & token usage over time",
                "Daily token consumption with cost trend overlaid",
            ),
            unsafe_allow_html=True,
        )
        if "stack_mode" not in st.session_state:
            st.session_state.stack_mode = "type"
        stack_mode = st.radio(
            "Stack mode",
            options=("type", "backend"),
            format_func=lambda m: (
                "By token type" if m == "type" else "By backend"
            ),
            index=0 if st.session_state.stack_mode == "type" else 1,
            horizontal=True,
            label_visibility="collapsed",
            key="_stack_mode_radio",
        )
        st.session_state.stack_mode = stack_mode

        backend_by_day: dict[date, dict[str, float]] = {}
        if stack_mode == "backend":
            for row in backend_daily_rows:
                backend_by_day.setdefault(row.day, {})
                backend_by_day[row.day][row.backend] = (
                    backend_by_day[row.day].get(row.backend, 0)
                    + int(row.total_tokens or 0)
                )

        st.plotly_chart(
            dashboard_charts.usage_over_time(
                ts_points,
                backend_rows_by_day=(
                    backend_by_day if stack_mode == "backend" else None
                ),
                mode=stack_mode,
                # The card header already renders the title; suppress
                # the in-chart title so it is not duplicated.
                title=None,
            ),
            use_container_width=True,
            config=PLOTLY_CONFIG,
        )


def _render_stage_review_bars(
    *,
    st: Any,
    dashboard_charts: Any,
    stage_rows: Any,
    review_round_rows: Any,
) -> None:
    """Render the side-by-side per-stage / per-review-round cost bars.

    Both bar panels are pinned to the same height (driven by whichever
    has more bars) so the two cards line up bottom-to-bottom.
    """
    bars_h = 40 * max(len(stage_rows), len(review_round_rows), 1) + 80
    col_stage, col_round = st.columns([7, 5])
    with col_stage:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Cost by workflow stage",
                    "Where spend lands across the issue lifecycle",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_stage(stage_rows, height=bars_h),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )
    with col_round:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Development and review by round",
                    "Developer and reviewer spend per review cycle",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_review_round(
                    review_round_rows, height=bars_h
                ),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )


def _render_issues_and_backends(
    *,
    st: Any,
    theme: Any,
    issues_rows: Any,
    backend_rows: Any,
    cost_coverage_rows: Any,
) -> None:
    """Render the top-cost issues table + backend-efficiency column.

    Left (7/12): the "Most expensive issues" table. `issues_rows` is
    already cost-ordered from SQL, but it is piped through
    `top_expensive_issues` so the in-memory cost / event-count
    tie-breakers stay authoritative and the set never exceeds
    `DEFAULT_EXPENSIVE_LIMIT`. Right (5/12): the per-backend efficiency
    cards above the cost-source coverage bar.
    """
    col_issues, col_backend = st.columns([7, 5])
    with col_issues:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Most expensive issues",
                    "Cost, run count, review rounds, and failure count",
                ),
                unsafe_allow_html=True,
            )
            expensive = top_expensive_issues(issues_rows)
            if expensive:
                st.markdown(
                    _issues_table_html(expensive),
                    unsafe_allow_html=True,
                )
            else:
                st.info("No agent runs with recorded cost in this window.")

    with col_backend:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Backend efficiency",
                    "Cost density, cache leverage, $/run",
                ),
                unsafe_allow_html=True,
            )
            if backend_rows:
                # One `st.markdown` per card (not a single joined
                # markdown) so Streamlit's inter-element gap keeps the
                # cards visually separated.
                for row in backend_rows:
                    st.markdown(
                        _backend_efficiency_card_html(row, theme=theme),
                        unsafe_allow_html=True,
                    )
            else:
                st.info("No `agent_exit` rows match the current filters.")
            if cost_coverage_rows:
                st.markdown(
                    _cost_coverage_bar_html(cost_coverage_rows, theme=theme),
                    unsafe_allow_html=True,
                )


def _render_repo_and_reliability(
    *,
    st: Any,
    dashboard_charts: Any,
    theme: Any,
    repo_rows: Any,
    summary: Summary,
    throughput_rows: Any,
    window: DateWindow,
    resolved: int,
    rejected: int,
) -> None:
    """Render the per-repo cost bars + reliability / throughput column.

    The reliability tiles source every value from the same full-window
    `Summary` aggregate (not the LIMIT-capped recent-runs read) so a
    long window still sees every timeout / failure. The resolved-per-day
    chart is passed the window so zero-resolution days the SQL elides
    still render an explicit bar against the calendar baseline.
    """
    col_repo, col_rel = st.columns([7, 5])
    with col_repo:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Cost by repository",
                    "Spend across managed repos",
                ),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.cost_by_repo(repo_rows),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )
    with col_rel:
        with st.container(border=True):
            st.markdown(
                _card_header_html(
                    "Reliability & throughput",
                    "Run health and issues resolved per day",
                ),
                unsafe_allow_html=True,
            )
            raw_tiles = reliability_tile_data(
                summary, resolved=resolved, rejected=rejected,
            )
            st.markdown(
                _reliability_tiles_html(raw_tiles, fmt_num=theme.fmt_num),
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                dashboard_charts.done_per_day_bars(
                    throughput_rows,
                    window_start=window.start.date(),
                    window_end=(window.end - timedelta(days=1)).date(),
                    title=None,
                ),
                use_container_width=True,
                config=PLOTLY_CONFIG,
            )


def _render_activity_heatmap(
    *,
    st: Any,
    dashboard_charts: Any,
    heatmap_rows: Any,
    tz_offset_choice: int,
) -> None:
    """Render the weekday × hour token-volume heatmap card.

    The in-card UTC-offset selectbox binds to
    `st.session_state["tz_offset_hours"]` (seeded in `main()` before
    the second-wave fan-out) so the heatmap read and this widget agree
    on the offset; on change Streamlit reruns and the next read buckets
    in the newly-picked zone.
    """
    tz_label = format_tz_offset(int(tz_offset_choice))
    with st.container(border=True):
        st.markdown(
            _card_header_html(
                "When agents run",
                f"Token volume by hour ({tz_label}) × weekday",
            ),
            unsafe_allow_html=True,
        )
        st.selectbox(
            "Timezone",
            TZ_OFFSET_OPTIONS,
            key="tz_offset_hours",
            format_func=format_tz_offset,
            help=(
                "Shifts heatmap bucketing and the \"Recent agent "
                "runs\" `ts` column to the selected UTC offset. "
                "`ts` is stored in UTC."
            ),
        )
        st.plotly_chart(
            dashboard_charts.hour_weekday_heatmap(
                heatmap_rows, tz_label=tz_label,
            ),
            use_container_width=True,
            config=PLOTLY_CONFIG,
        )


def _render_recent_runs(
    *,
    st: Any,
    pd: Any,
    agent_exits: Any,
    tz_offset_choice: int,
) -> None:
    """Render the "Recent agent runs" collapsible table.

    The `ts` column is shifted from stored UTC to the wall-clock of the
    selected offset via `shift_ts` so it reads in the same zone as the
    heatmap above it.
    """
    with st.expander("Recent agent runs", expanded=False):
        if agent_exits:
            ts_offset = timedelta(hours=int(tz_offset_choice))
            df_exits = pd.DataFrame([
                {
                    "ts": shift_ts(r.ts, ts_offset),
                    "repo": r.repo,
                    "issue": r.issue,
                    "stage": r.stage,
                    "agent": r.agent_role,
                    "backend": r.backend,
                    "duration (s)": r.duration_s,
                    "exit": r.exit_code,
                    "timed out": r.timed_out,
                    "round": r.review_round,
                    "retry": r.retry_count,
                    "input tokens": r.input_tokens,
                    "output tokens": r.output_tokens,
                    "cost (USD)": r.cost_usd,
                    "cost source": r.cost_source,
                }
                for r in agent_exits
            ])
            st.dataframe(df_exits, use_container_width=True)
        else:
            st.info("No `agent_exit` rows match the current filters.")


def _render_drilldown(
    *,
    st: Any,
    pd: Any,
    window: DateWindow,
    repo_filter: Optional[str],
    issue_input_parsed: Optional[int],
    event_filter: Optional[Sequence[str]],
    stage_filter: Optional[Sequence[str]],
) -> None:
    """Per-issue event trace section.

    Renders only when the operator typed a parseable issue number;
    when a repo is not also selected, surfaces an instructive notice
    so the empty result is not confused for a bug. Failures from the
    read model are caught and surfaced inline -- a drill-down error
    must not poison the overview the operator already scrolled past.
    """
    if issue_input_parsed is None:
        return
    st.subheader(f"Issue #{issue_input_parsed} drill-down")
    if repo_filter is None:
        st.info(
            "Pick a specific repo in the sidebar before drilling "
            "into an issue number -- GitHub issue numbers repeat "
            "across repos."
        )
        return
    try:
        trace = _scoped_read(
            analytics_read.get_issue_events,
            repo=repo_filter,
            issue=issue_input_parsed,
            start=window.start,
            end=window.end,
            events=_filter_list(event_filter),
            stages=_filter_list(stage_filter),
        )
    except analytics_read.AnalyticsReadError as e:
        st.error(f"Issue drill-down failed: {e}")
        return
    if trace:
        st.dataframe(
            pd.DataFrame([
                {
                    "ts": ev.ts,
                    "event": ev.event,
                    "stage": ev.stage,
                    "duration (s)": ev.duration_s,
                    "result": ev.result,
                    "agent": ev.agent_role,
                    "backend": ev.backend,
                    "exit": ev.exit_code,
                    "cost (USD)": ev.cost_usd,
                }
                for ev in trace
            ]),
            use_container_width=True,
        )
    else:
        st.info(
            f"No analytics events recorded for "
            f"`{repo_filter}#{issue_input_parsed}` "
            "under the current filters."
        )


if __name__ == "__main__":
    main()
