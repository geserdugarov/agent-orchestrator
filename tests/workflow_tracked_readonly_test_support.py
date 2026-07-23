# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Reviewer fixtures for tracked-repository prompt tests."""
from __future__ import annotations

from tests import workflow_tracked_repos_test_support as support


def _review_seed():
    github = support.FakeGitHubClient()
    issue = support.make_issue(
        support._REVIEW_ISSUE_NUMBER,
        label="validating",
    )
    github.add_issue(issue)
    github.seed_state(
        support._REVIEW_ISSUE_NUMBER,
        pr_number=support._REVIEW_PR_NUMBER,
        branch="orchestrator/geserdugarov__agent-orchestrator/issue-711",
        codex_session_id=support._DEV_SESSION_ID,
        review_round=0,
    )
    return github, issue


def _review_prompt(case) -> str:
    github, issue = _review_seed()
    mocks = case._run(
        lambda: support.workflow._handle_validating(
            github,
            support._TEST_SPEC,
            issue,
        ),
        run_agent=support._agent(
            last_message=support.REVIEW_APPROVED_MESSAGE,
        ),
    )
    return support._prompt_of(mocks[support._RUN_AGENT_ATTR])
