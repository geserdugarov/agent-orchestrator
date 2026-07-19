# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared low-level Plotly primitives for the analytics chart builders.

Home of the small building blocks the chart-family leaves reuse: the no-data
placeholder figure (`_empty_figure`), the money / monospace-textfont /
two-line-y-tick label helpers, the list-reversal helper, and the
horizontal-bar panel-height / legend helpers with their row / extra-height
constants. The `orchestrator.dashboard_charts_usage`,
`orchestrator.dashboard_charts_cost`, and
`orchestrator.dashboard_charts_throughput` leaves import the primitives they
need from here (the `orchestrator.dashboard_charts_heatmap` leaf inlines its
own empty-state and imports none), so the primitives have a single home and the
dependency stays one-directional: the leaves depend on this base and this base
depends on none of them, which keeps a direct import of any chart module
cycle-free.

Plotly is imported at module load here, like the sibling chart leaves: this
module is only reachable from the lazy `import dashboard_charts` inside
`orchestrator.dashboard.main`, so the polling tick never imports it.
"""
from __future__ import annotations

from typing import Optional, Sequence

from plotly import graph_objects as go

from orchestrator import dashboard_theme as theme

# Horizontal-bar panel sizing (px): per-row height plus the fixed
# margin / axis base every horizontal cost bar adds on top.
_HORIZONTAL_BAR_ROW_HEIGHT = 40
_HORIZONTAL_BAR_EXTRA_HEIGHT = 80


def _empty_figure(message: str, *, height: int) -> go.Figure:
    """Return a placeholder figure with a centered annotation.

    Plotly raises no error on an empty data series, but the default
    "blank canvas" is a confusing empty-state. Every builder routes
    its no-data branch through here so the user sees a single
    consistent "nothing matches" label across charts. `height` mirrors
    the builder's pinned non-empty height so empty cards do not snap
    to Plotly's 450px default and dwarf surrounding cards.
    """
    fig = go.Figure()
    layout = theme.base_layout()
    layout["height"] = height
    fig.update_layout(**layout)
    fig.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"color": theme.MUTED_TEXT, "size": theme.FONT_SIZE},
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def _money_text(amounts: Sequence[float]) -> list[str]:
    return [theme.fmt_money(amount) for amount in amounts]


def _monospace_textfont() -> dict[str, object]:
    return {
        "color": theme.TEXT,
        "size": 12,
        "family": theme.MONO_FONT_FAMILY,
    }


def _two_line_y_ticks(
    labels: Sequence[str], subs: Sequence[str]
) -> list[str]:
    ticks: list[str] = []
    for label, sub in zip(labels, subs):
        label_html = f"<b>{label}</b>"
        if sub:
            ticks.append(
                f"{label_html}<br>"
                f"<span style='color:{theme.MUTED_TEXT};font-size:11px'>"
                f"{sub}</span>"
            )
        else:
            ticks.append(label_html)
    return ticks


def _reverse_lists(*sequences: Sequence) -> tuple[list, ...]:
    return tuple(list(reversed(sequence)) for sequence in sequences)


def _horizontal_panel_height(
    row_count: int,
    *,
    height: Optional[int],
    row_height: int = _HORIZONTAL_BAR_ROW_HEIGHT,
    extra_height: int = _HORIZONTAL_BAR_EXTRA_HEIGHT,
) -> int:
    if height is not None:
        return height
    return row_height * max(row_count, 1) + extra_height


def _horizontal_legend(*, traceorder: Optional[str] = None) -> dict[str, object]:
    legend: dict[str, object] = {
        "orientation": "h",
        "x": 0,
        "y": 1.12,
        "xanchor": "left",
        "yanchor": "bottom",
    }
    if traceorder is not None:
        legend["traceorder"] = traceorder
    return legend
