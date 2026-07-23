# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for tracked-repository prompt tests."""
from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import config, workflow

from tests import fakes as _fakes
from tests import workflow_helpers as _helpers


REVIEW_APPROVED_MESSAGE = _helpers.REVIEW_APPROVED_MESSAGE
_PatchedWorkflowMixin = _helpers._PatchedWorkflowMixin
_TEST_SPEC = _helpers._TEST_SPEC
_agent = _helpers._agent
_fake_worktree = _helpers._fake_worktree

FakeComment = _fakes.FakeComment
FakeGitHubClient = _fakes.FakeGitHubClient
FakeUser = _fakes.FakeUser
make_issue = _fakes.make_issue

_BLOCK_MARKER = "This orchestrator also tracks the repositories below"
_OTHER_REPO_SLUG = "acme/sibling"
_EXPOSE_REPOS_ATTR = "EXPOSE_TRACKED_REPOS"
_DEFAULT_SPECS_ATTR = "default_repo_specs"
_RUN_AGENT_ATTR = "run_agent"
_DEV_SESSION_ID = "dev-sess"
_BEFORE_SHA = "aaa"
_AFTER_SHA = "bbb"
_QUESTION_LABEL = "question"
_IMPLEMENTER_ISSUE_NUMBER = 701
_DOCUMENTATION_ISSUE_NUMBER = 702
_DOCUMENTATION_PR_NUMBER = 72
_RESUME_ISSUE_NUMBER = 703
_DECOMPOSER_ISSUE_NUMBER = 710
_REVIEW_ISSUE_NUMBER = 711
_REVIEW_PR_NUMBER = 11
_FRESH_QUESTION_ISSUE_NUMBER = 712
_RECOVERY_QUESTION_ISSUE_NUMBER = 713
_RECOVERY_COMMENT_ID = 42000
_RECOVERY_WATERMARK = 41000
_RESUMED_QUESTION_ISSUE_NUMBER = 714
_RESUME_COMMENT_ID = 52000
_RESUME_WATERMARK = 51000
_DOCUMENTATION_WATERMARK = 6000
_DOCUMENTATION_REPLY_ID = 6100

_OTHER_SPEC = config.RepoSpec(
    slug=_OTHER_REPO_SLUG,
    target_root=Path("/srv/sibling-checkout"),
    base_branch="develop",
)
_MULTI_SPECS = (_TEST_SPEC, _OTHER_SPEC)
_DECOMPOSITION_MANIFEST = (
    "fits one context\n\n"
    "```orchestrator-manifest\n"
    '{"decision": "single", "rationale": "small"}\n'
    "```\n"
)


@contextlib.contextmanager
def _multi_repo():
    with patch.object(config, _EXPOSE_REPOS_ATTR, True), patch.object(
        config,
        _DEFAULT_SPECS_ATTR,
        lambda: list(_MULTI_SPECS),
    ):
        yield


def _prompt_of(run_agent_mock) -> str:
    call = run_agent_mock.call_args
    return call.kwargs.get("prompt") or call.args[1]


def _implementer_prompt(case) -> str:
    github = FakeGitHubClient()
    issue = make_issue(_IMPLEMENTER_ISSUE_NUMBER, label="implementing")
    github.add_issue(issue)
    mocks = case._run(
        lambda: workflow._handle_implementing(github, _TEST_SPEC, issue),
        run_agent=_agent(session_id="sess-1", last_message="done"),
        has_new_commits=[False, True],
        push_branch=True,
    )
    return _prompt_of(mocks[_RUN_AGENT_ATTR])


def _documentation_seed(**state):
    github = FakeGitHubClient()
    issue = make_issue(_DOCUMENTATION_ISSUE_NUMBER, label="documenting")
    github.add_issue(issue)
    defaults = dict(
        pr_number=_DOCUMENTATION_PR_NUMBER,
        branch="orchestrator/geserdugarov__agent-orchestrator/issue-702",
        dev_agent="codex",
        dev_session_id=_DEV_SESSION_ID,
    )
    defaults.update(state)
    github.seed_state(_DOCUMENTATION_ISSUE_NUMBER, **defaults)
    return github, issue


def _resume_seed(*, resume_count: int):
    github = FakeGitHubClient()
    issue = make_issue(
        _RESUME_ISSUE_NUMBER,
        label="in_review",
        body="implement the thing",
    )
    github.add_issue(issue)
    github.seed_state(
        _RESUME_ISSUE_NUMBER,
        dev_agent="claude",
        dev_session_id="live-sess",
        silent_park_count=0,
        dev_resume_count=resume_count,
    )
    return github, issue


def _resume_prompt(github, issue, *, threshold: int) -> str:
    run_mock = MagicMock(
        return_value=_agent(session_id="fresh-sess", last_message="ok"),
    )
    state = github.read_pinned_state(issue)
    with (
        _multi_repo(),
        patch.object(config, "DEV_SESSION_MAX_RESUMES", threshold),
        patch.object(workflow, "_ensure_worktree", _fake_worktree),
        patch.object(workflow, _RUN_AGENT_ATTR, run_mock),
    ):
        workflow._resume_dev_with_text(
            github,
            _TEST_SPEC,
            issue,
            state,
            "fix it",
        )
    return _prompt_of(run_mock)


def _decomposer_prompt(case) -> str:
    github = FakeGitHubClient()
    issue = make_issue(_DECOMPOSER_ISSUE_NUMBER, label="decomposing")
    github.add_issue(issue)
    mocks = case._run(
        lambda: workflow._handle_decomposing(github, _TEST_SPEC, issue),
        run_agent=_agent(
            session_id="dec-1",
            last_message=_DECOMPOSITION_MANIFEST,
        ),
    )
    return _prompt_of(mocks[_RUN_AGENT_ATTR])
