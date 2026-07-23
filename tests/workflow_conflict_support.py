# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed resolving-conflict scenarios for focused workflow tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

from orchestrator import workflow

from tests.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_other_labels import LABEL_RESOLVING_CONFLICT
from tests.workflow_patch_models import _agent
from tests.workflow_patch_runner import _PatchedWorkflowMixin
from tests.workflow_repo_values import BACKEND_CLAUDE, STATE_OPEN
from tests.workflow_value_helpers import _issue_branch


_CONFLICT_ISSUE_NUMBER = 200
_CONFLICT_BRANCH = _issue_branch(_CONFLICT_ISSUE_NUMBER)
_CONFLICT_PR_NUMBER = 800
_CONFLICT_PR_HEAD_SHA = "cafe1234"


@dataclass(frozen=True)
class _ConflictSeedContext:
    merge_succeeded: bool = True
    conflicted_files: tuple = ()
    head_shas: tuple = ("before", "after")
    push_branch: bool = True
    run_agent_result: Any = None
    pr_state: str = STATE_OPEN
    pr_merged: bool = False
    extra_state: Any = None


@dataclass(frozen=True)
class _ConflictRunContext:
    merge_succeeded: bool = True
    conflicted_files: tuple = ()
    head_shas: tuple = ("before", "after")
    push_branch: bool = True
    run_agent_result: Any = None
    fetch_returncode: int = 0
    dirty_files: tuple = ()
    rebase_in_progress: bool = False


@dataclass(frozen=True)
class _ConflictMocks:
    merge: MagicMock
    git: MagicMock
    git_hardened: MagicMock


def _seed_conflict(owner, context: _ConflictSeedContext):
    github = FakeGitHubClient()
    issue = make_issue(
        owner.issue_number,
        label=LABEL_RESOLVING_CONFLICT,
    )
    github.add_issue(issue)
    pull_request = FakePR(
        number=owner.pr_number,
        head_branch=owner.issue_branch,
        head=FakePRRef(sha=owner.pr_head_sha),
        mergeable=False,
        check_state="success",
        merged=context.pr_merged,
        state=context.pr_state,
    )
    github.add_pr(pull_request)
    state = {
        "pr_number": owner.pr_number,
        "branch": owner.issue_branch,
        "dev_agent": BACKEND_CLAUDE,
        "dev_session_id": "dev-sess",
        "review_round": 2,
        "conflict_round": 0,
    }
    if context.extra_state:
        state.update(context.extra_state)
    github.seed_state(owner.issue_number, **state)
    return github, issue, pull_request


def _build_conflict_mocks(context: _ConflictRunContext) -> _ConflictMocks:
    fetch_result = MagicMock(
        returncode=context.fetch_returncode,
        stdout="",
        stderr="",
    )
    return _ConflictMocks(
        merge=MagicMock(return_value=(
            context.merge_succeeded,
            list(context.conflicted_files),
        )),
        git=MagicMock(return_value=fetch_result),
        git_hardened=MagicMock(return_value=fetch_result),
    )


def _run_conflict_merge(owner, github, issue, context):
    agent_result = context.run_agent_result or _agent(
        session_id="dev-sess",
        last_message="resolved",
    )
    mocks = _build_conflict_mocks(context)
    with patch.object(
        workflow,
        "_rebase_base_into_worktree",
        mocks.merge,
    ), patch.object(
        workflow,
        "_git",
        mocks.git,
    ), patch.object(
        workflow,
        "_git_hardened",
        mocks.git_hardened,
    ):
        workflow_mocks = owner._run_resolving_conflict(
            github,
            issue,
            run_agent=agent_result,
            push_branch=context.push_branch,
            head_shas=context.head_shas,
            dirty_files=context.dirty_files,
            rebase_in_progress=context.rebase_in_progress,
        )
    return workflow_mocks, mocks.merge, mocks.git


class _ResolvingConflictMixin(_PatchedWorkflowMixin):
    """Seed and run resolving-conflict scenarios without shelling out."""

    issue_number = _CONFLICT_ISSUE_NUMBER
    issue_branch = _CONFLICT_BRANCH
    pr_number = _CONFLICT_PR_NUMBER
    pr_head_sha = _CONFLICT_PR_HEAD_SHA

    def _seed(self, **seed_options):
        return _seed_conflict(
            self,
            _ConflictSeedContext(**seed_options),
        )

    def _run_with_merge(self, github, issue, **run_options):
        return _run_conflict_merge(
            self,
            github,
            issue,
            _ConflictRunContext(**run_options),
        )

    def _seed_with_baseline_hash(self, github, issue, **extra):
        state_data = github.pinned_data(self.issue_number)
        state_data.update(extra)
        state_data["user_content_hash"] = (
            workflow._compute_user_content_hash(issue, set())
        )
        github.seed_state(self.issue_number, **state_data)
