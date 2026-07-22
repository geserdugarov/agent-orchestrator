# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Topbar, filter metadata, deltas, and KPI-strip HTML."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date
from inspect import Parameter, Signature
from typing import Any, Sequence

from orchestrator import _dashboard_sparkline_html as sparklines


@dataclass(frozen=True)
class _TopbarRequest:
    extent: Any
    distinct_repos: int
    total_events: int
    spend_in_range: float
    fmt_money_exact: Any
    fmt_num: Any


def _plural_s(count: int) -> str:
    return "" if count == 1 else "s"


def _delta_style(delta_value: float, invert: bool) -> tuple[str, str]:
    if delta_value > 0:
        return ("down" if invert else "up"), "▲"
    return ("up" if invert else "down"), "▼"


def _delta_pill(*args: Any, **kwargs: Any) -> str:
    """Render a KPI delta pill through the historical keyword surface."""
    bound = _DELTA_SIGNATURE.bind(*args, **kwargs)
    bound.apply_defaults()
    delta_value = bound.arguments["value"]
    if delta_value is None or delta_value == 0:
        return ""
    percentage_text = "{0:.1f}%".format(abs(delta_value) * 100)
    css_class, arrow = _delta_style(delta_value, bound.arguments["invert"])
    return f'<span class="orch-delta {css_class}">{arrow} {percentage_text}</span>'


def _topbar_html(*args: Any, **kwargs: Any) -> str:
    """Render the topbar through its historical keyword-only surface."""
    request = _TopbarRequest(**_TOPBAR_SIGNATURE.bind(*args, **kwargs).arguments)
    if request.extent.min_ts is None or request.extent.max_ts is None:
        range_label = "no data recorded yet"
    else:
        range_label = "{0} → {1} available".format(
            request.extent.min_ts.date().isoformat(),
            request.extent.max_ts.date().isoformat(),
        )
    subtitle = (
        f"{html.escape(range_label)} · "
        f"{request.distinct_repos} repo{_plural_s(request.distinct_repos)} · "
        f"{request.fmt_num(request.total_events)} events"
    )
    spend = html.escape(request.fmt_money_exact(request.spend_in_range))
    return (
        '<div class="orch-topbar"><div class="orch-brand">'
        '<span class="orch-brand-mark">OA</span><div>'
        f'<h1>Orchestrator Analytics</h1><p class="orch-sub">{subtitle}</p>'
        '</div></div><div class="orch-spend">'
        '<span class="label">Spend in range</span>'
        f'<span class="value">{spend}</span></div></div>'
    )


def _filter_meta_html(
    *,
    from_d: date,
    to_d: date,
    days: int,
    runs: int,
    fmt_num,
) -> str:
    return (
        '<div class="orch-filter-meta">'
        f"{from_d.isoformat()} → {to_d.isoformat()} · "
        f"{days} day{_plural_s(days)} · {fmt_num(runs)} runs</div>"
    )


def _kpi_strip_html(kpis: Sequence[dict]) -> str:
    """Render the four-tile KPI strip."""
    cells = []
    for kpi in kpis:
        delta_html = _delta_pill(
            kpi.get("delta"),
            invert=kpi.get("invert", False),
        )
        spark_html = ""
        if kpi.get("spark") is not None:
            spark_html = sparklines._sparkline_svg(
                kpi["spark"],
                color=kpi.get("spark_color", "#5b54e0"),
            )
        cells.append(
            '<div class="orch-kpi"><div class="kpi-top">'
            f'<span class="kpi-label">{html.escape(kpi["label"])}</span>'
            f"{delta_html}</div>"
            f'<div class="kpi-value">{html.escape(str(kpi["value"]))}</div>'
            '<div class="kpi-foot">'
            f'<span>{html.escape(str(kpi.get("sub", "")))}</span>'
            f"{spark_html}</div></div>"
        )
    return '<div class="orch-kpis">{0}</div>'.format("".join(cells))


_DELTA_SIGNATURE = Signature(
    (
        Parameter("value", Parameter.POSITIONAL_OR_KEYWORD),
        Parameter("invert", Parameter.KEYWORD_ONLY, default=False),
    ),
)
_TOPBAR_SIGNATURE = Signature(
    tuple(
        Parameter(parameter_name, Parameter.KEYWORD_ONLY)
        for parameter_name in (
            "extent",
            "distinct_repos",
            "total_events",
            "spend_in_range",
            "fmt_money_exact",
            "fmt_num",
        )
    ),
)
_delta_pill.__signature__ = _DELTA_SIGNATURE
_topbar_html.__signature__ = _TOPBAR_SIGNATURE
