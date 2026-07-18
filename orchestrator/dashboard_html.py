# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Inline-HTML rendering helpers for the analytics dashboard.

The page renders several panels directly from HTML strings (rather
than Plotly figures or `st.dataframe`) -- the topbar, filter meta,
KPI strip, insight banners, the per-card header, the inline SVG
sparkline / delta pill, the "Most expensive issues" table, the
backend-efficiency cards, the cost-attribution coverage bar, the
reliability-tile strip, and the "Skill trigger rates" aggregate
table. The per-skill trigger matrix that sits under that table lives
in `orchestrator.dashboard_skill_matrix`, which reuses this module's
shared compact-table primitives (`_table_css` / `_table_html`). Each
builder takes read-model rows / small dataclasses (plus, where a panel needs
them, the formatter callables -- or the whole `dashboard_theme`
handle -- the caller passes in) and returns a string the page drops
into `st.markdown(..., unsafe_allow_html=True)`.

Keeping these in their own module means the rendering markup stays
together and free of any Streamlit / Plotly import, so the polling
tick's import surface never touches it.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from orchestrator.analytics.read import (
    BackendEfficiencyRow,
    CostCoverageRow,
    DataExtent,
    IssueSummaryRow,
    SkillTriggerRateRow,
)
from orchestrator.dashboard_kpis import InsightBanner


_UNKNOWN = "unknown"
# Smallest positive span used to avoid a zero-division in sparkline scaling.
_EPSILON = 1e-9
# Tokens per million, for per-million cost ratios.
_MILLION = 1_000_000


def _table_css(table_class: str, *, extra_rules: str = "") -> str:
    """Return the shared inline CSS block for compact dashboard tables."""
    return f"""
<style>
  .{table_class} {{ width: 100%; border-collapse: collapse;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 12.5px; }}
  .{table_class} thead th {{ color: var(--orch-muted);
    font-size: 11px; font-weight: 500; letter-spacing: 0.05em;
    text-transform: uppercase; text-align: left;
    padding: 4px 6px 8px; border-bottom: 1px solid var(--orch-border); }}
  .{table_class} thead th.r {{ text-align: right; }}
  .{table_class} tbody td {{ padding: 8px 6px; vertical-align: middle;
    border-bottom: 1px solid var(--orch-grid); }}
  .{table_class} tbody tr:last-child td {{ border-bottom: 0; }}
  .{table_class} td.r {{ text-align: right; font-family:
    ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums; color: var(--orch-ink); }}
{extra_rules}
</style>
"""


def _table_head_html(columns: Sequence[tuple[str, bool]]) -> str:
    cells = []
    for label, right in columns:
        css_class = ' class="r"' if right else ""
        cells.append(f"<th{css_class}>{html.escape(label)}</th>")
    cells_html = "".join(cells)
    return f"<thead><tr>{cells_html}</tr></thead>"


def _table_html(
    *, table_class: str, css: str, head: str, rows: Sequence[str]
) -> str:
    return (
        css
        + f'<table class="{table_class}">'
        + head
        + "<tbody>" + "".join(rows) + "</tbody>"
        + "</table>"
    )


def _relative_width_pct(magnitude: float, maximum: float) -> float:
    return (magnitude / maximum * 100.0) if maximum > 0 else 0.0


def _short_repo_name(repo: str) -> str:
    return repo.split("/")[-1] if "/" in repo else repo


def _sparkline_y(sample: float, *, lo: float, span: float, pad: int, height: int) -> float:
    normalized = (sample - lo) / span
    drawable_height = height - pad * 2
    return pad + (1 - normalized) * drawable_height


def _int_or_zero(raw: object) -> int:
    if raw is None:
        return 0
    return int(raw)


def _money_or_dash(raw: object) -> str:
    if raw is None:
        return "—"
    return f"${raw:,.2f}"


def _plural_s(count: int) -> str:
    if count == 1:
        return ""
    return "s"


@dataclass(frozen=True)
class _SparklineLayout:
    low: float
    span: float
    padding: int
    height: int
    step: float


@dataclass(frozen=True)
class _SparklinePaths:
    polyline: str
    area: str


def _sparkline_step(width: int, padding: int, value_count: int) -> float:
    drawable_width = width - padding * 2
    intervals = max(value_count - 1, 1)
    return drawable_width / intervals


def _sparkline_layout(series: Sequence[float], *, width: int, height: int) -> _SparklineLayout:
    low = min(series)
    padding = 2
    return _SparklineLayout(
        low=low,
        span=max(max(series) - low, _EPSILON),
        padding=padding,
        height=height,
        step=_sparkline_step(width, padding, len(series)),
    )


def _sparkline_point(index: int, sample: float, layout: _SparklineLayout) -> tuple[float, float]:
    return (
        layout.padding + index * layout.step,
        _sparkline_y(
            sample,
            lo=layout.low,
            span=layout.span,
            pad=layout.padding,
            height=layout.height,
        ),
    )


def _sparkline_points(
    series: Sequence[float], *, width: int, height: int,
) -> list[tuple[float, float]]:
    numbers = [float(sample or 0) for sample in series]
    if not numbers or max(numbers) == min(numbers) == 0:
        return []
    layout = _sparkline_layout(numbers, width=width, height=height)
    return [
        _sparkline_point(index, sample, layout)
        for index, sample in enumerate(numbers)
    ]


def _sparkline_paths(
    points: Sequence[tuple[float, float]], *, height: int,
) -> _SparklinePaths:
    padding = 2
    polyline = " ".join(map(_sparkline_point_text, points))
    area = _sparkline_area_path(points, height=height, padding=padding)
    return _SparklinePaths(polyline=polyline, area=area)


def _sparkline_point_text(point: tuple[float, float]) -> str:
    return f"{point[0]:.1f},{point[1]:.1f}"


def _sparkline_area_path(
    points: Sequence[tuple[float, float]],
    *,
    height: int,
    padding: int,
) -> str:
    baseline = height - padding
    first_x = points[0][0]
    last_x = points[-1][0]
    segments = " L".join(map(_sparkline_point_text, points))
    return (
        f"M{first_x:.1f},{baseline:.1f}"
        f" L{segments}"
        f" L{last_x:.1f},{baseline:.1f} Z"
    )


def _sparkline_svg(
    values: Sequence[float], *, color: str, w: int = 96, h: int = 26
) -> str:
    """Inline SVG sparkline for KPI cards.

    Renders a filled curve under the polyline; rendering is HTML-only
    so the dashboard can drop it inside `st.markdown(..., unsafe_allow_html=True)`
    without a chart round-trip. Empty / flat data renders an empty SVG
    so the layout slot stays consistent across KPIs.
    """
    points = _sparkline_points(values, width=w, height=h)
    if not points:
        return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"></svg>'
    paths = _sparkline_paths(points, height=h)
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'style="display:block">'
        f'<path d="{paths.area}" fill="{color}" fill-opacity="0.18" />'
        f'<polyline points="{paths.polyline}" fill="none" stroke="{color}" '
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
    pct = abs(value) * 100
    pct_str = f"{pct:.1f}%"
    if value > 0:
        css_class = "down" if invert else "up"
        arrow = "▲"
    else:
        css_class = "up" if invert else "down"
        arrow = "▼"
    return f'<span class="orch-delta {css_class}">{arrow} {pct_str}</span>'


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
        min_date = extent.min_ts.date().isoformat()
        max_date = extent.max_ts.date().isoformat()
        range_label = (
            f"{min_date} → "
            f"{max_date} available"
        )
    sub = (
        f"{html.escape(range_label)} · "
        f"{distinct_repos} repo{_plural_s(distinct_repos)} · "
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
        f'{days} day{_plural_s(days)} · '
        f'{fmt_num(runs)} runs'
        '</div>'
    )


def _kpi_strip_html(kpis: Sequence[dict]) -> str:
    """Render the four-tile KPI strip.

    Each KPI dict carries `label`, `value`, `delta`, `sub`,
    optionally `spark` (list of floats) and `spark_color`.
    """
    cells = []
    for kpi in kpis:
        delta_html = _delta_pill(
            kpi.get("delta"), invert=kpi.get("invert", False)
        )
        spark_html = ""
        if kpi.get("spark") is not None:
            spark_html = _sparkline_svg(
                kpi["spark"], color=kpi.get("spark_color", "#5b54e0")
            )
        cells.append(
            '<div class="orch-kpi">'
            '<div class="kpi-top">'
            f'<span class="kpi-label">{html.escape(kpi["label"])}</span>'
            f'{delta_html}'
            '</div>'
            f'<div class="kpi-value">{html.escape(str(kpi["value"]))}</div>'
            '<div class="kpi-foot">'
            f'<span>{html.escape(str(kpi.get("sub", "")))}</span>'
            f'{spark_html}'
            '</div></div>'
        )
    cells_html = "".join(cells)
    return f'<div class="orch-kpis">{cells_html}</div>'


_ISSUES_TABLE_COLUMNS = (
    ("Issue", False),
    ("Cost", True),
    ("Runs", True),
    ("Review rds", True),
    ("Retries", True),
    ("Status", True),
)

_ISSUES_TABLE_EXTRA_CSS = """
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
"""


def _issue_status_pill(failed: int) -> str:
    if failed:
        return f'<span class="orch-pill bad">{failed} fail</span>'
    return '<span class="orch-pill ok">clean</span>'


def _review_round_html(review_rounds: int) -> str:
    if review_rounds >= 3:
        return f'<span class="orch-badge-warn">{review_rounds}</span>'
    return str(review_rounds)


@dataclass(frozen=True)
class _IssueRowView:
    short_repo: str
    cost_text: str
    bar_pct: float
    review_rounds: int
    retries: int
    failed: int


def _issue_row_view(row: IssueSummaryRow, max_cost: float) -> _IssueRowView:
    return _IssueRowView(
        short_repo=_short_repo_name(row.repo),
        cost_text=_money_or_dash(row.total_cost_usd),
        bar_pct=_relative_width_pct(float(row.total_cost_usd or 0), max_cost),
        review_rounds=_int_or_zero(row.max_review_round),
        retries=_int_or_zero(row.max_retry_count),
        failed=int(row.failed_agent_runs or 0),
    )


def _issue_table_row_html(row: IssueSummaryRow, *, max_cost: float) -> str:
    view = _issue_row_view(row, max_cost)
    return (
        "<tr>"
        "<td>"
        '<div class="orch-issue-cell">'
        f'<span><span class="orch-issue-name">{html.escape(view.short_repo)}</span>'
        f' <span class="orch-issue-num">#{int(row.issue)}</span></span>'
        f'<span class="orch-issue-bar"><span style="width:{view.bar_pct:.1f}%">'
        "</span></span>"
        "</div>"
        "</td>"
        f'<td class="r strong">{html.escape(view.cost_text)}</td>'
        f'<td class="r">{int(row.agent_exits or 0)}</td>'
        f'<td class="r">{_review_round_html(view.review_rounds)}</td>'
        f'<td class="r">{view.retries}</td>'
        f'<td class="r">{_issue_status_pill(view.failed)}</td>'
        "</tr>"
    )


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
        (float(row.total_cost_usd or 0) for row in rows),
        default=0,
    ) or 1.0
    css = _table_css("orch-issues", extra_rules=_ISSUES_TABLE_EXTRA_CSS)
    body = [
        _issue_table_row_html(row, max_cost=max_cost)
        for row in rows
    ]
    return _table_html(
        table_class="orch-issues",
        css=css,
        head=_table_head_html(_ISSUES_TABLE_COLUMNS),
        rows=body,
    )


_SKILL_TRIGGERS_TABLE_COLUMNS = (
    ("Role", False),
    ("Backend", False),
    ("Runs", True),
    ("Skill runs", True),
    ("Trigger rate", True),
    ("Triggers", True),
)

_SKILL_TRIGGERS_EXTRA_CSS = """
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
    row: SkillTriggerRateRow, *, max_rate: float
) -> str:
    role = row.agent_role or _UNKNOWN
    backend = row.backend or _UNKNOWN
    rate_pct = row.rate * 100.0
    bar_pct = _relative_width_pct(row.rate, max_rate)
    return (
        "<tr>"
        f'<td class="strong">{html.escape(role)}</td>'
        f'<td>{html.escape(backend)}</td>'
        f'<td class="r">{int(row.runs)}</td>'
        f'<td class="r">{int(row.skill_runs)}</td>'
        '<td class="r"><span class="orch-skill-rate">'
        '<span class="orch-skill-bar">'
        f'<span style="width:{bar_pct:.1f}%"></span></span>'
        f'<span class="orch-skill-pct">{rate_pct:.0f}%</span>'
        "</span></td>"
        f'<td class="r">{int(row.total_triggers)}</td>'
        "</tr>"
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
    max_rate = max((row.rate for row in rows), default=0) or 1.0
    css = _table_css(
        "orch-skills", extra_rules=_SKILL_TRIGGERS_EXTRA_CSS
    )
    body = [
        _skill_trigger_row_html(row, max_rate=max_rate)
        for row in rows
    ]
    return _table_html(
        table_class="orch-skills",
        css=css,
        head=_table_head_html(_SKILL_TRIGGERS_TABLE_COLUMNS),
        rows=body,
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
    for banner in banners:
        icon = icon_for.get(banner.severity, "›")
        rows.append(
            f'<div class="orch-insight {html.escape(banner.severity)}">'
            f'<span class="icon">{icon}</span>'
            f'<span>{html.escape(banner.message)}</span>'
            '</div>'
        )
    rows_html = "".join(rows)
    return f'<div class="orch-insights">{rows_html}</div>'


def _backend_efficiency_card_html(
    row: BackendEfficiencyRow, *, theme
) -> str:
    """Render one backend-efficiency card to inline HTML.

    A spend headline over a `$ / 1M tok` · `% cache hit` · `$ / run`
    row. The caller renders one card per backend (a separate
    `st.markdown` each, so Streamlit's inter-element gap keeps the cards
    spaced). Two accounting choices match the rest of the redesigned
    page:

    - **Token total** is `input + output + cache_read + cache_write`
      (the same volume the headline KPI reports), so the `cost / 1M
      tok` tile divides by that full total rather than raw input.
    - **Cache leverage** is `cache_read / (cache_read + input)` -- the
      share of billable input served from cache, which is the cost
      lever the operator reads off the card. A high cache hit means a
      smaller fraction of input tokens pays the model's input rate.

    Colors and formatters come from the caller's `dashboard_theme`
    handle so this module stays free of the theme import (and the
    lazy-import invariant the dashboard relies on).
    """
    metrics = _backend_efficiency_metrics(row)
    color = theme.color_for(
        row.backend, explicit=theme.BACKEND_COLORS
    )
    return (
        f'<div style="border:1px solid {theme.BORDER};'
        f'border-radius:8px;padding:10px 12px;'
        f'margin-bottom:8px">'
        f'<div style="display:flex;align-items:center;'
        f'gap:8px;margin-bottom:4px">'
        f'<span style="display:inline-block;width:10px;'
        f'height:10px;border-radius:50%;background:{color}">'
        f'</span>'
        f'<b style="color:{theme.TEXT}">'
        f'{html.escape(row.backend)}</b>'
        f'<span style="color:{theme.MUTED_TEXT};'
        f'font-size:12px;margin-left:auto">'
        f'{row.runs} runs · {theme.fmt_tokens(metrics.tokens)} tok'
        '</span>'
        '</div>'
        f'<div style="color:{theme.TEXT};font-size:20px;'
        f'font-weight:600;'
        f'font-family:{theme.MONO_FONT_FAMILY};'
        f'margin-bottom:6px">'
        f'{html.escape(theme.fmt_money_exact(row.total_cost_usd))}'
        f'<span style="color:{theme.MUTED_TEXT};'
        f'font-size:11px;margin-left:8px;'
        f'font-family:{theme.FONT_FAMILY}">'
        f'spend</span></div>'
        f'<div style="display:flex;gap:14px;font-size:12px;'
        f'color:{theme.MUTED_TEXT}">'
        f'<span>${metrics.cost_per_million:.2f} / 1M tok</span>'
        f'<span>{metrics.cache_hit_pct:.0f}% cache hit</span>'
        f'<span>${metrics.cost_per_run:.2f} / run</span>'
        '</div></div>'
    )


@dataclass(frozen=True)
class _BackendEfficiencyMetrics:
    tokens: int
    cost_per_million: float
    cost_per_run: float
    cache_hit_pct: float


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _backend_efficiency_metrics(
    row: BackendEfficiencyRow,
) -> _BackendEfficiencyMetrics:
    tokens = int(
        (row.total_input_tokens or 0)
        + (row.total_output_tokens or 0)
        + (row.total_cache_read_tokens or 0)
        + (row.total_cache_write_tokens or 0)
    )
    cache_read = int(row.total_cache_read_tokens or 0)
    cache_input_total = cache_read + int(row.total_input_tokens or 0)
    return _BackendEfficiencyMetrics(
        tokens=tokens,
        cost_per_million=_safe_ratio(row.total_cost_usd, tokens / _MILLION),
        cost_per_run=_safe_ratio(row.total_cost_usd, row.runs),
        cache_hit_pct=_safe_ratio(cache_read, cache_input_total) * 100,
    )


def _cost_coverage_weights(
    rows: Sequence[CostCoverageRow],
) -> tuple[list[int], int]:
    total_tokens = sum(int(row.total_tokens or 0) for row in rows)
    if total_tokens > 0:
        return [int(row.total_tokens or 0) for row in rows], total_tokens
    weights = [int(row.runs or 0) for row in rows]
    return weights, sum(weights) or 1


def _cost_source_color(
    cost_source: str, cost_sources: Sequence[str], theme
) -> str:
    return theme.color_for(
        cost_source,
        cost_sources,
        explicit=theme.COST_SOURCE_COLORS,
    )


@dataclass(frozen=True)
class _CoverageSegment:
    bar_html: str
    legend: str


def _coverage_segment(
    row: CostCoverageRow,
    weight: int,
    total: int,
    cost_sources: Sequence[str],
    theme,
) -> _CoverageSegment:
    pct = weight / total * 100
    color = _cost_source_color(row.cost_source, cost_sources, theme)
    return _CoverageSegment(
        bar_html=(
            f'<span style="width:{pct:.1f}%;background:{color}" '
            f'title="{html.escape(row.cost_source)}"></span>'
        ),
        legend=(
            f'<span><span class="dot" style="background:{color}"></span>'
            f'{html.escape(row.cost_source)} '
            f'<b style="color:{theme.TEXT};'
            f'font-family:{theme.MONO_FONT_FAMILY}">{pct:.1f}%</b>'
            '</span>'
        ),
    )


def _coverage_segments(
    rows: Sequence[CostCoverageRow],
    weights: Sequence[int],
    total: int,
    cost_sources: Sequence[str],
    theme,
) -> list[_CoverageSegment]:
    return [
        _coverage_segment(row, weight, total, cost_sources, theme)
        for row, weight in zip(rows, weights)
    ]


def _cost_coverage_bar_html(
    rows: Sequence[CostCoverageRow], *, theme
) -> str:
    """Render the cost-attribution coverage bar to inline HTML.

    Segments are sized by token share, not run share -- a few
    high-token runs can dominate cost while looking like a thin slice
    of the run count, so the bar follows the standalone mock and sizes
    by `total_tokens`. Falls back to the run-count share only when the
    window carries no token volume yet (a fresh database with
    `agent_exit` rows that never reported usage). Colors / formatters
    come from the caller's `dashboard_theme` handle.
    """
    weights, total = _cost_coverage_weights(rows)
    segments = _coverage_segments(
        rows, weights, total, [row.cost_source for row in rows], theme
    )
    bars = "".join(segment.bar_html for segment in segments)
    legends = "".join(segment.legend for segment in segments)
    return (
        '<div class="orch-cov-title">'
        'Cost attribution coverage</div>'
        f'<div class="orch-cov-bar">{bars}</div>'
        f'<div class="orch-cov-legend">{legends}</div>'
    )


def _reliability_tiles_html(
    tiles: Sequence[tuple], *, fmt_num
) -> str:
    """Render the reliability-tile strip to inline HTML.

    Each tile is a `(value, label, tone)` triple from
    `dashboard_kpis.reliability_tile_data`; numeric values format
    through the caller's `fmt_num`, string values (e.g. the `0%`
    success rate) pass through verbatim. The `tone` class paints the
    warn / bad tiles so a window's failures and timeouts stand out.
    """
    tiles_html = "".join(
        f'<div class="orch-rel-tile {tone}">'
        f'<div class="orch-rel-value">'
        f'{html.escape(tile_value if isinstance(tile_value, str) else fmt_num(tile_value))}'
        f'</div>'
        f'<div class="orch-rel-label">{html.escape(lbl)}</div>'
        '</div>'
        for tile_value, lbl, tone in tiles
    )
    return f'<div class="orch-rel-tiles">{tiles_html}</div>'
