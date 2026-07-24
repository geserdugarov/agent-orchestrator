# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pull-request feedback watermarks and label bootstrap."""
from __future__ import annotations

import logging
from typing import Optional

from github import GithubException
from github.IssueComment import IssueComment
from github.PullRequest import PullRequest

from orchestrator import _github_labels, _github_reviews
from orchestrator._github_pull_checks import GitHubPullChecksMixin
from orchestrator.github import pinned_state

log = logging.getLogger("orchestrator.github")


class GitHubFeedbackMixin(GitHubPullChecksMixin):
    """Unread PR feedback surfaces and repository label bootstrap."""

    def pr_conversation_comments_after(
        self,
        pr: PullRequest,
        after_id: Optional[int],
    ) -> list[IssueComment]:
        """Return PR conversation comments newer than their watermark."""
        pr_comments: list[IssueComment] = []
        for pr_comment in pr.get_issue_comments():
            if pinned_state.PINNED_STATE_MARKER in (pr_comment.body or ""):
                continue
            if after_id is None or pr_comment.id > after_id:
                pr_comments.append(pr_comment)
        pr_comments.sort(key=lambda comment: comment.id)
        return pr_comments

    def pr_inline_comments_after(
        self,
        pr: PullRequest,
        after_id: Optional[int],
    ) -> list:
        """Return inline review comments newer than their own watermark."""
        review_comments: list = []
        for review_comment in pr.get_review_comments():
            if pinned_state.PINNED_STATE_MARKER in (review_comment.body or ""):
                continue
            if after_id is None or review_comment.id > after_id:
                review_comments.append(review_comment)
        review_comments.sort(key=lambda comment: comment.id)
        return review_comments

    def pr_reviews_after(
        self,
        pr: PullRequest,
        after_id: Optional[int],
    ) -> list:
        """Return actionable review summaries newer than their watermark."""
        review_summaries = [
            candidate_review
            for candidate_review in pr.get_reviews()
            if _github_reviews.is_actionable_review_summary(
                candidate_review,
                after_id,
            )
        ]
        review_summaries.sort(key=lambda review_summary: review_summary.id)
        return review_summaries

    def ensure_workflow_labels(self) -> None:
        """Best-effort creation of missing workflow and control labels."""
        try:
            existing_labels = {
                repo_label.name
                for repo_label in self.repo.get_labels()
            }
        except GithubException as error:
            log.warning(
                "could not list labels (HTTP %s); skipping label bootstrap. "
                "Grant the PAT 'Issues: Read and write' to enable.",
                error.status,
            )
            return
        label_specs = (
            _github_labels.WORKFLOW_LABEL_SPECS
            + _github_labels.CONTROL_LABEL_SPECS
        )
        for name, color, description in label_specs:
            if name in existing_labels:
                continue
            try:
                self.repo.create_label(
                    name=name,
                    color=color,
                    description=description,
                )
            except GithubException as error:
                log.error(
                    "could not create label %r (HTTP %s). "
                    "Fine-grained PAT needs 'Issues: Read and write'. "
                    "Skipping remaining label bootstrap; orchestrator will "
                    "keep running and may retry on the next restart.",
                    name,
                    error.status,
                )
                return
            log.info("created label %r", name)
