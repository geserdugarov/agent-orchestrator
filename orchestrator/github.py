# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable PyGithub client surface for workflow and operator code.

Issue, state-comment, pull-request, feedback, checks, and internal-query
responsibilities live in focused mixin leaves. ``GitHubClient`` retains the
historical constructor and every method/import path through that composed
surface.
"""
from __future__ import annotations

from typing import Optional

from github import Auth, Github
from github.Label import Label
from github.Repository import Repository

from orchestrator import _github_api, config

PINNED_STATE_MARKER = _github_api.PINNED_STATE_MARKER
PINNED_STATE_RE = _github_api.PINNED_STATE_RE
PINNED_STATE_BODY_RE = _github_api.PINNED_STATE_BODY_RE
PINNED_STATE_TEMPLATE = _github_api.PINNED_STATE_TEMPLATE
PinnedState = _github_api.PinnedState
_pinned_state_from_comment = _github_api.pinned_state_from_comment

WORKFLOW_LABEL_SPECS = _github_api.WORKFLOW_LABEL_SPECS
WORKFLOW_LABELS = _github_api.WORKFLOW_LABELS
BACKLOG_LABEL = _github_api.BACKLOG_LABEL
PAUSED_LABEL = _github_api.PAUSED_LABEL
COMMUNITY_CONTRIBUTION_LABEL = _github_api.COMMUNITY_CONTRIBUTION_LABEL
CONTROL_LABEL_SPECS = _github_api.CONTROL_LABEL_SPECS
HARD_SKIP_CONTROL_LABELS = _github_api.HARD_SKIP_CONTROL_LABELS
issue_has_label = _github_api.issue_has_label
hard_skip_control_label = _github_api.hard_skip_control_label

_iter_new_non_pr_issues = _github_api.iter_new_non_pr_issues
_issue_query_options = _github_api.issue_query_options
_append_event_line = _github_api.append_event_line
_write_event_record = _github_api.write_event_record
build_event_record = _github_api.build_event_record

_CheckSurfaceRead = _github_api.CheckSurfaceRead
_normalize_combined_status = _github_api.normalize_combined_status
_normalize_check_runs = _github_api.normalize_check_runs
_fold_check_states = _github_api.fold_check_states
_FAILED_CHECK_RUN_CONCLUSIONS = _github_api.failed_check_run_conclusions
_SUCCESSFUL_CHECK_RUN_CONCLUSIONS = _github_api.successful_check_run_conclusions
_CHECK_STATE_FAILURE = _github_api.check_state_failure
_CHECK_STATE_PENDING = _github_api.check_state_pending
_REVIEW_CHANGES_REQUESTED = _github_api.review_changes_requested
_review_state_for_head = _github_api.review_state_for_head
_latest_review_states_for_head = _github_api.latest_review_states_for_head
_record_latest_review = _github_api.record_latest_review
_is_actionable_review_summary = _github_api.is_actionable_review_summary

_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_ISSUE_STATE_OPEN = "open"
_RECORDED_EVENTS_CAP = 500


class GitHubClient(_github_api.GitHubClientBase):
    """Authenticated repository client with a worker-safe clone seam."""

    def __init__(
        self,
        token: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_spec: Optional["config.RepoSpec"] = None,
        *,
        bot_login: Optional[str] = None,
    ) -> None:
        slug = repo_slug or config.REPO if repo_spec is None else repo_spec.slug
        if token is None:
            token = config._resolve_github_token(slug)
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN is empty. Export it in the orchestrator's "
                "environment or write it to "
                f"~/.config/{slug}/token "
                "(override path with ORCHESTRATOR_TOKEN_FILE). "
                "Do NOT put it in REPO_ROOT/.env -- the implementer agent "
                "can read that file.",
            )
        self._gh = Github(auth=Auth.Token(token))
        self.repo: Repository = self._gh.get_repo(slug)
        self._repo_slug = slug
        self._token = token
        self._bot_login = (
            self._gh.get_user().login
            if bot_login is None
            else bot_login
        )
        self.recorded_events: list[dict] = []
        self._label_cache: dict[str, Label] = {}
        self._pollable_calls = 0
