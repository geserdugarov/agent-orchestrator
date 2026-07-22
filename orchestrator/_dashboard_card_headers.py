# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard card headers, insights, and reliability tiles."""
from __future__ import annotations

import html
from typing import Sequence

from orchestrator.dashboard_kpis import InsightBanner


def card_header_html(title: str, subtitle: str = "") -> str:
    """Build the title and optional subtitle for a dashboard card."""
    subtitle_html = (
        f'<p class="orch-card-sub">{html.escape(subtitle)}</p>'
        if subtitle
        else ""
    )
    return (
        '<span class="orch-cardmark"></span>'
        f'<p class="orch-card-title">{html.escape(title)}</p>{subtitle_html}'
    )


def insights_html(banners: Sequence[InsightBanner]) -> str:
    """Render the computed-insight stack."""
    icon_for = {
        "error": "✕",
        "warning": "!",
        "info": "›",
        "success": "✓",
    }
    rows = []
    for banner in banners:
        icon = icon_for.get(banner.severity, "›")
        rows.append(
            f'<div class="orch-insight {html.escape(banner.severity)}">'
            f'<span class="icon">{icon}</span>'
            f'<span>{html.escape(banner.message)}</span>'
            "</div>"
        )
    rows_html = "".join(rows)
    return f'<div class="orch-insights">{rows_html}</div>'


def reliability_tiles_html(tiles: Sequence[tuple], *, fmt_num) -> str:
    """Render the reliability-tile strip to inline HTML."""
    tiles_html = "".join(
        f'<div class="orch-rel-tile {tone}">'
        '<div class="orch-rel-value">'
        f'{html.escape(tile_value if isinstance(tile_value, str) else fmt_num(tile_value))}'
        "</div>"
        f'<div class="orch-rel-label">{html.escape(label)}</div>'
        "</div>"
        for tile_value, label, tone in tiles
    )
    return f'<div class="orch-rel-tiles">{tiles_html}</div>'
