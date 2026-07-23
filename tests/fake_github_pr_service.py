# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pull-request services for the in-memory GitHub client."""
from __future__ import annotations

from typing import Iterable, Optional

from orchestrator.github import PINNED_STATE_MARKER

from tests.fake_github_pr_helpers import (
    _pr_combined_check_state,
    _pr_has_changes_requested,
    _pr_has_label,
    _pr_is_approved,
    _pr_is_mergeable,
    _pr_state,
)
from tests.fake_model_helpers import _review_has_feedback
from tests.fake_models import (
    FakeComment,
    FakeLabel,
    FakePR,
    FakePRReview,
    FakeUser,
)


_STATE_CLOSED = "closed"
_STATE_OPEN = "open"


class _PullCreationService:
    pr_has_label = _pr_has_label

    def open_pr(
        self,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> FakePR:
        pull_request = FakePR(
            number=next(self._pr_id),
            head_branch=branch,
            base_branch=base,
            title=title,
            body=body,
        )
        self.opened_prs.append(pull_request)
        return pull_request

    def pr_comment(self, pr_number: int, body: str) -> FakeComment:
        new_comment = FakeComment(
            id=next(self._comment_id),
            body=body,
            user=FakeUser("orchestrator"),
        )
        self.posted_pr_comments.append((pr_number, body))
        pull_request = self.pulls.get(pr_number)
        if pull_request is not None:
            pull_request.issue_comments.append(new_comment)
        return new_comment

    def find_open_pr(self, *, branch: str, base: str) -> Optional[FakePR]:
        return self.existing_open_pr.get(branch)

    def iter_open_prs(self) -> Iterable[FakePR]:
        return [
            pull_request
            for pull_request in self.pulls.values()
            if pull_request.state == _STATE_OPEN
        ]

    def add_pr_label(self, pr: FakePR, label_name: str) -> None:
        if not self.pr_has_label(pr, label_name):
            pr.labels.append(FakeLabel(label_name))

    def add_pr(self, pr: FakePR) -> None:
        self.pulls[pr.number] = pr

    def get_pr(self, pr_number: int) -> FakePR:
        return self.pulls[pr_number]


class _PullStatusService:
    pr_state = _pr_state
    pr_is_mergeable = _pr_is_mergeable
    pr_is_approved = _pr_is_approved
    pr_has_changes_requested = _pr_has_changes_requested
    pr_combined_check_state = _pr_combined_check_state

    def merge_pr(
        self,
        pr: FakePR,
        *,
        sha: str,
        method: str = "squash",
    ) -> bool:
        self.merge_calls.append((pr.number, sha, method))
        if not self.merge_returns_ok:
            return False
        pr.merged = True
        pr.state = _STATE_CLOSED
        return True

    def delete_remote_branch(self, branch: str) -> bool:
        self.deleted_remote_branches.append(branch)
        return self.delete_remote_branch_returns_ok


class _PullFeedbackService:
    def pr_conversation_comments_after(
        self,
        pr: FakePR,
        after_id: Optional[int],
    ) -> list[FakeComment]:
        comments = _comments_after(pr.issue_comments, after_id)
        comments.sort(key=lambda listed_comment: listed_comment.id)
        return comments

    def pr_inline_comments_after(
        self,
        pr: FakePR,
        after_id: Optional[int],
    ) -> list[FakeComment]:
        comments = _comments_after(pr.review_comments, after_id)
        comments.sort(key=lambda listed_comment: listed_comment.id)
        return comments

    def pr_reviews_after(
        self,
        pr: FakePR,
        after_id: Optional[int],
    ) -> list[FakePRReview]:
        return sorted(
            (
                review
                for review in pr.reviews
                if _review_has_feedback(review)
                and (after_id is None or review.id > after_id)
            ),
            key=lambda review: review.id,
        )

    def _for_worker_thread(self):
        return self


def _comments_after(
    comments: Iterable[FakeComment],
    after_id: Optional[int],
) -> list[FakeComment]:
    return [
        comment
        for comment in comments
        if PINNED_STATE_MARKER not in (comment.body or "")
        and (after_id is None or comment.id > after_id)
    ]
