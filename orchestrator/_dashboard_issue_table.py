# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Most-expensive-issues table projection and HTML."""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Sequence

from orchestrator.analytics.read import IssueSummaryRow
from orchestrator import _dashboard_table_html as tables


ISSUES_TABLE_COLUMNS = (
    ("Issue", False),
    ("Cost", True),
    ("Runs", True),
    ("Review rds", True),
    ("Retries", True),
    ("Status", True),
)
ISSUES_TABLE_EXTRA_CSS = """
  .orch-issues td.strong { font-weight: 600; }
  .orch-issue-cell { display: flex; flex-direction: column; gap: 4px; }
  .orch-issue-name { color: var(--orch-ink); font-weight: 500; }
  .orch-issue-num { color: var(--orch-muted); font-weight: 400; margin-left: 2px; }
  .orch-issue-bar { display: block; height: 4px; border-radius: 2px;
    background: var(--orch-grid); overflow: hidden; }
  .orch-issue-bar > span { display: block; height: 100%;
    background: var(--orch-accent); border-radius: 2px; }
  .orch-pill { display: inline-block; padding: 2px 9px; border-radius: 999px;
    font-size: 11.5px; font-weight: 500;
    font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
  .orch-pill.ok { background: rgba(26, 163, 154, 0.14); color: var(--orch-success); }
  .orch-pill.bad { background: rgba(217, 83, 74, 0.14); color: var(--orch-danger); }
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
        short_repo=tables._short_repo_name(row.repo),
        cost_text=tables._money_or_dash(row.total_cost_usd),
        bar_pct=tables._relative_width_pct(
            float(row.total_cost_usd or 0),
            max_cost,
        ),
        review_rounds=tables._int_or_zero(row.max_review_round),
        retries=tables._int_or_zero(row.max_retry_count),
        failed=int(row.failed_agent_runs or 0),
    )


def _issue_table_row_html(row: IssueSummaryRow, *, max_cost: float) -> str:
    row_view = _issue_row_view(row, max_cost)
    return (
        '<tr><td><div class="orch-issue-cell">'
        f'<span><span class="orch-issue-name">{html.escape(row_view.short_repo)}</span>'
        f' <span class="orch-issue-num">#{int(row.issue)}</span></span>'
        f'<span class="orch-issue-bar"><span style="width:{row_view.bar_pct:.1f}%">'
        "</span></span></div></td>"
        f'<td class="r strong">{html.escape(row_view.cost_text)}</td>'
        f'<td class="r">{int(row.agent_exits or 0)}</td>'
        f'<td class="r">{_review_round_html(row_view.review_rounds)}</td>'
        f'<td class="r">{row_view.retries}</td>'
        f'<td class="r">{_issue_status_pill(row_view.failed)}</td></tr>'
    )


def _issues_table_html(rows: Sequence[IssueSummaryRow]) -> str:
    """Render the most-expensive-issues table to inline HTML."""
    max_cost = max(
        (float(row.total_cost_usd or 0) for row in rows),
        default=0,
    ) or 1.0
    return tables._table_html(
        table_class="orch-issues",
        css=tables._table_css(
            "orch-issues",
            extra_rules=ISSUES_TABLE_EXTRA_CSS,
        ),
        head=tables._table_head_html(ISSUES_TABLE_COLUMNS),
        rows=[_issue_table_row_html(row, max_cost=max_cost) for row in rows],
    )
