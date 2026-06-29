# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Inline-HTML rendering helpers for the analytics dashboard.

The page renders several panels directly from HTML strings (rather
than Plotly figures or `st.dataframe`) -- the topbar, filter meta,
KPI strip, insight banners, the per-card header, the inline SVG
sparkline / delta pill, the "Most expensive issues" table, the
"Skill trigger rates" aggregate table, and the per-skill trigger
matrix that sits under it. Each builder takes read-model rows /
small dataclasses (plus, where a panel needs them, the formatter
callables the caller passes from `dashboard_theme`) and returns a
string the page drops into `st.markdown(..., unsafe_allow_html=True)`.

Keeping these in their own module means the rendering markup stays
together and free of any Streamlit / Plotly import, so the polling
tick's import surface never touches it.
"""
from __future__ import annotations

import html
from datetime import date
from typing import Optional, Sequence

from orchestrator.analytics.read import (
    DataExtent,
    IssueSummaryRow,
    SkillTriggerMatrixRow,
    SkillTriggerRateRow,
)
from orchestrator.dashboard_kpis import InsightBanner


def _sparkline_svg(
    values: Sequence[float], *, color: str, w: int = 96, h: int = 26
) -> str:
    """Inline SVG sparkline for KPI cards.

    Renders a filled curve under the polyline; rendering is HTML-only
    so the dashboard can drop it inside `st.markdown(..., unsafe_allow_html=True)`
    without a chart round-trip. Empty / flat data renders an empty SVG
    so the layout slot stays consistent across KPIs.
    """
    nums = [float(v or 0) for v in values]
    if not nums or max(nums) == min(nums) == 0:
        return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"></svg>'
    lo, hi = min(nums), max(nums)
    span = max(hi - lo, 1e-9)
    pad = 2
    step = (w - pad * 2) / max(len(nums) - 1, 1)

    def y(v: float) -> float:
        return pad + (1 - (v - lo) / span) * (h - pad * 2)

    points = [(pad + i * step, y(v)) for i, v in enumerate(nums)]
    poly = " ".join(f"{x:.1f},{yv:.1f}" for x, yv in points)
    area_path = (
        "M" + f"{points[0][0]:.1f},{h - pad:.1f}"
        + " L" + " L".join(f"{x:.1f},{yv:.1f}" for x, yv in points)
        + f" L{points[-1][0]:.1f},{h - pad:.1f} Z"
    )
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'style="display:block">'
        f'<path d="{area_path}" fill="{color}" fill-opacity="0.18" />'
        f'<polyline points="{poly}" fill="none" stroke="{color}" '
        f'stroke-width="1.6" stroke-linecap="round" '
        f'stroke-linejoin="round" />'
        "</svg>"
    )


def _delta_pill(value: Optional[float], *, invert: bool = False) -> str:
    """Render a KPI delta pill (▲/▼ NN.N%) as inline HTML.

    Color convention -- ``.orch-delta.up`` is red, ``.orch-delta.down``
    is green. With ``invert=False`` (the default) a rising value paints
    red and a falling value paints green: this is the right convention
    for cost / token KPIs where "up is bad". ``invert=True`` swaps the
    coloring so positive growth paints green -- use it for KPIs where
    "up is good" (e.g. issues resolved, success rate). The arrow always
    follows the value's sign so the direction is unambiguous even at a
    glance.

    ``None`` (no prior window to compare against) and an exactly-zero
    delta render nothing: a grey placeholder pill in the card corner
    reads like a (non-functional) minimize control, so the KPI top row
    simply drops the indicator when there is no movement to show.
    """
    if value is None or value == 0:
        return ""
    pct_str = f"{abs(value) * 100:.1f}%"
    if value > 0:
        cls = "up" if not invert else "down"
        arrow = "▲"
    else:
        cls = "down" if not invert else "up"
        arrow = "▼"
    return f'<span class="orch-delta {cls}">{arrow} {pct_str}</span>'


def _topbar_html(
    *,
    extent: DataExtent,
    distinct_repos: int,
    total_events: int,
    spend_in_range: float,
    fmt_money_exact,
    fmt_num,
) -> str:
    """Render the page topbar block.

    Mirrors the standalone mock's brand mark + h1 + spend pill.
    """
    if extent.min_ts is None or extent.max_ts is None:
        range_label = "no data recorded yet"
    else:
        range_label = (
            f"{extent.min_ts.date().isoformat()} → "
            f"{extent.max_ts.date().isoformat()} available"
        )
    sub = (
        f"{html.escape(range_label)} · "
        f"{distinct_repos} repo{'s' if distinct_repos != 1 else ''} · "
        f"{fmt_num(total_events)} events"
    )
    return (
        '<div class="orch-topbar">'
        '<div class="orch-brand">'
        '<span class="orch-brand-mark">OA</span>'
        '<div>'
        '<h1>Orchestrator Analytics</h1>'
        f'<p class="orch-sub">{sub}</p>'
        '</div></div>'
        '<div class="orch-spend">'
        '<span class="label">Spend in range</span>'
        f'<span class="value">{html.escape(fmt_money_exact(spend_in_range))}</span>'
        '</div></div>'
    )


def _filter_meta_html(
    *,
    from_d: date, to_d: date, days: int, runs: int, fmt_num
) -> str:
    return (
        '<div class="orch-filter-meta">'
        f'{from_d.isoformat()} → {to_d.isoformat()} · '
        f'{days} day{"s" if days != 1 else ""} · '
        f'{fmt_num(runs)} runs'
        '</div>'
    )


def _kpi_strip_html(kpis: Sequence[dict]) -> str:
    """Render the four-tile KPI strip.

    Each KPI dict carries `label`, `value`, `delta`, `sub`,
    optionally `spark` (list of floats) and `spark_color`.
    """
    cells = []
    for k in kpis:
        delta_html = _delta_pill(
            k.get("delta"), invert=k.get("invert", False)
        )
        spark_html = ""
        if k.get("spark") is not None:
            spark_html = _sparkline_svg(
                k["spark"], color=k.get("spark_color", "#5b54e0")
            )
        cells.append(
            '<div class="orch-kpi">'
            '<div class="kpi-top">'
            f'<span class="kpi-label">{html.escape(k["label"])}</span>'
            f'{delta_html}'
            '</div>'
            f'<div class="kpi-value">{html.escape(str(k["value"]))}</div>'
            '<div class="kpi-foot">'
            f'<span>{html.escape(str(k.get("sub", "")))}</span>'
            f'{spark_html}'
            '</div></div>'
        )
    return '<div class="orch-kpis">' + "".join(cells) + '</div>'


def _issues_table_html(rows: Sequence[IssueSummaryRow]) -> str:
    """Render the "Most expensive issues" table to inline HTML.

    Matches the standalone mock's columns -- Issue / Cost / Runs /
    Review rds / Retries / Status -- and adds two representational
    details `st.dataframe` cannot express:

    - **In-row cost bars.** Each Issue cell carries a thin bar
      under the label whose width is the issue's cost relative to
      the most expensive issue in the panel. Lets the operator
      eyeball the spread without comparing numbers row by row.
    - **Clean / fail status pills.** The Status cell renders as a
      colored pill (`clean` is green, `N fail` is red) instead of
      flat text, matching the mock's pill treatment.

    Local CSS goes inline next to the table so the rules survive a
    future tweak without having to touch `dashboard_theme.PAGE_CSS`
    -- the issues table is the only consumer.
    """
    max_cost = max(
        (float(r.total_cost_usd or 0.0) for r in rows),
        default=0.0,
    ) or 1.0
    css = """
<style>
  .orch-issues { width: 100%; border-collapse: collapse;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 12.5px; }
  .orch-issues thead th { color: var(--orch-muted);
    font-size: 11px; font-weight: 500; letter-spacing: 0.05em;
    text-transform: uppercase; text-align: left;
    padding: 4px 6px 8px; border-bottom: 1px solid var(--orch-border); }
  .orch-issues thead th.r { text-align: right; }
  .orch-issues tbody td { padding: 8px 6px; vertical-align: middle;
    border-bottom: 1px solid var(--orch-grid); }
  .orch-issues tbody tr:last-child td { border-bottom: 0; }
  .orch-issues td.r { text-align: right; font-family:
    ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums; color: var(--orch-ink); }
  .orch-issues td.strong { font-weight: 600; }
  .orch-issue-cell { display: flex; flex-direction: column;
    gap: 4px; }
  .orch-issue-name { color: var(--orch-ink); font-weight: 500; }
  .orch-issue-num { color: var(--orch-muted); font-weight: 400;
    margin-left: 2px; }
  .orch-issue-bar { display: block; height: 4px; border-radius: 2px;
    background: var(--orch-grid); overflow: hidden; }
  .orch-issue-bar > span { display: block; height: 100%;
    background: var(--orch-accent); border-radius: 2px; }
  .orch-pill { display: inline-block; padding: 2px 9px;
    border-radius: 999px; font-size: 11.5px; font-weight: 500;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
  .orch-pill.ok { background: rgba(26, 163, 154, 0.14);
    color: var(--orch-success); }
  .orch-pill.bad { background: rgba(217, 83, 74, 0.14);
    color: var(--orch-danger); }
  .orch-badge-warn { color: var(--orch-warn); font-weight: 600; }
</style>
"""
    body: list[str] = []
    for r in rows:
        short = r.repo.split("/")[-1] if "/" in r.repo else r.repo
        cost = float(r.total_cost_usd or 0.0)
        bar_pct = (cost / max_cost * 100.0) if max_cost > 0 else 0.0
        cost_text = (
            f"${r.total_cost_usd:,.2f}"
            if r.total_cost_usd is not None
            else "—"
        )
        review_rounds = (
            int(r.max_review_round)
            if r.max_review_round is not None
            else 0
        )
        retries = (
            int(r.max_retry_count)
            if r.max_retry_count is not None
            else 0
        )
        failed = int(r.failed_agent_runs or 0)
        if failed:
            pill = f'<span class="orch-pill bad">{failed} fail</span>'
        else:
            pill = '<span class="orch-pill ok">clean</span>'
        # High review-round counts get a warning color so the
        # operator can spot rework-heavy issues without reading the
        # number.
        review_html = (
            f'<span class="orch-badge-warn">{review_rounds}</span>'
            if review_rounds >= 3
            else str(review_rounds)
        )
        body.append(
            "<tr>"
            "<td>"
            '<div class="orch-issue-cell">'
            f'<span><span class="orch-issue-name">{html.escape(short)}</span>'
            f' <span class="orch-issue-num">#{int(r.issue)}</span></span>'
            f'<span class="orch-issue-bar"><span style="width:{bar_pct:.1f}%">'
            "</span></span>"
            "</div>"
            "</td>"
            f'<td class="r strong">{html.escape(cost_text)}</td>'
            f'<td class="r">{int(r.agent_exits or 0)}</td>'
            f'<td class="r">{review_html}</td>'
            f'<td class="r">{retries}</td>'
            f'<td class="r">{pill}</td>'
            "</tr>"
        )
    head = (
        "<thead><tr>"
        "<th>Issue</th>"
        '<th class="r">Cost</th>'
        '<th class="r">Runs</th>'
        '<th class="r">Review rds</th>'
        '<th class="r">Retries</th>'
        '<th class="r">Status</th>'
        "</tr></thead>"
    )
    return (
        css
        + '<table class="orch-issues">'
        + head
        + "<tbody>" + "".join(body) + "</tbody>"
        + "</table>"
    )


def _skill_triggers_html(rows: Sequence[SkillTriggerRateRow]) -> str:
    """Render the "Skill trigger rates" table to inline HTML.

    One row per `(agent_role, backend)` group in the order the read
    model returned them (skill-active groups first). Each Trigger-rate
    cell carries a thin bar whose width is the group's rate relative to
    the busiest group, so the operator can eyeball which roles actually
    pull their skills without comparing percentages row by row.

    Rendered as inline HTML (matching the backend-efficiency cards and
    the cost-coverage bar) rather than a Plotly chart: the data is
    small and categorical, and the panel has to read cleanly even when
    every rate is `0%` (the `TRACK_SKILL_TRIGGERS=off` baseline). The
    local CSS sits inline next to the table -- the skill panel is its
    only consumer -- and reuses the shared `var(--orch-*)` theme tokens.
    """
    max_rate = max((r.rate for r in rows), default=0.0) or 1.0
    css = """
<style>
  .orch-skills { width: 100%; border-collapse: collapse;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 12.5px; }
  .orch-skills thead th { color: var(--orch-muted);
    font-size: 11px; font-weight: 500; letter-spacing: 0.05em;
    text-transform: uppercase; text-align: left;
    padding: 4px 6px 8px; border-bottom: 1px solid var(--orch-border); }
  .orch-skills thead th.r { text-align: right; }
  .orch-skills tbody td { padding: 8px 6px; vertical-align: middle;
    border-bottom: 1px solid var(--orch-grid); }
  .orch-skills tbody tr:last-child td { border-bottom: 0; }
  .orch-skills td.r { text-align: right; font-family:
    ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums; color: var(--orch-ink); }
  .orch-skills td.strong { font-weight: 600; color: var(--orch-ink); }
  .orch-skill-rate { display: flex; align-items: center; gap: 8px;
    justify-content: flex-end; }
  .orch-skill-bar { display: block; height: 4px; width: 64px;
    border-radius: 2px; background: var(--orch-grid); overflow: hidden; }
  .orch-skill-bar > span { display: block; height: 100%;
    background: var(--orch-accent); border-radius: 2px; }
  .orch-skill-pct { min-width: 34px; color: var(--orch-ink); }
</style>
"""
    body: list[str] = []
    for r in rows:
        role = r.agent_role or "unknown"
        backend = r.backend or "unknown"
        rate_pct = r.rate * 100.0
        bar_pct = (r.rate / max_rate * 100.0) if max_rate > 0 else 0.0
        body.append(
            "<tr>"
            f'<td class="strong">{html.escape(role)}</td>'
            f'<td>{html.escape(backend)}</td>'
            f'<td class="r">{int(r.runs)}</td>'
            f'<td class="r">{int(r.skill_runs)}</td>'
            '<td class="r"><span class="orch-skill-rate">'
            '<span class="orch-skill-bar">'
            f'<span style="width:{bar_pct:.1f}%"></span></span>'
            f'<span class="orch-skill-pct">{rate_pct:.0f}%</span>'
            "</span></td>"
            f'<td class="r">{int(r.total_triggers)}</td>'
            "</tr>"
        )
    head = (
        "<thead><tr>"
        "<th>Role</th>"
        "<th>Backend</th>"
        '<th class="r">Runs</th>'
        '<th class="r">Skill runs</th>'
        '<th class="r">Trigger rate</th>'
        '<th class="r">Triggers</th>'
        "</tr></thead>"
    )
    return (
        css
        + '<table class="orch-skills">'
        + head
        + "<tbody>" + "".join(body) + "</tbody>"
        + "</table>"
    )


# Shown in place of the matrix table when `get_skill_trigger_matrix`
# returns no rows: no `repo_skill_catalog` records matched the window
# AND no run fired a skill, so there is no catalog-backed matrix to
# build. Names the `TRACK_SKILL_TRIGGERS` switch (the same caveat the
# aggregate table carries) so a quiet panel is not mistaken for a bug.
SKILL_MATRIX_EMPTY_MESSAGE = (
    "No catalog-backed skill matrix for this window. The matrix pairs "
    "each repo's offered-skill catalog with the skills its runs "
    "triggered; it fills in once `TRACK_SKILL_TRIGGERS` (default off) "
    "has recorded a repo skill catalog and at least one run's triggered "
    "skills."
)


def _skill_matrix_html(rows: Sequence[SkillTriggerMatrixRow]) -> str:
    """Render the per-skill trigger matrix to inline HTML.

    The second table under the "Skill trigger rates" panel: one row per
    `(repo, agent_role, backend, skill)` cell from
    `get_skill_trigger_matrix`, with columns Repo / Role / Backend /
    Skill / Runs with skill. Unlike the aggregate table above it, this
    one folds in each repo's `repo_skill_catalog` so a skill the repo
    offers but no cohort triggered surfaces as an explicit `0`
    "Runs with skill" cell rather than a missing row; the zero cell is
    muted so the offered-but-quiet skills read distinctly from the ones
    that actually fired.

    When the read model returns no rows -- no catalog records matched the
    window and no run fired a skill -- there is no catalog-backed matrix
    to build, so a clear fallback notice (`SKILL_MATRIX_EMPTY_MESSAGE`)
    is rendered in place of the table. Rendered as inline HTML (matching
    the aggregate table) so it reads cleanly even when every cell is `0`;
    the local CSS sits inline next to the table and reuses the shared
    `var(--orch-*)` theme tokens.
    """
    if not rows:
        # Self-contained inline style so the notice still reads as muted
        # body copy without the table's `<style>` block (skipped on this
        # early-return path).
        return (
            '<div class="orch-skillmatrix-empty" '
            'style="color:var(--orch-muted);font-size:12.5px;'
            'padding:8px 2px">'
            f"{html.escape(SKILL_MATRIX_EMPTY_MESSAGE)}"
            "</div>"
        )
    css = """
<style>
  .orch-skillmatrix { width: 100%; border-collapse: collapse;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 12.5px; }
  .orch-skillmatrix thead th { color: var(--orch-muted);
    font-size: 11px; font-weight: 500; letter-spacing: 0.05em;
    text-transform: uppercase; text-align: left;
    padding: 4px 6px 8px; border-bottom: 1px solid var(--orch-border); }
  .orch-skillmatrix thead th.r { text-align: right; }
  .orch-skillmatrix tbody td { padding: 8px 6px; vertical-align: middle;
    border-bottom: 1px solid var(--orch-grid); }
  .orch-skillmatrix tbody tr:last-child td { border-bottom: 0; }
  .orch-skillmatrix td.r { text-align: right; font-family:
    ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums; color: var(--orch-ink); }
  .orch-skillmatrix td.strong { font-weight: 600; color: var(--orch-ink); }
  .orch-skillmatrix-zero { color: var(--orch-muted-soft); }
</style>
"""
    body: list[str] = []
    for r in rows:
        repo = r.repo or "unknown"
        role = r.agent_role or "unknown"
        backend = r.backend or "unknown"
        skill = r.skill or "unknown"
        runs = int(r.runs)
        # A `0` is an offered-but-never-triggered catalog cell -- mute it
        # so the cells that actually fired stand out at a glance.
        runs_html = (
            '<span class="orch-skillmatrix-zero">0</span>'
            if runs == 0
            else str(runs)
        )
        body.append(
            "<tr>"
            f'<td class="strong">{html.escape(repo)}</td>'
            f'<td>{html.escape(role)}</td>'
            f'<td>{html.escape(backend)}</td>'
            f'<td>{html.escape(skill)}</td>'
            f'<td class="r">{runs_html}</td>'
            "</tr>"
        )
    head = (
        "<thead><tr>"
        "<th>Repo</th>"
        "<th>Role</th>"
        "<th>Backend</th>"
        "<th>Skill</th>"
        '<th class="r">Runs with skill</th>'
        "</tr></thead>"
    )
    return (
        css
        + '<table class="orch-skillmatrix">'
        + head
        + "<tbody>" + "".join(body) + "</tbody>"
        + "</table>"
    )


def _card_header_html(title: str, subtitle: str = "") -> str:
    """Inline HTML for the title + subtitle at the top of a card.

    Always rendered through `st.markdown(unsafe_allow_html=True)`
    INSIDE a `st.container(border=True)` block -- a previous draft
    opened a `<div class="orch-card">` in one `st.markdown` and
    closed it in another, which leaves the chart / dataframe widget
    as a sibling of the card in Streamlit's DOM rather than a child.
    The card visual really has to come from a Streamlit container so
    the inner widgets sit inside it.
    """
    sub_html = (
        f'<p class="orch-card-sub">{html.escape(subtitle)}</p>'
        if subtitle
        else ""
    )
    # The hidden `.orch-cardmark` is the per-card sentinel the white-fill
    # / equal-height rules in `dashboard_theme.PAGE_CSS` key off via
    # `:has(> stElementContainer .orch-cardmark)`. Rendering it inside the
    # header markdown keeps it the bordered container's first element.
    return (
        '<span class="orch-cardmark"></span>'
        f'<p class="orch-card-title">{html.escape(title)}</p>{sub_html}'
    )


def _insights_html(
    banners: Sequence[InsightBanner],
) -> str:
    """Render the computed-insight stack.

    The colored icon (red `✕` / `!` for warning + error, neutral `›`
    / `✓` for info + success) carries the severity, so the rendered
    message no longer leads with a redundant `Warning.` / `Info.`
    prefix -- the standalone mock leads each banner with a short
    descriptive title and lets the icon paint the severity.
    """
    icon_for = {
        "error": "✕", "warning": "!", "info": "›", "success": "✓",
    }
    rows = []
    for b in banners:
        icon = icon_for.get(b.severity, "›")
        rows.append(
            f'<div class="orch-insight {html.escape(b.severity)}">'
            f'<span class="icon">{icon}</span>'
            f'<span>{html.escape(b.message)}</span>'
            '</div>'
        )
    return '<div class="orch-insights">' + "".join(rows) + '</div>'
