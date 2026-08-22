# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pull-request lookup, labeling, status helpers, and merge-side mutations."""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from github import GithubException
from github.IssueComment import IssueComment
from github.PullRequest import PullRequest

from orchestrator.github.aliases import StaticMethodAlias
from orchestrator.github.comments import carries_own_marker
from orchestrator.github.pinned_state import GitHubStateMixin

log = logging.getLogger("orchestrator.github")

_ISSUE_STATE_OPEN = "open"
_ISSUE_STATE_CLOSED = "closed"
# What a lookup asks for when a PR that is no longer open is still the answer:
# a publication that crashed before recording its number can have been merged
# (and its branch auto-deleted) by the time anything comes looking.
_ISSUE_STATE_ALL = "all"
_HTTP_NOT_FOUND = 404


class _UnreadablePrLookup:
    """What the commit-pinned lookup answers when GitHub could not be asked.

    Neither a pull request nor the absence of one, because what a caller does
    with those two differs and only one of them is safe to do on a guess. A
    recovery told "no pull request carries this commit" pushes the branch back
    into existence and asks for a second pull request -- which, after the
    original was amended and squash-merged and its head branch auto-deleted,
    means recreating a branch GitHub removed on purpose and opening a PR that
    reverts the amendment. Told instead that the question could not be put, it
    holds and asks again on the next tick, having changed nothing.
    """

    def __repr__(self) -> str:
        return "PR_LOOKUP_UNREADABLE"


# The one answer `find_pr_for_commit` gives that is not about pull requests at
# all: the commit list of at least one candidate could not be read, so nothing
# here can say whether this commit is already published.
PR_LOOKUP_UNREADABLE = _UnreadablePrLookup()


def pr_has_label(pr: PullRequest, label_name: str) -> bool:
    """Return whether a pull request has a case-insensitive label name."""
    wanted_label = (label_name or "").lower()
    return any(
        ((getattr(label, "name", "") or "").lower() == wanted_label)
        for label in (pr.labels or [])
    )


def _pr_carries_commit(pr: PullRequest, head_sha: str) -> Optional[bool]:
    """Whether `head_sha` is one of the commits this pull request is made of.

    The head is checked first because it is free and answers the ordinary case:
    a publication that opened a PR and died before recording its number left
    that PR sitting on exactly the commit it pushed.

    It is not the whole question, though. A human can push onto that branch, or
    merge the base into it to make it mergeable, in the same window -- and each
    moves the head while the commit this publication put there stays in the
    pull request. Asked of the head alone, the recovery finds nothing and goes
    on to push the older SHA over their work or to open a second pull request
    for a design that already has one. The commit list is what the PR actually
    carries, and it answers the same for an open PR and for one a merge has
    since closed.

    A read that failed is its own answer rather than a no. The commit list is
    the only place a squash-merged, since-amended publication is still visible
    -- its head moved and its branch is gone -- so a transient failure read as
    "this PR does not carry it" is precisely the reading that makes a recovery
    republish over work that already landed.
    """
    if getattr(pr.head, "sha", None) == head_sha:
        return True
    try:
        return any(commit.sha == head_sha for commit in pr.get_commits())
    except GithubException as error:
        log.warning(
            "could not read the commits of PR #%s (HTTP %s): %s",
            pr.number, error.status, error.data,
        )
        return None


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
    """Pull-request lookup, labeling, and merge-side mutation methods."""

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

    def edit_pr_body(self, pr: PullRequest, body: str) -> None:
        """Rewrite one pull request's body.

        For a caller that adopts a PR it did not necessarily open -- the
        discussion stage reuses whatever is open on the branch it published,
        and implementing reuses that same plan PR once the dev's commits are
        on the branch -- and so has to make the description say what the
        branch now carries.
        """
        pr.edit(body=body)

    def pr_comment(self, pr_number: int, body: str) -> IssueComment:
        """Post one pull-request conversation comment."""
        return self.repo.get_pull(pr_number).create_issue_comment(body)

    def supersede_pr(
        self, pr: PullRequest, *, notice: str, marker: str,
    ) -> bool:
        """Say once on a pull request that it is superseded, and close it.

        Two effects with one guard, because they are one obligation: a change
        nobody is going to merge has to say so where the humans looking at it
        will read it, and then stop being open. Splitting them would let a
        pull request end up closed with nothing on it saying why, which is the
        state the notice exists to prevent.

        Idempotent by asking the thread rather than by remembering. The comment
        and whatever durable record the caller keeps of it cannot be made one
        operation, so a crash between them is a repeat waiting to happen: the
        thread is searched for `marker` and the notice is posted only when it
        is not already there. Two things make that search safe. The caller
        scopes the marker to the one episode it belongs to, so a reused pull
        request cannot read an earlier episode's receipt as this one's; and
        the comment has to be OURS, since an HTML comment is invisible in the
        rendered thread and anybody could otherwise post the marker to
        suppress the one notice saying this change is not to be merged. The
        close needs no such check; a pull request that is not open is left
        exactly as it is, which also keeps a merged one from being reopened
        and re-closed.

        False is every way this did not finish, and the caller retries the
        whole thing: the notice is idempotent and the close is a no-op on the
        second pass, so a retry costs a read. Every exception is caught rather
        than only GitHub's, because a lazy pull request raises from the first
        attribute read as readily as from the write, and a supersession that
        could not be made must hand the tick back rather than end it.
        """
        try:
            self._supersede(pr, notice, marker)
        except Exception:
            log.warning(
                "could not supersede PR #%s", getattr(pr, "number", "?"),
                exc_info=True,
            )
            return False
        return True

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

    def find_pr_for_commit(
        self,
        *,
        branch: str,
        base: str,
        head_sha: str,
    ) -> PullRequest | _UnreadablePrLookup | None:
        """Return the PR for this head branch that carries `head_sha`, any state.

        The lookup a crash window needs and `find_open_pr` cannot serve. A tick
        that opened a pull request and died before persisting its number leaves
        nothing pinned pointing at it, and the next tick re-derives the same
        commit -- but by then a human may have merged that PR, which closes it
        and (with the repository's auto-delete on) takes its head branch with
        it. Searched by open state alone, the recovery finds nothing, pushes
        the branch back into existence, and asks GitHub to open a second pull
        request for a commit that is already in the base.

        The commit is what makes the answer safe to act on. A branch name is
        reused across a whole issue lifetime and can carry several pull
        requests over it, so state is widened only because the commit narrows
        it back: what comes back is the pull request this exact publication
        landed on, or nothing.

        CARRIES the commit, not "is currently on it". The window this exists
        for is the one between opening a pull request and recording its number,
        and a human pushing to that branch -- or merging the base into it --
        moves the head inside that window while the published commit stays in
        the pull request. Matched on the head alone, the recovery would miss
        its own PR and either push the older commit over their work or ask for
        a second pull request for a design that already has one.

        `PR_LOOKUP_UNREADABLE` when a candidate's commit list could not be
        read, since "no pull request carries this" and "nobody could say" are
        different answers to the caller: the first one publishes. A candidate
        that DOES carry it still wins -- a definite answer needs no other
        pull request to have been readable -- so the unreadable one only
        decides the case where nothing else matched.

        The enumeration itself is the same question one level up, and it fails
        the same way: `get_pulls` is a request, and the pages it is walked
        through are a request each, so a candidate that would have matched can
        be one this call never reaches. Nothing distinguishes that from a
        branch with no pull requests on it except that nobody asked, so it is
        the same answer -- and it has to be, because the caller's `None` is
        what pushes. A page that raises halfway also discards whatever the
        pages before it said: a match found there is returned as it is
        reached, so what a failure loses is only the question, never an answer.
        """
        try:
            return self._scan_prs_for_commit(branch, base, head_sha)
        except GithubException as error:
            log.warning(
                "could not list the pull requests on %s (HTTP %s): %s",
                branch, error.status, error.data,
            )
            return PR_LOOKUP_UNREADABLE

    def iter_open_prs(self) -> Iterable[PullRequest]:
        """Yield every open pull request regardless of head branch."""
        yield from self.repo.get_pulls(state=_ISSUE_STATE_OPEN)

    def add_pr_label(self, pr: PullRequest, label_name: str) -> None:
        """Add one pull-request label idempotently at the GitHub layer."""
        pr.add_to_labels(label_name)

    def get_pr(self, pr_number: int) -> PullRequest:
        """Return one pull request by repository number."""
        return self.repo.get_pull(pr_number)

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

    def _supersede(self, pr: PullRequest, notice: str, marker: str) -> None:
        """Post the notice this thread does not carry, then close it."""
        if not self._pr_carries_marker(pr, marker):
            pr.create_issue_comment(notice)
        if pr_state(pr) == _ISSUE_STATE_OPEN:
            pr.edit(state=_ISSUE_STATE_CLOSED)

    def _pr_carries_marker(self, pr: PullRequest, marker: str) -> bool:
        """Whether a comment of OURS on this pull request carries `marker`."""
        return carries_own_marker(
            pr.get_issue_comments(),
            marker,
            bot_login=getattr(self, "_bot_login", None),
        )

    def _scan_prs_for_commit(self, branch: str, base: str, head_sha: str):
        """Walk this branch's pull requests for one carrying `head_sha`."""
        owner_login = self.repo.owner.login
        head = f"{owner_login}:{branch}"
        unreadable = False
        for pull_request in self.repo.get_pulls(
            state=_ISSUE_STATE_ALL, head=head, base=base,
        ):
            carries = _pr_carries_commit(pull_request, head_sha)
            if carries:
                return pull_request
            unreadable = unreadable or carries is None
        return PR_LOOKUP_UNREADABLE if unreadable else None
