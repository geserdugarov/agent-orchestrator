# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared values and fakes for workflow drift tests."""
from __future__ import annotations

from dataclasses import dataclass

from orchestrator import workflow

from tests import fakes as _fakes
from tests import workflow_helpers as _helpers


BACKEND_CLAUDE = _helpers.BACKEND_CLAUDE
KEY_AWAITING_HUMAN = _helpers.KEY_AWAITING_HUMAN
KEY_LAST_ACTION_COMMENT_ID = _helpers.KEY_LAST_ACTION_COMMENT_ID
LABEL_BLOCKED = _helpers.LABEL_BLOCKED
LABEL_IMPLEMENTING = _helpers.LABEL_IMPLEMENTING
LABEL_IN_REVIEW = _helpers.LABEL_IN_REVIEW
LABEL_RESOLVING_CONFLICT = _helpers.LABEL_RESOLVING_CONFLICT
LABEL_VALIDATING = _helpers.LABEL_VALIDATING
_PatchedWorkflowMixin = _helpers._PatchedWorkflowMixin
_TEST_SPEC = _helpers._TEST_SPEC
_agent = _helpers._agent

FakeComment = _fakes.FakeComment
FakeGitHubClient = _fakes.FakeGitHubClient
FakePR = _fakes.FakePR
FakeUser = _fakes.FakeUser
make_issue = _fakes.make_issue

KEY_USER_CONTENT_HASH = "user_content_hash"

TRUSTED_AUTHOR = "alice"
DEV_SESSION = "dev-sess"
STALE_HASH = "stale-hash"
SAME_SHA = "same-sha"
SHA_AFTER = "after"

CONTINUE_COMMAND = "/orchestrator continue"
NEW_BODY = "new body"
CLARIFIED_BODY = "clarified body"
EXISTING_WORK_MESSAGE = "existing work satisfies the edit"
PARK_AGENT_SILENT = "agent_silent"
UNCHANGED_SHA = "same"

_BOT_COMMENT_ID = 200
_PINNED_COMMENT_ID = 300
_GUIDED_CONTINUE_COMMENT_ID = 101
_PROMPT_COMMENT_ID = 500
_INITIAL_LAST_ACTION_COMMENT_ID = 500
_BLOCKED_CHILD_ISSUE_NUMBER = 200
_BLOCKED_PARENT_ISSUE_NUMBER = 199
_VALIDATING_ACK_ISSUE_NUMBER = 600
_VALIDATING_ACK_PR_NUMBER = 6000
_IN_REVIEW_ACK_ISSUE_NUMBER = 700
_IN_REVIEW_ACK_PR_NUMBER = 7000
_VALIDATING_WATERMARK_ISSUE_NUMBER = 900
_VALIDATING_WATERMARK_PR_NUMBER = 9000
_VALIDATING_WATERMARK_COMMENT_ID = 5000
_IN_REVIEW_WATERMARK_ISSUE_NUMBER = 910
_IN_REVIEW_WATERMARK_PR_NUMBER = 9100
_IN_REVIEW_WATERMARK_COMMENT_ID = 6000
_IMPLEMENTING_WATERMARK_ISSUE_NUMBER = 920
_IMPLEMENTING_WATERMARK_COMMENT_ID = 7000
_CONFLICT_WATERMARK_ISSUE_NUMBER = 930
_CONFLICT_WATERMARK_PR_NUMBER = 9300
_CONFLICT_WATERMARK_COMMENT_ID = 8000
_EVICTED_BOT_COMMENT_ID = 12345
_HUMAN_COMMENT_ID = 12346
_BOT_FILTER_HUMAN_COMMENT_ID = 900
_BOT_FILTER_BOT_COMMENT_ID = 901
_TYPED_HUMAN_COMMENT_ID = 910
_VALIDATING_CLARIFICATION_ISSUE_NUMBER = 601
_VALIDATING_CLARIFICATION_PR_NUMBER = 6001
_IN_REVIEW_CLARIFICATION_ISSUE_NUMBER = 701
_IN_REVIEW_CLARIFICATION_PR_NUMBER = 7001
_IMPLEMENTING_CLARIFICATION_ISSUE_NUMBER = 602


def _continue_comment(body: str) -> FakeComment:
    return FakeComment(
        id=1,
        body=body,
        user=FakeUser(TRUSTED_AUTHOR),
    )


@dataclass(frozen=True)
class _ContentChangeContext:
    github: FakeGitHubClient
    issue: object
    state: object
    prior_hash: str
    current_hash: str
    before_writes: int


def _content_change_case(
    old_body: str,
    new_body: str,
    *,
    comments: tuple[FakeComment, ...] = (),
    include_bare_continue: bool = False,
) -> _ContentChangeContext:
    old_issue = make_issue(1, body=old_body, comments=list(comments))
    prior_hash = workflow._compute_user_content_hash(
        old_issue,
        set(),
        include_bare_continue=include_bare_continue,
    )
    issue = make_issue(1, body=new_body, comments=list(comments))
    github = FakeGitHubClient()
    github.add_issue(issue)
    github.seed_state(1, user_content_hash=prior_hash)
    state = github.read_pinned_state(issue)
    return _ContentChangeContext(
        github=github,
        issue=issue,
        state=state,
        prior_hash=prior_hash,
        current_hash=workflow._compute_user_content_hash(issue, set()),
        before_writes=github.write_state_calls,
    )
