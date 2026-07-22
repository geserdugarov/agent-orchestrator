# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable dashboard HTML surface backed by focused rendering leaves."""
from __future__ import annotations

from orchestrator import _dashboard_issue_table as issues
from orchestrator import _dashboard_skill_trigger_table as skill_triggers
from orchestrator import _dashboard_sparkline_data as sparkline_data
from orchestrator import _dashboard_sparkline_html as sparkline_html
from orchestrator import _dashboard_summary_html as summary
from orchestrator import _dashboard_table_html as tables


_UNKNOWN = skill_triggers.UNKNOWN
_EPSILON = sparkline_data._EPSILON
_table_css = tables._table_css
_table_head_html = tables._table_head_html
_table_html = tables._table_html
_relative_width_pct = tables._relative_width_pct
_short_repo_name = tables._short_repo_name
_sparkline_y = sparkline_data._sparkline_y
_int_or_zero = tables._int_or_zero
_money_or_dash = tables._money_or_dash
_plural_s = summary._plural_s
_SparklineLayout = sparkline_data._SparklineLayout
_SparklinePaths = sparkline_data._SparklinePaths
_sparkline_step = sparkline_data._sparkline_step
_sparkline_layout = sparkline_data._sparkline_layout
_sparkline_point = sparkline_data._sparkline_point
_sparkline_points = sparkline_data._sparkline_points
_sparkline_paths = sparkline_html._sparkline_paths
_sparkline_point_text = sparkline_html._sparkline_point_text
_sparkline_area_path = sparkline_html._sparkline_area_path
_sparkline_svg = sparkline_html._sparkline_svg
_delta_pill = summary._delta_pill
_topbar_html = summary._topbar_html
_filter_meta_html = summary._filter_meta_html
_kpi_strip_html = summary._kpi_strip_html
_ISSUES_TABLE_COLUMNS = issues.ISSUES_TABLE_COLUMNS
_ISSUES_TABLE_EXTRA_CSS = issues.ISSUES_TABLE_EXTRA_CSS
_issue_status_pill = issues._issue_status_pill
_review_round_html = issues._review_round_html
_IssueRowView = issues._IssueRowView
_issue_row_view = issues._issue_row_view
_issue_table_row_html = issues._issue_table_row_html
_issues_table_html = issues._issues_table_html
_SKILL_TRIGGERS_TABLE_COLUMNS = skill_triggers.SKILL_TRIGGERS_TABLE_COLUMNS
_SKILL_TRIGGERS_EXTRA_CSS = skill_triggers.SKILL_TRIGGERS_EXTRA_CSS
_skill_trigger_row_html = skill_triggers._skill_trigger_row_html
_skill_triggers_html = skill_triggers._skill_triggers_html
