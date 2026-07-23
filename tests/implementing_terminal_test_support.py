# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and protocol values for implementing terminal tests."""

from __future__ import annotations

from unittest import mock

from orchestrator import workflow as _workflow
from tests import fakes, workflow_helpers

MagicMock = mock.MagicMock
workflow = _workflow
FakeGitHubClient = fakes.FakeGitHubClient
FakePR = fakes.FakePR
FakePRRef = fakes.FakePRRef
make_issue = fakes.make_issue
EVENT_PR_CLOSED_WITHOUT_MERGE = workflow_helpers.EVENT_PR_CLOSED_WITHOUT_MERGE
LABEL_IMPLEMENTING = workflow_helpers.LABEL_IMPLEMENTING
_PatchedWorkflowMixin = workflow_helpers._PatchedWorkflowMixin
_TEST_SPEC = workflow_helpers._TEST_SPEC
_agent = workflow_helpers._agent
_issue_branch = workflow_helpers._issue_branch

RUN_AGENT = "run_agent"
CLEANUP_TERMINAL_BRANCH = "_cleanup_terminal_branch"
LABEL_DONE = "done"
LABEL_REJECTED = "rejected"
CLOSED_WITHOUT_MERGE_AT = "closed_without_merge_at"
PR_HEAD_SHA = "cafe1234"
DEV_AGENT = "claude"
DEV_SESSION = "dev-sess"
EXTERNALLY_MERGED_ISSUE = 150
EXTERNALLY_MERGED_PR = 15000
NO_PR_ISSUE = 151
OPEN_PR_ISSUE = 152
OPEN_PR = 15200
CLOSED_PR_ISSUE = 153
CLOSED_PR = 15300
FETCH_FAILURE_ISSUE = 154
FETCH_FAILURE_PR = 15400
MERGED_DEFER_ISSUE = 155
MERGED_DEFER_PR = 15500
USAGE_ISSUE = 156
NO_USAGE_ISSUE = 157
USAGE_TOKEN_COUNT = 3400
USAGE_COST_USD = 0.31
