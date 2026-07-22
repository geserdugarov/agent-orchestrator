# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared compact-table HTML and value formatting."""
from __future__ import annotations

import html
from typing import Sequence


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
    for label, right_aligned in columns:
        css_class = ' class="r"' if right_aligned else ""
        cells.append(f"<th{css_class}>{html.escape(label)}</th>")
    return "<thead><tr>{0}</tr></thead>".format("".join(cells))


def _table_html(
    *,
    table_class: str,
    css: str,
    head: str,
    rows: Sequence[str],
) -> str:
    return (
        css
        + f'<table class="{table_class}">'
        + head
        + "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _relative_width_pct(magnitude: float, maximum: float) -> float:
    return magnitude / maximum * 100 if maximum > 0 else float()


def _short_repo_name(repo: str) -> str:
    return repo.split("/")[-1] if "/" in repo else repo


def _int_or_zero(raw: object) -> int:
    if raw is None:
        return 0
    return int(raw)


def _money_or_dash(raw: object) -> str:
    if raw is None:
        return "—"
    return "${0}".format(format(raw, ",.2f"))
