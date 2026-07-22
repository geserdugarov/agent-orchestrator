# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Run-level and per-turn trajectory usage HTML."""
from __future__ import annotations

import html

from orchestrator import dashboard_theme as theme
from orchestrator import trajectory_reader
from orchestrator._trajectory_dashboard_summary_html import _fmt_cost_usd


USAGE_SEPARATOR = '<span class="orch-traj-usage-sep">·</span>'


def _usage_chip(text: str, css_class: str = "") -> str:
    classes = f"orch-traj-chip {css_class}".rstrip()
    return f'<span class="{classes}">{html.escape(text)}</span>'


def _run_usage_chips(run: trajectory_reader.TrajectoryRun) -> list[str]:
    usage = run.run_usage
    if usage is None:
        return []
    chips = [_usage_chip(model) for model in usage.models]
    chips.extend(
        (
            _usage_chip(f"total {theme.fmt_num(usage.total_tokens)} tok"),
            _usage_chip(f"in {theme.fmt_num(usage.input_tokens)}"),
            _usage_chip(f"out {theme.fmt_num(usage.output_tokens)}"),
            _usage_chip(f"cache-read {theme.fmt_num(usage.cache_read_tokens)}"),
            _usage_chip(f"cache-write {theme.fmt_num(usage.cache_write_tokens)}"),
        )
    )
    if usage.cached_tokens:
        chips.append(_usage_chip(f"cached {theme.fmt_num(usage.cached_tokens)}"))
    if usage.turns is not None:
        chips.append(_usage_chip(f"{usage.turns} turns"))
    source = run.cost_source or "unknown"
    cost_label = (
        source
        if run.cost_usd is None
        else f"{source} {_fmt_cost_usd(run.cost_usd)}"
    )
    chips.append(_usage_chip(cost_label, "cost"))
    return chips


def _run_usage_note(run: trajectory_reader.TrajectoryRun) -> str:
    if run.turns:
        return (
            "Run cost is authoritative when reported. The per-turn strips in "
            "the timeline are claude-only estimates and need not sum to it; "
            "entries with no strip (tool results, user turns) are turn inputs, "
            "billed on the next assistant turn."
        )
    return (
        "Run cost is authoritative when reported. Per-turn usage is not "
        "available for this backend, so the run-level summary is its only "
        "usage surface."
    )


def _run_usage_html(run: trajectory_reader.TrajectoryRun) -> str:
    """Render run-level usage chips and their accuracy note."""
    if run.run_usage is None:
        return ""
    chips_html = "".join(_run_usage_chips(run))
    row_html = (
        '<div class="orch-traj-chips"><span class="lbl">Run usage</span>'
        f"{chips_html}</div>"
    )
    return (
        f'{row_html}<p class="orch-traj-usage-note">'
        f"{html.escape(_run_usage_note(run))}</p>"
    )


def _turn_usage_html(usage: trajectory_reader.TurnUsageView) -> str:
    """Render compact usage for one assistant turn."""
    segments = []
    if usage.model:
        segments.append(
            '<span class="orch-traj-turn-model">{0}</span>'.format(
                html.escape(usage.model),
            )
        )
    estimated_cost = (
        "est. n/a"
        if usage.cost_usd is None
        else f"est. {_fmt_cost_usd(usage.cost_usd, decimals=4)}"
    )
    usage_labels = (
        f"in {theme.fmt_num(usage.input_tokens)} tok",
        f"out {theme.fmt_num(usage.output_tokens)} tok",
        f"cache-read {theme.fmt_num(usage.cache_read_tokens)}",
        f"cache-write {theme.fmt_num(usage.cache_write_tokens)}",
        estimated_cost,
    )
    segments.extend(f"<span>{usage_label}</span>" for usage_label in usage_labels)
    cache_hit = (
        '<span class="orch-traj-cache-hit">cache hit</span>'
        if usage.cache_read_tokens > 0
        else ""
    )
    return (
        '<div class="orch-traj-turn">'
        f"{USAGE_SEPARATOR.join(segments)}{cache_hit}</div>"
    )
