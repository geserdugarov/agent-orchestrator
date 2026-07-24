# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable PyGithub client surface for workflow and operator code.

Issue, state-comment, pull-request, feedback, checks, and internal-query
responsibilities live in focused mixin leaves. ``GitHubClient`` and the
label / query / event / check / review re-exports resolve lazily through the
module ``__getattr__``: one of those mixin leaves (`_github_pull_requests`)
imports ``orchestrator.github.pinned_state``, and importing that submodule runs
this initializer. Reading ``_github_api`` eagerly here would pull the mixin
chain -- so a leaf-first import would re-enter a half-built initializer. The
pinned-state model stays eager because its owner never re-enters the package.
"""
from __future__ import annotations

from typing import Any

from orchestrator.github import pinned_state as _pinned_state

PINNED_STATE_MARKER = _pinned_state.PINNED_STATE_MARKER
PINNED_STATE_RE = _pinned_state.PINNED_STATE_RE
PINNED_STATE_BODY_RE = _pinned_state.PINNED_STATE_BODY_RE
PINNED_STATE_TEMPLATE = _pinned_state.PINNED_STATE_TEMPLATE
PinnedState = _pinned_state.PinnedState
_pinned_state_from_comment = _pinned_state.pinned_state_from_comment

_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_ISSUE_STATE_OPEN = "open"
_RECORDED_EVENTS_CAP = 500

# Historical facade name -> attribute on the compatibility inventory. Kept out
# of module globals so __getattr__ resolves each on first access, letting this
# initializer finish before _github_api (and its mixin chain) is imported.
_LAZY_API_EXPORTS = (
    ("WORKFLOW_LABEL_SPECS", "WORKFLOW_LABEL_SPECS"),
    ("WORKFLOW_LABELS", "WORKFLOW_LABELS"),
    ("BACKLOG_LABEL", "BACKLOG_LABEL"),
    ("PAUSED_LABEL", "PAUSED_LABEL"),
    ("COMMUNITY_CONTRIBUTION_LABEL", "COMMUNITY_CONTRIBUTION_LABEL"),
    ("CONTROL_LABEL_SPECS", "CONTROL_LABEL_SPECS"),
    ("HARD_SKIP_CONTROL_LABELS", "HARD_SKIP_CONTROL_LABELS"),
    ("issue_has_label", "issue_has_label"),
    ("hard_skip_control_label", "hard_skip_control_label"),
    ("_iter_new_non_pr_issues", "iter_new_non_pr_issues"),
    ("_issue_query_options", "issue_query_options"),
    ("_append_event_line", "append_event_line"),
    ("_write_event_record", "write_event_record"),
    ("build_event_record", "build_event_record"),
    ("_CheckSurfaceRead", "CheckSurfaceRead"),
    ("_normalize_combined_status", "normalize_combined_status"),
    ("_normalize_check_runs", "normalize_check_runs"),
    ("_fold_check_states", "fold_check_states"),
    ("_FAILED_CHECK_RUN_CONCLUSIONS", "failed_check_run_conclusions"),
    ("_SUCCESSFUL_CHECK_RUN_CONCLUSIONS", "successful_check_run_conclusions"),
    ("_CHECK_STATE_FAILURE", "check_state_failure"),
    ("_CHECK_STATE_PENDING", "check_state_pending"),
    ("_REVIEW_CHANGES_REQUESTED", "review_changes_requested"),
    ("_review_state_for_head", "review_state_for_head"),
    ("_latest_review_states_for_head", "latest_review_states_for_head"),
    ("_record_latest_review", "record_latest_review"),
    ("_is_actionable_review_summary", "is_actionable_review_summary"),
)


def __getattr__(name: str) -> Any:
    """Resolve GitHubClient and the inventory re-exports on first access."""
    if name == "GitHubClient":
        from orchestrator.github import client
        return client.GitHubClient
    from orchestrator import _github_api
    for export_name, api_attr in _LAZY_API_EXPORTS:
        if name == export_name:
            return getattr(_github_api, api_attr)
    raise AttributeError(name)
