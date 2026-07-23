# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and protocol values for implementing PR tests."""

from __future__ import annotations

import pathlib
from unittest import mock

from orchestrator import branch_publication as branch_publication
from orchestrator import config as _config
from orchestrator import workflow as workflow
from orchestrator import worktree_lifecycle as _worktree_lifecycle
from orchestrator.stages import implementing as _implementing
from tests import fakes, implementing_fixing_test_cases, workflow_helpers

Path = pathlib.Path
MagicMock = mock.MagicMock
config = _config
implementing = _implementing
patch = mock.patch
worktree_lifecycle = _worktree_lifecycle

FakeComment = fakes.FakeComment
FakeGitHubClient = fakes.FakeGitHubClient
FakeLabel = fakes.FakeLabel
FakePR = fakes.FakePR
FakeUser = fakes.FakeUser
make_issue = fakes.make_issue
IssueScenario = implementing_fixing_test_cases.IssueScenario
posted_comment_contains = implementing_fixing_test_cases.posted_comment_contains

LABEL_IMPLEMENTING = workflow_helpers.LABEL_IMPLEMENTING
_PatchedWorkflowMixin = workflow_helpers._PatchedWorkflowMixin
_TEST_SPEC = workflow_helpers._TEST_SPEC
_agent = workflow_helpers._agent

GIT_HELPER = "_git"
FAKE_WORKTREE = Path("/tmp/wt-not-real")
TEST_TARGET_ROOT = Path("/tmp/orchestrator-test-target-root")
DEFAULT_REVISION_RANGE = "origin/main..HEAD"
DEV_SESSION = "sess-1"
DONE_MESSAGE = "done"
FEATURE_PREFIX = "feat"
TEST_ISSUE_TITLE = "add a thing"
TEST_ISSUE_BODY = "please add a thing"
SPARKLY_TITLE = "add a sparkly thing"
SPARKLY_COMMIT_SUBJECT = "feat: add a sparkly thing"
REPO_LOCAL_FORBIDDEN_PREFIXES = ("feat:", "chore:", "refactor:", "test:")
FOREGROUND_MARKER = "NEVER start a background job"
GITHUB_BODY_LIMIT = 65536
EXISTING_PR_NUMBER = 42
FEEDBACK_COMMENT_ID = 42
BRANCHLESS_ISSUE = 11
BRANCHLESS_REPLY_ID = 2100
BRANCHLESS_WATERMARK = 2000
LONG_MESSAGE_WORD_COUNT = 20000
CODE_FENCE_LINE_COUNT = 20000
TOKEN_TAIL_LENGTH = 4000
LONG_BODY_REPEAT_COUNT = 5000
BODY_SHORT_ISSUE = 61
CONVENTIONAL_ISSUE = 30
SCOPED_CONVENTIONAL_ISSUE = 31
UNCONVENTIONAL_ISSUE = 32
BUG_FALLBACK_ISSUE = 33
EMPTY_SUBJECT_ISSUE = 34
CONVENTIONAL_TITLE_ISSUE = 35
CUSTOM_PREFIX_ISSUE = 36
INFERRED_PREFIX_ISSUE = 37
PREFIX_HELPER_ISSUE = 50
GIT_ERROR_ISSUE = 51
REMOTE_ROUTING_ISSUE = 52


class _GitRecorder:
    def __init__(self, stdout: str = "", *, returncode: int = 0, stderr: str = ""):
        self.calls: list[tuple] = []
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, *args, cwd):
        self.calls.append((args, cwd))
        return MagicMock(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class _RepoLocalStyleAssertions:
    def _assert_repo_local_style(self, prompt: str) -> None:
        self.assertIn("git log", prompt)
        self.assertIn("repository-local", prompt)
        self.assertIn("event:", prompt)
        self.assertIn("career:", prompt)
        self.assertNotIn("Conventional", prompt)
        for prefix in REPO_LOCAL_FORBIDDEN_PREFIXES:
            self.assertNotIn(prefix, prompt)
        self.assertIn("subject line only", prompt)
        self.assertIn("Co-Authored-By", prompt)


class _ConventionalTitleFixtureMixin(_PatchedWorkflowMixin):
    def _seeded(self, *, issue_number: int = 30, label_name: str = "") -> tuple:
        gh = FakeGitHubClient()
        issue = make_issue(
            issue_number,
            label=LABEL_IMPLEMENTING,
            title=SPARKLY_TITLE,
        )
        if label_name:
            issue.labels.append(FakeLabel(label_name))
        gh.add_issue(issue)
        return gh, issue


class _SubjectPrefixFixtureMixin:
    def _infer(self, stdout: str, *, bug: bool = False) -> str:
        issue = make_issue(PREFIX_HELPER_ISSUE, title="do a thing")
        if bug:
            issue.labels.append(FakeLabel("bug"))
        git = _GitRecorder(stdout)
        with patch.object(branch_publication, GIT_HELPER, git):
            return workflow._infer_subject_prefix(
                _TEST_SPEC,
                FAKE_WORKTREE,
                issue,
            )
