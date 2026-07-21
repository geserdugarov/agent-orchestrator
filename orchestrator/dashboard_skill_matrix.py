# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Inline-HTML rendering for the invocation-level per-skill trigger matrix.

The fold-out table under the skill panel's invocation-level diagnostics
expander (beneath the primary session-adoption matrix) pairs each repo's
offered-skill catalog with the skills its runs triggered. Its column
headers are clickable sort controls that write `mtx_sort` / `mtx_dir`
query params (`parse_skill_matrix_sort` reads them back), so this module
owns both the sort-param parsing and the sortable-table markup.

The shared compact-table CSS / wrapper primitives (`_table_css`,
`_table_html`) and the `_UNKNOWN` placeholder stay in
`orchestrator.dashboard_html`; this module imports them so the matrix
carries the same table chrome as its sibling panels.

`orchestrator.dashboard` re-exports `_skill_matrix_html` and
`parse_skill_matrix_sort` from here under their original names, so the
historical `orchestrator.dashboard.*` surface (and its test patch
points) resolve unchanged.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Optional, Sequence

from orchestrator.analytics.read import SkillTriggerMatrixRow
from orchestrator.dashboard_html import _UNKNOWN, _table_css, _table_html


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

_SKILL_MATRIX_EXTRA_CSS = """
  .orch-skillmatrix td.strong { font-weight: 600; color: var(--orch-ink); }
  .orch-skillmatrix-zero { color: var(--orch-muted-soft); }
  .orch-skillmatrix thead th a.orch-skillmatrix-h { color: inherit;
    text-decoration: none; cursor: pointer; }
  .orch-skillmatrix thead th a.orch-skillmatrix-h:hover {
    color: var(--orch-ink); text-decoration: underline; }
  .orch-skillmatrix-sort { margin-left: 3px; color: var(--orch-accent); }
"""


@dataclass(frozen=True)
class _SkillMatrixColumn:
    key: str
    label: str
    right_aligned: bool
    sort_value: Callable[[SkillTriggerMatrixRow], object]


# Column model for the per-skill trigger matrix. The key is the stable
# identifier a clickable header encodes into the `mtx_sort` query param,
# and the sort function pulls exactly the value the column shows.
_SKILL_MATRIX_COLUMNS = (
    _SkillMatrixColumn("repo", "Repo", False, lambda row: (row.repo or "").lower()),
    _SkillMatrixColumn("role", "Role", False, lambda row: (row.agent_role or "").lower()),
    _SkillMatrixColumn("backend", "Backend", False, lambda row: (row.backend or "").lower()),
    _SkillMatrixColumn("skill", "Skill", False, lambda row: (row.skill or "").lower()),
    _SkillMatrixColumn("runs", "Runs", True, lambda row: int(row.runs)),
    _SkillMatrixColumn("skill_runs", "Runs with skill", True, lambda row: int(row.skill_runs)),
    _SkillMatrixColumn("rate", "Trigger rate", True, lambda row: row.rate),
)

# Numeric columns default a first click to descending (largest first is
# the interesting end for run / rate counts); text columns default to
# ascending (A→Z). Re-clicking the active column flips its direction.
_SKILL_MATRIX_NUMERIC_KEYS = frozenset(("runs", "skill_runs", "rate"))

_SKILL_MATRIX_SORT_KEYS = MappingProxyType({
    column.key: column.sort_value for column in _SKILL_MATRIX_COLUMNS
})

# Query-param names the clickable headers write and the dashboard reads
# back via `parse_skill_matrix_sort`.
SKILL_MATRIX_SORT_PARAM = "mtx_sort"
SKILL_MATRIX_DIR_PARAM = "mtx_dir"


def parse_skill_matrix_sort(params) -> tuple[Optional[str], bool]:
    """Resolve the matrix sort column + direction from query params.

    Reads the `mtx_sort` / `mtx_dir` params the clickable headers encode
    and returns a `(column key, descending)` pair. An unknown or absent
    `mtx_sort` degrades to `(None, False)` -- the default view (repo
    ascending, then trigger rate descending) -- instead of raising, so a
    stale or hand-edited URL never breaks the render. `mtx_dir == "desc"`
    sorts descending; anything else ascending.
    """
    key = params.get(SKILL_MATRIX_SORT_PARAM)
    if key not in _SKILL_MATRIX_SORT_KEYS:
        return None, False
    return key, params.get(SKILL_MATRIX_DIR_PARAM) == "desc"


def _sort_skill_matrix_rows(
    rows: Sequence[SkillTriggerMatrixRow],
    sort_key: Optional[str],
    descending: bool,
) -> list[SkillTriggerMatrixRow]:
    """Sort matrix rows by a column key; identity order when key is unknown.

    Python's sort is stable, so rows sharing a sort value keep the read
    model's order (Runs-with-skill DESC then Runs DESC then a stable
    repo/role/backend/skill tiebreak) as the secondary ordering.
    """
    keyfn = _SKILL_MATRIX_SORT_KEYS.get(sort_key)
    if keyfn is None:
        return list(rows)
    return sorted(rows, key=keyfn, reverse=descending)


def _default_sort_skill_matrix_rows(
    rows: Sequence[SkillTriggerMatrixRow],
) -> list[SkillTriggerMatrixRow]:
    """Order the matrix for its default view (no header column selected).

    Repo ascending (A→Z), then trigger rate descending within each repo,
    so each repo's hottest skills lead. Python's sort is stable, so rows
    tying on both keys keep the read model's order (Runs-with-skill DESC
    then Runs DESC then a stable repo/role/backend/skill tiebreak).
    """
    return sorted(rows, key=_skill_matrix_default_sort_key)


def _skill_matrix_default_sort_key(
    row: SkillTriggerMatrixRow,
) -> tuple[str, float]:
    repo = (row.repo or "").lower()
    rate = -row.rate
    return repo, rate


@dataclass(frozen=True)
class _SkillMatrixHeaderState:
    direction: str
    arrow: str


def _skill_matrix_header_state(
    column: _SkillMatrixColumn,
    active_key: Optional[str],
    descending: bool,
) -> _SkillMatrixHeaderState:
    if column.key == active_key:
        direction = "asc" if descending else "desc"
        arrow = "▼" if descending else "▲"
        return _SkillMatrixHeaderState(direction=direction, arrow=arrow)
    if column.key in _SKILL_MATRIX_NUMERIC_KEYS:
        return _SkillMatrixHeaderState(direction="desc", arrow="")
    return _SkillMatrixHeaderState(direction="asc", arrow="")


def _skill_matrix_header_cell(
    column: _SkillMatrixColumn,
    active_key: Optional[str],
    descending: bool,
) -> str:
    state = _skill_matrix_header_state(column, active_key, descending)
    cell_class = ' class="r"' if column.right_aligned else ""
    arrow_html = ""
    if state.arrow:
        arrow_html = f'<span class="orch-skillmatrix-sort">{state.arrow}</span>'
    return (
        f"<th{cell_class}>"
        f'<a class="orch-skillmatrix-h" '
        f'href="?{SKILL_MATRIX_SORT_PARAM}={column.key}'
        f'&{SKILL_MATRIX_DIR_PARAM}={state.direction}" target="_self">'
        f"{html.escape(column.label)}</a>{arrow_html}"
        "</th>"
    )


def _skill_matrix_header_html(active_key: Optional[str], descending: bool) -> str:
    """Render the matrix `<thead>` with clickable, sortable column headers.

    Each header is an anchor whose href writes the `mtx_sort` / `mtx_dir`
    query params for that column: clicking the active column flips its
    direction, clicking any other selects it at its default direction
    (descending for numeric columns, ascending for text). The active
    column carries a ▲ / ▼ indicator so the current sort is visible.
    `target="_self"` keeps the navigation in-tab so Streamlit reruns in
    place rather than opening a new window.
    """
    cells = (
        _skill_matrix_header_cell(column, active_key, descending)
        for column in _SKILL_MATRIX_COLUMNS
    )
    cells_html = "".join(cells)
    return f"<thead><tr>{cells_html}</tr></thead>"


def _muted_zero_html(text: str) -> str:
    return f'<span class="orch-skillmatrix-zero">{text}</span>'


@dataclass(frozen=True)
class _SkillMatrixRowView:
    repo: str
    role: str
    backend: str
    skill: str
    runs: int
    skill_runs_html: str
    rate_html: str


def _skill_matrix_row_view(row: SkillTriggerMatrixRow) -> _SkillMatrixRowView:
    skill_runs = int(row.skill_runs)
    if skill_runs == 0:
        skill_runs_html = _muted_zero_html("0")
        rate_html = _muted_zero_html("0%")
    else:
        skill_runs_html = str(skill_runs)
        rate_pct = row.rate * 100.0
        rate_html = f"{rate_pct:.0f}%"
    return _SkillMatrixRowView(
        repo=row.repo or _UNKNOWN,
        role=row.agent_role or _UNKNOWN,
        backend=row.backend or _UNKNOWN,
        skill=row.skill or _UNKNOWN,
        runs=int(row.runs),
        skill_runs_html=skill_runs_html,
        rate_html=rate_html,
    )


def _skill_matrix_row_html(row: SkillTriggerMatrixRow) -> str:
    view = _skill_matrix_row_view(row)
    return (
        "<tr>"
        f'<td class="strong">{html.escape(view.repo)}</td>'
        f'<td>{html.escape(view.role)}</td>'
        f'<td>{html.escape(view.backend)}</td>'
        f'<td>{html.escape(view.skill)}</td>'
        f'<td class="r">{view.runs}</td>'
        f'<td class="r">{view.skill_runs_html}</td>'
        f'<td class="r">{view.rate_html}</td>'
        "</tr>"
    )


def _skill_matrix_html(
    rows: Sequence[SkillTriggerMatrixRow],
    *,
    sort_key: Optional[str] = None,
    descending: bool = False,
) -> str:
    """Render the per-skill trigger matrix to inline HTML.

    The fold-out table under the skill panel's invocation-level
    diagnostics expander: one row per `(repo, agent_role, backend,
    skill)` cell from `get_skill_trigger_matrix`, with columns Repo /
    Role / Backend /
    Skill / Runs / Runs with skill / Trigger rate. `Runs` is the total
    agent-exit runs in the cell's cohort, `Runs with skill` the subset
    that fired this skill, and `Trigger rate` the share of the two
    (`skill_runs / runs`), so a low/zero trigger count reads against the
    cohort size instead of in a vacuum. Unlike the aggregate table above it,
    this one folds in each repo's `repo_skill_catalog` so a skill the
    repo offers but no cohort triggered surfaces as an explicit `0`
    "Runs with skill" cell rather than a missing row; that zero cell and
    its `0%` trigger rate are muted so the offered-but-quiet skills read
    distinctly from the ones that actually fired. The cohort `Runs` total
    is always `>= 1` (a cell exists only for a cohort that ran), so it is
    never muted.

    The column headers are clickable sort controls: each is an anchor
    that writes `mtx_sort` / `mtx_dir` query params, and the caller
    passes the parsed `(sort_key, descending)` back in so the rows are
    re-sorted on that column and the active header shows a ▲ / ▼
    indicator. With no `sort_key` the rows default to repo ascending,
    then trigger rate descending within each repo.

    When the read model returns no rows -- no catalog records matched the
    window and no run fired a skill -- there is no catalog-backed matrix
    to build, so a clear fallback notice (`SKILL_MATRIX_EMPTY_MESSAGE`)
    is rendered in place of the table. Rendered as inline HTML (matching
    the aggregate table) so it reads cleanly even when every cell is `0`;
    the local CSS sits inline next to the table and reuses the shared
    `var(--orch-*)` theme tokens.
    """
    if len(rows) == 0:
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
    if sort_key is None:
        rows = _default_sort_skill_matrix_rows(rows)
    else:
        rows = _sort_skill_matrix_rows(rows, sort_key, descending)
    return _table_html(
        table_class="orch-skillmatrix",
        css=_table_css(
            "orch-skillmatrix", extra_rules=_SKILL_MATRIX_EXTRA_CSS
        ),
        head=_skill_matrix_header_html(sort_key, descending),
        rows=[_skill_matrix_row_html(row) for row in rows],
    )
