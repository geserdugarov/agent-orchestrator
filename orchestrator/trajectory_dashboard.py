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
if globals().get("__package__"):
    from orchestrator.script_launch import ensure_repo_root_on_path
else:  # script-launched: only `orchestrator/` is on sys.path
    from script_launch import ensure_repo_root_on_path

ensure_repo_root_on_path(__file__)

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
  /* Run-level usage summary + per-turn strip -------------------- */
  /* The cost chip in the run-usage row is the headline number, so it
     carries the accent border to read louder than the token chips. */
  .orch-traj-chip.cost {{
    border-color: var(--orch-accent); font-weight: 600;
  }}
  .orch-traj-usage-note {{
    color: var(--orch-muted-soft); font-size: 11.5px;
    margin: 0 0 12px; font-family: {theme.FONT_FAMILY};
  }}
  .orch-traj-turn {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
    margin: 14px 0 2px; padding: 5px 11px;
    border: 1px solid var(--orch-border); border-radius: 8px;
    background: var(--orch-chip); color: var(--orch-muted);
    font-size: 11.5px; font-family: {theme.MONO_FONT_FAMILY};
  }}
  .orch-traj-turn .orch-traj-turn-model {{
    color: var(--orch-ink); font-weight: 600;
  }}
  .orch-traj-usage-sep {{ color: var(--orch-muted-soft); }}
  .orch-traj-cache-hit {{
    background: rgba(26,163,154,.14); color: var(--orch-cache);
    border-radius: 999px; padding: 1px 8px;
    font-size: 10px; font-weight: 600; letter-spacing: 0.02em;
  }}
  /* Five KPI tiles on this page (runs / issues / repos / tool calls /
     total cost); re-declare the shared chrome's <=1080px two-column
     reflow so the added tile does not force five across on a narrow
     viewport. Both rules follow `PAGE_CSS`, so they win the cascade. */
  .orch-kpis {{ grid-template-columns: repeat(5, 1fr); }}
  @media (max-width: 1080px) {{
    .orch-kpis {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
"""

# Separator drawn between the segments of a per-turn usage strip.
_USAGE_SEP = '<span class="orch-traj-usage-sep">·</span>'


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


def _fmt_cost_usd(amount: float, *, decimals: int = 2) -> str:
    """Exact dollar figure for a usage cost.

    The dashboard's compact `theme.fmt_money` drops the cents on any figure
    at or above $10 and abbreviates thousands, which would misreport an
    authoritative run or total cost (a $12.50 run must not read as `$12`).
    Per-turn estimates pass `decimals=4` so a sub-cent turn is not floored
    to `$0.00`.
    """
    return f"${amount:,.{decimals}f}"


@dataclass(frozen=True)
class _TrajectoryKpi:
    label: str
    figure: str
    foot: str = ""


def _trajectory_kpis(
    summary: trajectory_reader.TrajectorySummary,
) -> tuple[_TrajectoryKpi, ...]:
    if summary.truncated_runs:
        truncated_foot = f"{theme.fmt_num(summary.truncated_runs)} truncated"
    else:
        truncated_foot = "none truncated"
    return (
        _TrajectoryKpi("Runs", theme.fmt_num(summary.total_runs), truncated_foot),
        _TrajectoryKpi("Issues", theme.fmt_num(summary.distinct_issues)),
        _TrajectoryKpi("Repos", theme.fmt_num(summary.distinct_repos)),
        _TrajectoryKpi("Tool calls", theme.fmt_num(summary.total_tool_calls)),
        _TrajectoryKpi(
            "Total cost",
            _fmt_cost_usd(summary.total_cost_usd),
            "reported + est.",
        ),
    )


def _trajectory_kpi_html(kpi: _TrajectoryKpi) -> str:
    if kpi.foot:
        foot_html = (
            f'<div class="kpi-foot"><span>{html.escape(kpi.foot)}</span></div>'
        )
    else:
        foot_html = '<div class="kpi-foot"></div>'
    return (
        '<div class="orch-kpi">'
        f'<div class="kpi-top"><span class="kpi-label">'
        f'{html.escape(kpi.label)}</span></div>'
        f'<div class="kpi-value">{html.escape(kpi.figure)}</div>'
        f'{foot_html}'
        '</div>'
    )


def _kpi_strip_html(summary: trajectory_reader.TrajectorySummary) -> str:
    """Five-tile KPI strip reusing the dashboard's `.orch-kpi` markup.

    The *Total cost* tile sums the authoritative run cost over the filtered
    runs that recorded one (a mix of reported and estimated figures, hence
    the foot), read entirely from the file -- no Postgres.
    """
    cells = (_trajectory_kpi_html(kpi) for kpi in _trajectory_kpis(summary))
    return f'<div class="orch-kpis">{"".join(cells)}</div>'


def _meta_html(run: TrajectoryRun) -> str:
    """Per-run metadata grid. Only non-empty fields render a tile."""
    fields: list[tuple[str, str]] = [
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
        f'<div class="k">{html.escape(label)}</div>'
        f'<div class="v">{html.escape(cell)}</div>'
        '</div>'
        for label, cell in fields
        if cell
    ]
    return f'<div class="orch-traj-meta">{"".join(cells)}</div>'


def _labeled_chips_html(label: str, names: Sequence[str]) -> str:
    """A label followed by a pill per name; empty `names` yields ''."""
    if not names:
        return ""
    chips = "".join(
        f'<span class="orch-traj-chip">{html.escape(name)}</span>'
        for name in names
    )
    return (
        '<div class="orch-traj-chips">'
        f'<span class="lbl">{html.escape(label)}</span>{chips}'
        '</div>'
    )


def _run_table_row_html(run: TrajectoryRun) -> str:
    round_cell = ""
    if run.review_round is not None:
        round_cell = str(run.review_round)
    row_class = ' class="fixture"' if run.is_fixture else ""
    fixture_tag = ""
    if run.is_fixture:
        fixture_tag = '<span class="orch-traj-fixture-tag">fixture</span>'
    return (
        f"<tr{row_class}>"
        f'<td class="num">#{html.escape(str(run.issue))}</td>'
        f"<td>{html.escape(run.repo)}{fixture_tag}</td>"
        f"<td>{html.escape(run.stage)}</td>"
        f"<td>{html.escape(run.agent_role)}</td>"
        f"<td>{html.escape(run.backend)}</td>"
        f'<td class="num">{html.escape(round_cell)}</td>'
        f'<td class="num">{html.escape(str(run.step_count))}</td>'
        f'<td class="num">{html.escape(str(run.tool_calls))}</td>'
        f"<td>{html.escape(run.ts)}</td>"
        "</tr>"
    )


def _runs_table_html(runs: Sequence[TrajectoryRun]) -> str:
    """Compact overview table of the (already-sliced) run list."""
    headers = (
        "Issue", "Repo", "Stage", "Role", "Backend",
        "Round", "Steps", "Tool calls", "Recorded",
    )
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    rows = (_run_table_row_html(run) for run in runs)
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


def _usage_chip(text: str, css_class: str = "") -> str:
    classes = f"orch-traj-chip {css_class}".rstrip()
    return f'<span class="{classes}">{html.escape(text)}</span>'


def _run_usage_chips(run: TrajectoryRun) -> list[str]:
    usage = run.run_usage
    if usage is None:
        return []
    chips = [_usage_chip(model) for model in usage.models]
    chips.extend((
        _usage_chip(f"total {theme.fmt_num(usage.total_tokens)} tok"),
        _usage_chip(f"in {theme.fmt_num(usage.input_tokens)}"),
        _usage_chip(f"out {theme.fmt_num(usage.output_tokens)}"),
        _usage_chip(f"cache-read {theme.fmt_num(usage.cache_read_tokens)}"),
        _usage_chip(f"cache-write {theme.fmt_num(usage.cache_write_tokens)}"),
    ))
    if usage.cached_tokens:
        chips.append(_usage_chip(f"cached {theme.fmt_num(usage.cached_tokens)}"))
    if usage.turns is not None:
        chips.append(_usage_chip(f"{usage.turns} turns"))
    source = run.cost_source or "unknown"
    if run.cost_usd is None:
        cost_label = source
    else:
        cost_label = f"{source} {_fmt_cost_usd(run.cost_usd)}"
    chips.append(_usage_chip(cost_label, "cost"))
    return chips


def _run_usage_note(run: TrajectoryRun) -> str:
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


def _run_usage_html(run: TrajectoryRun) -> str:
    """Run-level usage / cost summary as a labeled chip row, plus a note.

    The run headline and codex's only usage surface: the model(s), the token
    buckets, the turn count, and the *authoritative* run cost tagged with its
    `cost_source` (`reported $0.83` / `estimated $0.79`, or the bare source
    when the run was unpriced). A pre-usage record (`run_usage is None`)
    renders nothing, so the detail card degrades to its pre-usage shape. The
    trailing note carries the two honesty points: the run cost is
    authoritative only when reported, and the per-turn strips are claude-only
    estimates that need not sum to it -- or, when the run has no per-turn
    detail (codex), that per-turn usage is unavailable for the backend.
    """
    if run.run_usage is None:
        return ""
    chips = _run_usage_chips(run)
    row = (
        '<div class="orch-traj-chips">'
        '<span class="lbl">Run usage</span>'
        f'{"".join(chips)}'
        '</div>'
    )
    note = _run_usage_note(run)
    return f'{row}<p class="orch-traj-usage-note">{html.escape(note)}</p>'


def _turn_usage_html(usage: trajectory_reader.TurnUsageView) -> str:
    """Compact per-turn usage strip drawn above the first entry of a turn.

    `model · in N tok · out N tok · cache-read N · cache-write N · est. $X`,
    with a *cache hit* chip when the turn read from cache -- the direct answer
    to "was the cache used". The cost is always an estimate (the strip never
    carries the authoritative run figure), so it is labelled `est.`; an
    unpriced turn reads `est. n/a`.
    """
    segments = []
    if usage.model:
        segments.append(
            '<span class="orch-traj-turn-model">'
            f'{html.escape(usage.model)}</span>'
        )
    est_cost = (
        "est. n/a"
        if usage.cost_usd is None
        else f"est. {_fmt_cost_usd(usage.cost_usd, decimals=4)}"
    )
    for text in (
        f"in {theme.fmt_num(usage.input_tokens)} tok",
        f"out {theme.fmt_num(usage.output_tokens)} tok",
        f"cache-read {theme.fmt_num(usage.cache_read_tokens)}",
        f"cache-write {theme.fmt_num(usage.cache_write_tokens)}",
        est_cost,
    ):
        segments.append(f"<span>{text}</span>")
    cache_hit = (
        '<span class="orch-traj-cache-hit">cache hit</span>'
        if usage.cache_read_tokens > 0
        else ""
    )
    return (
        '<div class="orch-traj-turn">'
        f'{_USAGE_SEP.join(segments)}{cache_hit}'
        '</div>'
    )


def _timeline_with_usage(
    run: TrajectoryRun,
) -> list[
    tuple[Optional[trajectory_reader.TurnUsageView],
          trajectory_reader.TimelineEntry]
]:
    """Pair each timeline entry with the per-turn usage strip to draw above it.

    The strip belongs on the *first* entry of each assistant turn: a new turn
    starts when an entry's `turn` differs from the last one seen, so that entry
    pairs with the turn's usage while every later entry of the same turn -- and
    every `turn=None` turn input (tool results, user turns) between turns --
    pairs with `None`. A turn the sink's budget dropped from `turns[]` pairs
    with `None` too, but still advances the boundary so its siblings do not
    re-probe for it. A codex or pre-usage run has `turn=None` throughout, so
    every entry pairs with `None` and no strip renders.
    """
    paired: list[
        tuple[Optional[trajectory_reader.TurnUsageView],
              trajectory_reader.TimelineEntry]
    ] = []
    prev_turn: Optional[int] = None
    for entry in run.timeline:
        strip = None
        if entry.turn is not None and entry.turn != prev_turn:
            strip = run.usage_for_turn(entry.turn)
            prev_turn = entry.turn
        paired.append((strip, entry))
    return paired


def _run_picker_label(run: TrajectoryRun) -> str:
    """The run's per-run picker label (`detail_label`), prefixed when it
    is a synthetic fixture.

    The repo and issue live in their own cascading selectors above this
    one, so the per-run picker shows only the `detail_label` cohort
    (stage/role · backend · round · ts), not the full `label`.
    """
    label = run.detail_label()
    return f"{_FIXTURE_LABEL_PREFIX}{label}" if run.is_fixture else label


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
    st.markdown(
        _card_header_html(
            f"Run #{run.issue} · {run.repo or 'unknown repo'}",
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
        repo_choice = st.selectbox("Repo", ("All", *options.repos), index=0)
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
    return st.selectbox("Repo", repos)


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
