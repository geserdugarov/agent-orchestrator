# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-worker cloning, cached reads, and analytics hooks."""
from __future__ import annotations

import logging
from typing import Optional

from github import GithubException
from github.Issue import Issue
from github.Label import Label

from orchestrator import analytics
from orchestrator import _github_checks, _github_reviews
from orchestrator._github_feedback import GitHubFeedbackMixin

log = logging.getLogger("orchestrator.github")
_HTTP_FORBIDDEN = 403


class GitHubInternalsMixin(GitHubFeedbackMixin):
    """Internal seams used by polling, checks, and worker isolation."""

    _latest_review_states_for_head = _github_reviews.LATEST_REVIEW_STATES_METHOD

    def _for_worker_thread(self):
        """Build a fresh requester/repository pair for one worker thread."""
        from orchestrator.github import GitHubClient

        return GitHubClient(
            token=self._token,
            repo_slug=self._repo_slug,
            bot_login=self._bot_login,
        )

    def _cached_label(self, name: str) -> Optional[Label]:
        """Resolve and cache a label, while leaving failures retryable."""
        cached_label = self._label_cache.get(name)
        if cached_label is not None:
            return cached_label
        try:
            label_object = self.repo.get_label(name)
        except GithubException as error:
            log.warning(
                "could not look up %r label for closed-issue sweep "
                "(HTTP %s); skipping. Externally-merged %s issues will "
                "not finalize to `done` until the label exists.",
                name,
                error.status,
                name,
            )
            return None
        self._label_cache[name] = label_object
        return label_object

    def _emit_stage_enter(self, issue: Issue, stage: str) -> None:
        """Record matching audit and analytics stage-enter events."""
        issue_number = getattr(issue, "number", 0) or 0
        self.emit_event(
            "stage_enter",
            issue_number=issue_number,
            stage=stage,
        )
        analytics.record_stage_enter(
            repo=self._repo_slug,
            issue=issue_number,
            stage=stage,
        )

    def _read_combined_status(
        self,
        head_sha: str,
    ) -> _github_checks.CheckSurfaceRead:
        """Read and normalize the legacy commit-status surface."""
        try:
            combined_status = (
                self.repo.get_commit(head_sha).get_combined_status()
            )
        except GithubException as error:
            log.warning(
                "could not read combined status for %s (HTTP %s); ignoring",
                head_sha,
                error.status,
            )
            return _github_checks.CheckSurfaceRead(read_failed=True)
        return _github_checks.CheckSurfaceRead(
            state=_github_checks.normalize_combined_status(combined_status),
        )

    def _read_check_runs(
        self,
        head_sha: str,
    ) -> _github_checks.CheckSurfaceRead:
        """Read and normalize the check-runs surface."""
        try:
            return _github_checks.CheckSurfaceRead(
                state=_github_checks.normalize_check_runs(
                    self.repo.get_commit(head_sha).get_check_runs(),
                ),
            )
        except GithubException as error:
            if error.status == _HTTP_FORBIDDEN:
                log.error(
                    "could not read check-runs for %s (HTTP 403). The "
                    "orchestrator PAT needs 'Checks: read' to evaluate "
                    "GitHub Actions PRs. Without it, check_state is "
                    "reported as 'none' on Actions-only PRs. Add the "
                    "permission and restart.",
                    head_sha,
                )
            else:
                log.warning(
                    "could not read check-runs for %s (HTTP %s); ignoring",
                    head_sha,
                    error.status,
                )
            return _github_checks.CheckSurfaceRead(read_failed=True)
