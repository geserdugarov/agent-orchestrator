# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and protocol values for full-agent-spec tests."""

from __future__ import annotations

from unittest import mock

from orchestrator import config as config
from orchestrator import workflow as _workflow
from tests import fakes, implementing_fixing_test_cases, workflow_helpers

MagicMock = mock.MagicMock
patch = mock.patch
workflow = _workflow

FakeComment = fakes.FakeComment
FakeGitHubClient = fakes.FakeGitHubClient
FakeUser = fakes.FakeUser
make_issue = fakes.make_issue
IssueScenario = implementing_fixing_test_cases.IssueScenario

BACKEND_CLAUDE = workflow_helpers.BACKEND_CLAUDE
BACKEND_CODEX = workflow_helpers.BACKEND_CODEX
KEY_AWAITING_HUMAN = workflow_helpers.KEY_AWAITING_HUMAN
LABEL_DECOMPOSING = workflow_helpers.LABEL_DECOMPOSING
LABEL_IMPLEMENTING = workflow_helpers.LABEL_IMPLEMENTING
LABEL_VALIDATING = workflow_helpers.LABEL_VALIDATING
REVIEW_APPROVED_MESSAGE = workflow_helpers.REVIEW_APPROVED_MESSAGE
REVIEW_CHANGES_REQUESTED_MESSAGE = workflow_helpers.REVIEW_CHANGES_REQUESTED_MESSAGE
_FAKE_WT = workflow_helpers._FAKE_WT
_PatchedWorkflowMixin = workflow_helpers._PatchedWorkflowMixin
_TEST_SPEC = workflow_helpers._TEST_SPEC
_agent = workflow_helpers._agent
_issue_branch = workflow_helpers._issue_branch

CODEX_SPEC = "codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"
CODEX_ARGS = (
    "-m",
    "gpt-5.5",
    "-c",
    'model_reasoning_effort="xhigh"',
)
CLAUDE_SPEC = "claude --model claude-opus-4-7"
CLAUDE_ARGS = ("--model", "claude-opus-4-7")

RUN_AGENT = "run_agent"
EXTRA_ARGS = "extra_args"
RESUME_SESSION_ID = "resume_session_id"
DEV_AGENT_KEY = "dev_agent"
DEV_SESSION_ID = "dev_session_id"
DECOMPOSER_AGENT = "decomposer_agent"
OK_MESSAGE = "ok"
TEST_AUTHOR = "alice"
UNCHANGED_SHA = "aaa"

FRESH_DEV_ISSUE = 67001
RESUMED_DEV_ISSUE = 67002
LEGACY_BACKEND_ISSUE = 67003
LEGACY_SESSION_ISSUE = 67004
POISONED_DROP_ISSUE = 67005
POISONED_LEGACY_ISSUE = 67006
FRESH_REVIEW_ISSUE = 67010
REVIEW_COMMENT_ISSUE = 67011
CHANGE_REQUEST_ISSUE = 67012
REVIEW_PROMPT_ISSUE = 67013
FRESH_DECOMPOSER_ISSUE = 67020
RESUMED_DECOMPOSER_ISSUE = 67021
NO_SESSION_DEV_ISSUE = 67030
ENV_FLIP_DEV_ISSUE = 67031
NO_SESSION_DECOMPOSER_ISSUE = 67032
ENV_FLIP_DECOMPOSER_ISSUE = 67033

LEGACY_REPLY_ID = 2100
LEGACY_ACTION_WATERMARK = 2000
CODEX_SESSION_REPLY_ID = 2200
DECOMPOSER_PARK_ID = 3000
DECOMPOSER_REPLY_ID = 3010
DEV_ENV_FLIP_REPLY_ID = 4000
DECOMPOSER_ENV_FLIP_REPLY_ID = 4100


class _FullSpecFixtureMixin(_PatchedWorkflowMixin):
    """Patch configured agent specifications for persistence scenarios."""

    def _patch_dev_config(
        self,
        spec: str,
        backend: str,
        args: tuple[str, ...],
    ):
        return [
            patch.object(config, "DEV_AGENT_SPEC", spec),
            patch.object(config, "DEV_AGENT", backend),
            patch.object(config, "DEV_AGENT_ARGS", args),
        ]

    def _patch_review_config(
        self,
        spec: str,
        backend: str,
        args: tuple[str, ...],
    ):
        return [
            patch.object(config, "REVIEW_AGENT_SPEC", spec),
            patch.object(config, "REVIEW_AGENT", backend),
            patch.object(config, "REVIEW_AGENT_ARGS", args),
        ]

    def _patch_decompose_config(
        self,
        spec: str,
        backend: str,
        args: tuple[str, ...],
    ):
        return [
            patch.object(config, "DECOMPOSE_AGENT_SPEC", spec),
            patch.object(config, "DECOMPOSE_AGENT", backend),
            patch.object(config, "DECOMPOSE_AGENT_ARGS", args),
        ]

    def _enter(self, patches):
        for config_patch in patches:
            config_patch.start()
            self.addCleanup(config_patch.stop)
