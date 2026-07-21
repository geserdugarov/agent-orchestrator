# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Inline-HTML rendering for the per-skill session-adoption matrix.

The primary skill panel: one row per `(repo, agent_role, backend, skill)`
cell from `get_skill_adoption`, counting skill use by *logical agent
session* rather than by raw agent run. It answers "what share of the
sessions that had a skill available actually loaded it" -- the headline
adoption metric -- and carries the window-scoped invocation diagnostics
(load and incidental counts) alongside it. Its column headers are
clickable sort controls that write `adopt_sort` / `adopt_dir` query
params (`parse_skill_adoption_sort` reads them back), so this module owns
both the sort-param parsing and the sortable-table markup.

The shared compact-table CSS / wrapper primitives (`_table_css`,
`_table_html`) and the `_UNKNOWN` placeholder stay in
`orchestrator.dashboard_html`; this module imports them so the matrix
carries the same table chrome as its sibling panels. It mirrors the
invocation-level `orchestrator.dashboard_skill_matrix` module, which
renders as a diagnostic beneath this one.

`orchestrator.dashboard` re-exports `_skill_adoption_html` and
`parse_skill_adoption_sort` from here under their original names, so the
`orchestrator.dashboard.*` surface (and its test patch points) resolve
unchanged.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Optional, Sequence

from orchestrator.analytics.read import SkillAdoptionRow
from orchestrator.dashboard_html import _UNKNOWN, _table_css, _table_html


# Shown in place of the adoption table when `get_skill_adoption` returns
# no rows: no `agent_exit` run in the window loaded or referenced a skill
# whose session had it available. Names the `TRACK_SKILL_TRIGGERS` switch
# (the same caveat the invocation-level views carry) so a quiet panel is
# not mistaken for a bug.
SKILL_ADOPTION_EMPTY_MESSAGE = (
    "No per-session skill adoption for this window. The table counts, per "
    "logical agent session, how many had each skill available and how many "
    "loaded it; it fills in once `TRACK_SKILL_TRIGGERS` (default off) has "
    "recorded at least one session's available and loaded skills."
)

_SKILL_ADOPTION_EXTRA_CSS = """
  .orch-skilladopt td.strong { font-weight: 600; color: var(--orch-ink); }
  .orch-skilladopt-zero { color: var(--orch-muted-soft); }
  .orch-skilladopt thead th a.orch-skilladopt-h { color: inherit;
    text-decoration: none; cursor: pointer; }
  .orch-skilladopt thead th a.orch-skilladopt-h:hover {
    color: var(--orch-ink); text-decoration: underline; }
  .orch-skilladopt-sort { margin-left: 3px; color: var(--orch-accent); }
"""


@dataclass(frozen=True)
class _SkillAdoptionColumn:
    key: str
    label: str
    right_aligned: bool
    sort_value: Callable[[SkillAdoptionRow], object]


# Column model for the per-skill adoption matrix. The key is the stable
# identifier a clickable header encodes into the `adopt_sort` query param,
# and the sort function pulls exactly the value the column shows. The five
# metric columns are the session denominator / numerator / rate followed
# by the two window-scoped invocation diagnostics.
_SKILL_ADOPTION_COLUMNS = (
    _SkillAdoptionColumn("repo", "Repo", False, lambda row: (row.repo or "").lower()),
    _SkillAdoptionColumn("role", "Role", False, lambda row: (row.agent_role or "").lower()),
    _SkillAdoptionColumn("backend", "Backend", False, lambda row: (row.backend or "").lower()),
    _SkillAdoptionColumn("skill", "Skill", False, lambda row: (row.skill or "").lower()),
    _SkillAdoptionColumn("sessions", "Sessions", True, lambda row: int(row.sessions)),
    _SkillAdoptionColumn(
        "adopted", "Sessions using skill", True, lambda row: int(row.adopted),
    ),
    _SkillAdoptionColumn("rate", "Adoption rate", True, lambda row: row.adoption_rate),
    _SkillAdoptionColumn(
        "loads", "Invocation loads", True, lambda row: int(row.load_rows),
    ),
    _SkillAdoptionColumn(
        "incidental", "Incidental references", True, lambda row: int(row.incidental),
    ),
)

# Numeric columns default a first click to descending (largest first is
# the interesting end for session / rate counts); text columns default to
# ascending (A→Z). Re-clicking the active column flips its direction.
_SKILL_ADOPTION_NUMERIC_KEYS = frozenset(
    ("sessions", "adopted", "rate", "loads", "incidental"),
)

_SKILL_ADOPTION_SORT_KEYS = MappingProxyType({
    column.key: column.sort_value for column in _SKILL_ADOPTION_COLUMNS
})

# Query-param names the clickable headers write and the dashboard reads
# back via `parse_skill_adoption_sort`.
SKILL_ADOPTION_SORT_PARAM = "adopt_sort"
SKILL_ADOPTION_DIR_PARAM = "adopt_dir"


def parse_skill_adoption_sort(params) -> tuple[Optional[str], bool]:
    """Resolve the adoption-matrix sort column + direction from query params.

    Reads the `adopt_sort` / `adopt_dir` params the clickable headers
    encode and returns a `(column key, descending)` pair. An unknown or
    absent `adopt_sort` degrades to `(None, False)` -- the default view
    (repo ascending, then adoption rate descending) -- instead of raising,
    so a stale or hand-edited URL never breaks the render. `adopt_dir ==
    "desc"` sorts descending; anything else ascending.
    """
    key = params.get(SKILL_ADOPTION_SORT_PARAM)
    if key not in _SKILL_ADOPTION_SORT_KEYS:
        return None, False
    return key, params.get(SKILL_ADOPTION_DIR_PARAM) == "desc"


def _sort_skill_adoption_rows(
    rows: Sequence[SkillAdoptionRow],
    sort_key: Optional[str],
    descending: bool,
) -> list[SkillAdoptionRow]:
    """Sort adoption rows by a column key; identity order when key is unknown.

    Python's sort is stable, so rows sharing a sort value keep the read
    model's order (sessions DESC then adopted DESC then invocations DESC
    then a stable repo/role/backend/skill tiebreak) as the secondary
    ordering.
    """
    keyfn = _SKILL_ADOPTION_SORT_KEYS.get(sort_key)
    if keyfn is None:
        return list(rows)
    return sorted(rows, key=keyfn, reverse=descending)


def _default_sort_skill_adoption_rows(
    rows: Sequence[SkillAdoptionRow],
) -> list[SkillAdoptionRow]:
    """Order the matrix for its default view (no header column selected).

    Repo ascending (A→Z), then adoption rate descending within each repo,
    so each repo's most-adopted skills lead. Python's sort is stable, so
    rows tying on both keys keep the read model's order (sessions DESC then
    adopted DESC then invocations DESC then a stable repo/role/backend/skill
    tiebreak).
    """
    return sorted(rows, key=_skill_adoption_default_sort_key)


def _skill_adoption_default_sort_key(
    row: SkillAdoptionRow,
) -> tuple[str, float]:
    repo = (row.repo or "").lower()
    rate = -row.adoption_rate
    return repo, rate


@dataclass(frozen=True)
class _SkillAdoptionHeaderState:
    direction: str
    arrow: str


def _skill_adoption_header_state(
    column: _SkillAdoptionColumn,
    active_key: Optional[str],
    descending: bool,
) -> _SkillAdoptionHeaderState:
    if column.key == active_key:
        direction = "asc" if descending else "desc"
        arrow = "▼" if descending else "▲"
        return _SkillAdoptionHeaderState(direction=direction, arrow=arrow)
    if column.key in _SKILL_ADOPTION_NUMERIC_KEYS:
        return _SkillAdoptionHeaderState(direction="desc", arrow="")
    return _SkillAdoptionHeaderState(direction="asc", arrow="")


def _skill_adoption_header_cell(
    column: _SkillAdoptionColumn,
    active_key: Optional[str],
    descending: bool,
) -> str:
    state = _skill_adoption_header_state(column, active_key, descending)
    cell_class = ' class="r"' if column.right_aligned else ""
    arrow_html = ""
    if state.arrow:
        arrow_html = f'<span class="orch-skilladopt-sort">{state.arrow}</span>'
    return (
        f"<th{cell_class}>"
        f'<a class="orch-skilladopt-h" '
        f'href="?{SKILL_ADOPTION_SORT_PARAM}={column.key}'
        f'&{SKILL_ADOPTION_DIR_PARAM}={state.direction}" target="_self">'
        f"{html.escape(column.label)}</a>{arrow_html}"
        "</th>"
    )


def _skill_adoption_header_html(active_key: Optional[str], descending: bool) -> str:
    """Render the matrix `<thead>` with clickable, sortable column headers.

    Each header is an anchor whose href writes the `adopt_sort` /
    `adopt_dir` query params for that column: clicking the active column
    flips its direction, clicking any other selects it at its default
    direction (descending for numeric columns, ascending for text). The
    active column carries a ▲ / ▼ indicator so the current sort is
    visible. `target="_self"` keeps the navigation in-tab so Streamlit
    reruns in place rather than opening a new window.
    """
    cells = (
        _skill_adoption_header_cell(column, active_key, descending)
        for column in _SKILL_ADOPTION_COLUMNS
    )
    cells_html = "".join(cells)
    return f"<thead><tr>{cells_html}</tr></thead>"


def _muted_zero_html(text: str) -> str:
    return f'<span class="orch-skilladopt-zero">{text}</span>'


def _adoption_count_html(count: int) -> str:
    """Render a count cell, muting a `0` so quiet cells read distinctly."""
    if count == 0:
        return _muted_zero_html("0")
    return str(count)


def _adoption_rate_html(row: SkillAdoptionRow) -> str:
    """Render the adoption-rate cell.

    A cell with no available session (`sessions == 0`) has an undefined
    rate -- it exists only for its window diagnostics (a purely incidental
    reference, or a load whose session reported a different availability
    set) -- so it renders a muted em-dash rather than a misleading `0%`
    that would imply the skill was offered and ignored. Otherwise the rate
    is `adopted / sessions`; a `0%` (nothing adopted) is muted so it reads
    distinctly from an active rate.
    """
    if row.sessions == 0:
        return _muted_zero_html("—")
    rate_pct = row.adoption_rate * 100.0
    if row.adopted == 0:
        return _muted_zero_html("0%")
    return f"{rate_pct:.0f}%"


@dataclass(frozen=True)
class _SkillAdoptionRowView:
    repo: str
    role: str
    backend: str
    skill: str
    sessions_html: str
    adopted_html: str
    rate_html: str
    loads_html: str
    incidental_html: str


def _skill_adoption_row_view(row: SkillAdoptionRow) -> _SkillAdoptionRowView:
    return _SkillAdoptionRowView(
        repo=row.repo or _UNKNOWN,
        role=row.agent_role or _UNKNOWN,
        backend=row.backend or _UNKNOWN,
        skill=row.skill or _UNKNOWN,
        sessions_html=_adoption_count_html(int(row.sessions)),
        adopted_html=_adoption_count_html(int(row.adopted)),
        rate_html=_adoption_rate_html(row),
        loads_html=_adoption_count_html(int(row.load_rows)),
        incidental_html=_adoption_count_html(int(row.incidental)),
    )


def _skill_adoption_row_html(row: SkillAdoptionRow) -> str:
    view = _skill_adoption_row_view(row)
    return (
        "<tr>"
        f'<td class="strong">{html.escape(view.repo)}</td>'
        f'<td>{html.escape(view.role)}</td>'
        f'<td>{html.escape(view.backend)}</td>'
        f'<td>{html.escape(view.skill)}</td>'
        f'<td class="r">{view.sessions_html}</td>'
        f'<td class="r">{view.adopted_html}</td>'
        f'<td class="r">{view.rate_html}</td>'
        f'<td class="r">{view.loads_html}</td>'
        f'<td class="r">{view.incidental_html}</td>'
        "</tr>"
    )


def _skill_adoption_html(
    rows: Sequence[SkillAdoptionRow],
    *,
    sort_key: Optional[str] = None,
    descending: bool = False,
) -> str:
    """Render the per-skill session-adoption matrix to inline HTML.

    The primary skill panel: one row per `(repo, agent_role, backend,
    skill)` cell from `get_skill_adoption`, with columns Repo / Role /
    Backend / Skill / Sessions / Sessions using skill / Adoption rate /
    Invocation loads / Incidental references. `Sessions` is how many
    logical sessions in the cohort had the skill available, `Sessions
    using skill` the subset that loaded it, and `Adoption rate` their share
    (`adopted / sessions`) -- counted once per session, so a resume chain
    that pulled a skill across three ticks weighs one, not three. The two
    trailing columns are window-scoped invocation diagnostics: `Invocation
    loads` counts the window runs that loaded the skill and `Incidental
    references` the window runs that referenced its `SKILL.md` without
    loading it. An incidental reference never counts toward availability or
    adoption, so a purely-incidental cell reads as `0` sessions / `0`
    adopted / `—` rate with its own `Incidental references` count -- an
    incidental mention can never raise the adoption rate.

    The column headers are clickable sort controls: each is an anchor that
    writes `adopt_sort` / `adopt_dir` query params, and the caller passes
    the parsed `(sort_key, descending)` back in so the rows are re-sorted
    on that column and the active header shows a ▲ / ▼ indicator. With no
    `sort_key` the rows default to repo ascending, then adoption rate
    descending within each repo.

    When the read model returns no rows -- no window run loaded or
    referenced a skill -- a clear fallback notice
    (`SKILL_ADOPTION_EMPTY_MESSAGE`) is rendered in place of the table.
    Rendered as inline HTML (matching the sibling tables) so it reads
    cleanly even when every rate is `0%`; the local CSS sits inline next to
    the table and reuses the shared `var(--orch-*)` theme tokens.
    """
    if len(rows) == 0:
        # Self-contained inline style so the notice still reads as muted
        # body copy without the table's `<style>` block (skipped on this
        # early-return path).
        return (
            '<div class="orch-skilladopt-empty" '
            'style="color:var(--orch-muted);font-size:12.5px;'
            'padding:8px 2px">'
            f"{html.escape(SKILL_ADOPTION_EMPTY_MESSAGE)}"
            "</div>"
        )
    if sort_key is None:
        rows = _default_sort_skill_adoption_rows(rows)
    else:
        rows = _sort_skill_adoption_rows(rows, sort_key, descending)
    return _table_html(
        table_class="orch-skilladopt",
        css=_table_css(
            "orch-skilladopt", extra_rules=_SKILL_ADOPTION_EXTRA_CSS
        ),
        head=_skill_adoption_header_html(sort_key, descending),
        rows=[_skill_adoption_row_html(row) for row in rows],
    )
