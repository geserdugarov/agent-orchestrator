# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pull-request lookup, labeling, and stateless status helpers."""
from __future__ import annotations

from typing import Iterable, Optional

from github import GithubException
from github.IssueComment import IssueComment
from github.PullRequest import PullRequest

from orchestrator._github_state import GitHubStateMixin
from orchestrator._static_alias import StaticMethodAlias

_ISSUE_STATE_OPEN = "open"


def pr_has_label(pr: PullRequest, label_name: str) -> bool:
    """Return whether a pull request has a case-insensitive label name."""
    wanted_label = (label_name or "").lower()
    return any(
        ((getattr(label, "name", "") or "").lower() == wanted_label)
        for label in (pr.labels or [])
    )


def pr_state(pr: PullRequest) -> str:
    """Return ``merged``, ``closed``, or ``open`` for a pull request."""
    if pr.merged:
        return "merged"
    if pr.state == "closed":
        return "closed"
    return _ISSUE_STATE_OPEN


def pr_is_mergeable(pr: PullRequest) -> Optional[bool]:
    """Refresh a lazily-computed mergeable field once when needed."""
    if pr.mergeable is None:
        try:
            pr.update()
        except GithubException:
            return None
    return pr.mergeable


PR_HAS_LABEL_METHOD = StaticMethodAlias(pr_has_label)
PR_STATE_METHOD = StaticMethodAlias(pr_state)
PR_IS_MERGEABLE_METHOD = StaticMethodAlias(pr_is_mergeable)


class GitHubPullRequestMixin(GitHubStateMixin):
    """Core pull-request lookup and labeling methods."""

    pr_has_label = PR_HAS_LABEL_METHOD
    pr_state = PR_STATE_METHOD
    pr_is_mergeable = PR_IS_MERGEABLE_METHOD

    def open_pr(
        self,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequest:
        """Open a pull request for a published issue branch."""
        return self.repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base=base,
        )

    def pr_comment(self, pr_number: int, body: str) -> IssueComment:
        """Post one pull-request conversation comment."""
        return self.repo.get_pull(pr_number).create_issue_comment(body)

    def find_open_pr(
        self,
        *,
        branch: str,
        base: str,
    ) -> Optional[PullRequest]:
        """Return an open PR for the repository-owned head branch."""
        owner_login = self.repo.owner.login
        head = f"{owner_login}:{branch}"
        return next(
            iter(self.repo.get_pulls(
                state=_ISSUE_STATE_OPEN,
                head=head,
                base=base,
            )),
            None,
        )

    def iter_open_prs(self) -> Iterable[PullRequest]:
        """Yield every open pull request regardless of head branch."""
        yield from self.repo.get_pulls(state=_ISSUE_STATE_OPEN)

    def add_pr_label(self, pr: PullRequest, label_name: str) -> None:
        """Add one pull-request label idempotently at the GitHub layer."""
        pr.add_to_labels(label_name)

    def get_pr(self, pr_number: int) -> PullRequest:
        """Return one pull request by repository number."""
        return self.repo.get_pull(pr_number)
