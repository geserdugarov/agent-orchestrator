# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Rebase contexts and a fake client shared by the base-sync owner tests."""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from unittest.mock import patch

from orchestrator import config
from orchestrator.git.base_sync import models
from orchestrator.workflow.state import WorkflowLabel
from tests.git.base_sync.refresh_test_support import GATE_CANDIDATE_SHA
from tests.support.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)

ISSUE = 7

PR_NUMBER = 42

BRANCH = "orchestrator/acme__widget/issue-7"

LABEL = "in_review"

VALIDATING = "workflow:validating"

PRE_REBASE_SHA = "be40e5ba" * 5

# The head a crash recovery finds the checkout standing on. It IS the commit
# the size gate proves that checkout to, because the two are one read of one
# worktree: the recovery names the commit it means to publish and the gate
# refuses a checkout standing anywhere else.
RECOVERED_SHA = GATE_CANDIDATE_SHA

REMOTE_SHA = "remote-sha"

WORKTREE = Path("/tmp/base-sync-owner-wt")

# The head the recovered pull request is standing on. It IS the pre-rebase
# sha, because that is what the recovery's own lease claims about that branch:
# an interrupted auto-rebase left the remote where it found it, and the two
# readings the gate compares -- the caller's lease and the pull request -- are
# two statements of that one fact.
RECOVERY_PR_HEAD_SHA = PRE_REBASE_SHA

# A pull request somebody else pushed to while the recovery was in flight.
MOVED_PR_HEAD_SHA = "0ec0de11" * 5

SPEC = config.RepoSpec(
    slug="acme/widget",
    target_root=Path("/tmp/base-sync-owner-target"),
    base_branch="main",
)

PARK_PUSH_FAILED = "auto_base_rebase_push_failed"

PARK_DIRTY = "auto_base_rebase_dirty"

KEY_AWAITING_HUMAN = "awaiting_human"

KEY_PARK_REASON = "park_reason"

KEY_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"

KEY_REVIEW_ROUND = "review_round"

KEY_LAST_ACTION_COMMENT_ID = "last_action_comment_id"

GIT_HARDENED = "_git_hardened"

GIT_FAILURE_EXIT_CODE = 128

BEHIND_BY = 3

HUMAN_COMMENT_BODY = "branch reconciled, please retry"


# What one interrupted attempt recorded about its own replay: the head it
# produced, and the publication it produced it for. Both are the fixture's
# ordinary world -- the head the recovery finds, on the pull request the issue
# records -- so a case says only what it moves.
RECORDED_REWRITE = models._PendingRewrite(
    sha=RECOVERED_SHA, pr_number=PR_NUMBER, stage=WorkflowLabel.IN_REVIEW,
)

# The same record one `git rebase` earlier: the terms an attempt pins beside
# its anchor before git may touch the branch, with no head yet naming what the
# replay produced.
IN_FLIGHT_REWRITE = models._PendingRewrite(
    pr_number=PR_NUMBER, stage=WorkflowLabel.IN_REVIEW,
)


def _git_result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Build a completed `git` result the owners read fields off."""
    return subprocess.CompletedProcess(
        args=["git"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _recovery_context(
    *,
    behind: int = 0,
    unparking_consumed_max: int | None = None,
    pending_rewrite: models._PendingRewrite = RECORDED_REWRITE,
    **state_fields,
) -> models._AutoRebaseRecoveryContext:
    """Seed issue #7 with PR #42 pinned and wrap it in a recovery context.

    `state_fields` merge into the pinned state, so a test names only the
    fields its case reads back.

    `pending_rewrite` defaults to the record a real attempt leaves -- the
    replay it produced, and the publication it produced it for -- because that
    is what every road past the anchor is decided against. A case about the
    window before that write, or about a record naming some other commit,
    hands in its own.
    """
    gh = FakeGitHubClient()
    issue = make_issue(ISSUE, label=LABEL)
    gh.add_issue(issue)
    gh.seed_state(
        ISSUE, pr_number=PR_NUMBER, branch=BRANCH, **state_fields,
    )
    # The pull request the recovered push joins. A number in pinned state
    # with nothing behind it is a state the size gate refuses before any
    # recovery may push, and the head it stands on is read at its exact
    # length.
    gh.add_pr(FakePR(
        number=PR_NUMBER,
        head_branch=BRANCH,
        head=FakePRRef(sha=RECOVERY_PR_HEAD_SHA),
    ))
    return models._AutoRebaseRecoveryContext(
        gh=gh,
        spec=SPEC,
        issue=issue,
        state=gh.read_pinned_state(issue),
        worktree=WORKTREE,
        pr_number=PR_NUMBER,
        label=LABEL,
        pending_pre_rebase_sha=PRE_REBASE_SHA,
        pending_rewrite=pending_rewrite,
        behind=behind,
        unparking_consumed_max=unparking_consumed_max,
    )


def _sync_context(
    *,
    label: str = LABEL,
    behind: int = BEHIND_BY,
    pending_pre_rebase_sha: str | None = None,
    comments: tuple = (),
    **state_fields,
) -> models._AutoRebaseContext:
    """Seed issue #7 with PR #42 pinned and wrap it in a rebase context.

    `comments` are `(id, login)` pairs appended to the issue thread, so a
    park-release case names the reply it expects to be recognized; the PR
    itself is registered by the caller that needs one readable.
    """
    gh = FakeGitHubClient()
    issue = make_issue(ISSUE, label=label)
    for comment_id, login in comments:
        issue.comments.append(
            FakeComment(
                id=comment_id,
                body=HUMAN_COMMENT_BODY,
                user=FakeUser(login),
            ),
        )
    gh.add_issue(issue)
    gh.seed_state(
        ISSUE, pr_number=PR_NUMBER, branch=BRANCH, **state_fields,
    )
    return models._AutoRebaseContext(
        gh=gh,
        spec=SPEC,
        issue=issue,
        state=gh.read_pinned_state(issue),
        worktree=WORKTREE,
        pr_number=PR_NUMBER,
        behind=behind,
        label=label,
        pending_pre_rebase_sha=pending_pre_rebase_sha,
    )


def _add_pr(
    gh: FakeGitHubClient,
    *,
    merged: bool = False,
    pr_state: str = "open",
) -> FakePR:
    """Register PR #42 on the pinned head branch at the given state."""
    pr = FakePR(
        number=PR_NUMBER,
        head_branch=BRANCH,
        merged=merged,
        state=pr_state,
    )
    gh.add_pr(pr)
    return pr


def _snapshot(
    *,
    local_head: str = RECOVERED_SHA,
    remote_head: str = REMOTE_SHA,
    ahead: int = 0,
    behind: int = 0,
) -> models._AutoRebaseRecoverySnapshot:
    """Build the local/remote comparison an outcome is selected from."""
    return models._AutoRebaseRecoverySnapshot(
        branch=BRANCH,
        local_head=local_head,
        remote_head=remote_head,
        ahead=ahead,
        behind=behind,
    )


class _OrderedCall:
    """Record one call on a shared log, then run the real one."""

    def __init__(self, ordered: list[str], name: str, original) -> None:
        self._ordered = ordered
        self._name = name
        self._original = original

    def __call__(self, *args, **kwargs):
        self._ordered.append(self._label(args))
        return self._original(*args, **kwargs)

    def _label(self, args) -> str:
        # `emit_event` and `_git_hardened` each land more than once in a
        # single sequence and take their subject first, so that argument is
        # what tells the entries apart. Every other recorded call leads with
        # an issue or a PR number instead.
        if args and isinstance(args[0], str):
            return f"{self._name}:{args[0]}"
        return self._name


@contextlib.contextmanager
def _recorded_calls(ordered: list[str], gh, *names: str):
    """Log the order `gh` receives `names`, still running each real call."""
    with contextlib.ExitStack() as stack:
        for name in names:
            stack.enter_context(
                patch.object(
                    gh, name, _OrderedCall(ordered, name, getattr(gh, name)),
                ),
            )
        yield
