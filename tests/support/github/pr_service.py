# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pull-request services for the in-memory GitHub client."""
from __future__ import annotations

from typing import Iterable, Optional

from orchestrator.github import pull_requests as _pull_requests
from orchestrator.github.pinned_state import PINNED_STATE_MARKER

from tests.support.github.pr_helpers import (
    _pr_combined_check_state,
    _pr_has_changes_requested,
    _pr_has_label,
    _pr_is_approved,
    _pr_is_mergeable,
    _pr_state,
)
from tests.support.github.model_helpers import _review_has_feedback
from tests.support.github.models import (
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

    def find_pr_for_commit(
        self, *, branch: str, base: str, head_sha: str,
    ) -> Optional[FakePR]:
        """The PR on this branch carrying `head_sha`, whatever state it is in.

        Searched over every PR the fake holds rather than over the open-PR
        table beside it, since the whole point of the real lookup is the one a
        merge has already closed -- and by the commits it carries as well as by
        its head, since a human pushing to the branch moves one and not the
        other.

        `unreadable_pr_commits` names the PRs whose commit list GitHub refuses,
        which the real client answers with `PR_LOOKUP_UNREADABLE` rather than
        with "does not carry it": the head is still free to match, and only a
        candidate that had to be asked about its commits and could not be
        leaves the whole lookup unanswered. `unreadable_pr_lookups` is the same
        answer one level up -- the enumeration itself failing, which reaches no
        candidate at all.
        """
        if branch in self.unreadable_pr_lookups:
            return _pull_requests.PR_LOOKUP_UNREADABLE
        unreadable = False
        for pull_request in self.pulls.values():
            carries = self._pr_carries(pull_request, branch, head_sha)
            if carries:
                return pull_request
            unreadable = unreadable or carries is None
        if unreadable:
            return _pull_requests.PR_LOOKUP_UNREADABLE
        return None

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

    def supersede_pr(self, pr: FakePR, *, notice: str, marker: str) -> bool:
        """Post one marked supersession notice and close an open PR.

        Idempotent through the thread, exactly as the real helper is: the
        marker is looked for among the pull request's own conversation
        comments, so a second call after a crash adds nothing and closes
        nothing that is already settled.
        """
        if pr.number in self.unsupersedable_prs:
            return False
        carried = any(
            marker in (pr_comment.body or "")
            for pr_comment in pr.issue_comments
        )
        if not carried:
            self.pr_comment(pr.number, notice)
        if self.pr_state(pr) == _STATE_OPEN:
            pr.state = _STATE_CLOSED
        return True

    def edit_pr_body(self, pr: FakePR, body: str) -> None:
        self.edited_pr_bodies.append((pr.number, body))
        pr.body = body

    def delete_remote_branch(self, branch: str) -> bool:
        self.deleted_remote_branches.append(branch)
        return self.delete_remote_branch_returns_ok

    def _pr_carries(
        self, pr: FakePR, branch: str, head_sha: str,
    ) -> Optional[bool]:
        """Whether one PR carries the commit, or None when it cannot be read."""
        if pr.head_branch != branch:
            return False
        if pr.head.sha == head_sha:
            return True
        if pr.number in self.unreadable_pr_commits:
            return None
        return head_sha in pr.commit_shas


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
