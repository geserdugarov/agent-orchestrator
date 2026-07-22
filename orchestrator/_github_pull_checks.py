# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pull-request checks, reviews, merge, and branch cleanup methods."""
from __future__ import annotations

import logging

from github import GithubException
from github.PullRequest import PullRequest

from orchestrator import _github_checks, _github_reviews
from orchestrator._github_pull_requests import GitHubPullRequestMixin

log = logging.getLogger("orchestrator.github")
_HTTP_NOT_FOUND = 404


class GitHubPullChecksMixin(GitHubPullRequestMixin):
    """Evaluate merge readiness and execute merge-side mutations."""

    def pr_combined_check_state(self, pr: PullRequest) -> str:
        """Fold legacy status and check-runs into one fail-closed state."""
        head_sha = pr.head.sha
        combined_surface = self._read_combined_status(head_sha)
        check_run_surface = self._read_check_runs(head_sha)
        return _github_checks.fold_check_states(
            (combined_surface.state, check_run_surface.state),
            read_failed=(
                combined_surface.read_failed
                or check_run_surface.read_failed
            ),
        )

    @classmethod
    def pr_has_changes_requested(
        cls,
        pr: PullRequest,
        *,
        head_sha: str,
    ) -> bool:
        """Return whether any reviewer's latest head review is a veto."""
        return any(
            review_state == _github_reviews._REVIEW_CHANGES_REQUESTED
            for review_state in cls._latest_review_states_for_head(
                pr,
                head_sha=head_sha,
            )
        )

    @classmethod
    def pr_is_approved(
        cls,
        pr: PullRequest,
        *,
        head_sha: str,
    ) -> bool:
        """Require one current-head approval and no current-head veto."""
        review_states = cls._latest_review_states_for_head(
            pr,
            head_sha=head_sha,
        )
        if not review_states:
            return False
        if any(
            review_state == _github_reviews._REVIEW_CHANGES_REQUESTED
            for review_state in review_states
        ):
            return False
        return any(
            review_state == "APPROVED"
            for review_state in review_states
        )

    def delete_remote_branch(self, branch: str) -> bool:
        """Delete a remote branch, treating an absent ref as success."""
        try:
            self.repo.get_git_ref(f"heads/{branch}").delete()
        except GithubException as error:
            if error.status == _HTTP_NOT_FOUND:
                return True
            log.warning(
                "could not delete remote branch %r (HTTP %s): %s",
                branch,
                error.status,
                error.data,
            )
            return False
        return True

    def merge_pr(
        self,
        pr: PullRequest,
        *,
        sha: str,
        method: str = "squash",
    ) -> bool:
        """Attempt one SHA-pinned merge without blind retries."""
        try:
            pr.merge(sha=sha, merge_method=method)
        except GithubException as error:
            log.warning(
                "merge failed for PR #%s (HTTP %s): %s",
                pr.number,
                error.status,
                error.data,
            )
            return False
        return True
