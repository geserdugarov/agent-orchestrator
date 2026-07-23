# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and protocol values for implementing drift tests."""

from __future__ import annotations

from orchestrator import workflow
from tests import fakes, implementing_fixing_test_cases, workflow_helpers

FakeComment = fakes.FakeComment
FakeGitHubClient = fakes.FakeGitHubClient
FakeUser = fakes.FakeUser
IssueScenario = implementing_fixing_test_cases.IssueScenario
posted_comment_contains = implementing_fixing_test_cases.posted_comment_contains
make_issue = fakes.make_issue
LABEL_IMPLEMENTING = workflow_helpers.LABEL_IMPLEMENTING
LABEL_VALIDATING = workflow_helpers.LABEL_VALIDATING
_PatchedWorkflowMixin = workflow_helpers._PatchedWorkflowMixin
_agent = workflow_helpers._agent
_issue_branch = workflow_helpers._issue_branch

RUN_AGENT = "run_agent"
USER_CONTENT_HASH = "user_content_hash"
AWAITING_HUMAN = "awaiting_human"
LAST_ACTION_COMMENT_ID = "last_action_comment_id"
STALE_CONTENT_HASH = "stale-hash"
DEV_AGENT = "claude"
DEV_SESSION = "dev-sess"
FRESH_SESSION = "new-sess"
IMPLEMENTED_MESSAGE = "implemented"
UPDATED_REQUIREMENTS = "new requirements"
IMPLEMENTER_PROMPT_FRAGMENT = "You are the implementer"
CONTINUE_COMMAND = "/orchestrator continue"
DRIFT_RESUME_ISSUE = 60
FRESH_DRIFT_ISSUE = 61
INTERRUPTED_DRIFT_ISSUE = 62
RECOVERED_COMMITS_ISSUE = 850
NO_SESSION_RECOVERED_ISSUE = 860
NO_SESSION_FRESH_ISSUE = 861
AWAITING_BODY_DRIFT_ISSUE = 1200
AWAITING_COMMENT_DRIFT_ISSUE = 1210
CONTINUE_RETRY_ISSUE = 730
CONTINUE_QUESTION_ISSUE = 731
CONTINUE_GUIDED_ISSUE = 732
HUMAN_COMMENT_ID = 500
PICKUP_COMMENT_ID = 900
COMMAND_COMMENT_ID = 9000
PRIOR_ACTION_WATERMARK = 8000


def _seed_parked_implementing(
    number: int,
    *,
    park_reason,
    command_body=CONTINUE_COMMAND,
    drift_neutral=False,
):
    gh = FakeGitHubClient()
    issue = make_issue(number, label=LABEL_IMPLEMENTING, body="the requirements")
    command = FakeComment(
        id=COMMAND_COMMENT_ID,
        body=command_body,
        user=FakeUser("dave"),
    )
    issue.comments.append(command)
    gh.add_issue(issue)
    content_hash = workflow._compute_user_content_hash(issue, set()) if drift_neutral else STALE_CONTENT_HASH
    gh.seed_state(
        number,
        user_content_hash=content_hash,
        dev_agent=DEV_AGENT,
        dev_session_id=DEV_SESSION,
        awaiting_human=True,
        park_reason=park_reason,
        silent_park_count=1,
        last_action_comment_id=PRIOR_ACTION_WATERMARK,
        branch=_issue_branch(number),
    )
    return gh, issue
