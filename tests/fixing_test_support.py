# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and protocol values for fixing-stage tests."""

from __future__ import annotations

import dataclasses
import datetime as datetime_module
import pathlib
from unittest import mock

from orchestrator import config as _config
from orchestrator import workflow as _workflow
from orchestrator.stages import fixing
from tests import fakes, implementing_fixing_test_cases, workflow_helpers

dataclass = dataclasses.dataclass
config = _config
datetime = datetime_module.datetime
timedelta = datetime_module.timedelta
timezone = datetime_module.timezone
Path = pathlib.Path
MagicMock = mock.MagicMock
patch = mock.patch
workflow = _workflow

FakeComment = fakes.FakeComment
FakeGitHubClient = fakes.FakeGitHubClient
FakePR = fakes.FakePR
FakePRRef = fakes.FakePRRef
FakePRReview = fakes.FakePRReview
FakeUser = fakes.FakeUser
make_issue = fakes.make_issue
IssueScenario = implementing_fixing_test_cases.IssueScenario
posted_comment_contains = implementing_fixing_test_cases.posted_comment_contains

EVENT_AGENT_SPAWN = workflow_helpers.EVENT_AGENT_SPAWN
ROLE_DEVELOPER = workflow_helpers.ROLE_DEVELOPER
_PatchedWorkflowMixin = workflow_helpers._PatchedWorkflowMixin
_agent = workflow_helpers._agent

_clear_pending_fix_bookmarks = fixing._clear_pending_fix_bookmarks
_pending_fix_id_set = fixing._pending_fix_id_set
_reconstruct_pending_fix_batch = fixing._reconstruct_pending_fix_batch


def _branch(issue_number: int) -> str:
    """Return the per-issue PR branch used by the fixing handler."""
    return f"orchestrator/geserdugarov__agent-orchestrator/issue-{issue_number}"


FIXING = "fixing"
VALIDATING = "validating"
DOCUMENTING = "documenting"
IN_REVIEW = "in_review"

ISSUE = 880
PR_NUMBER = 880
BRANCH = _branch(ISSUE)
PR_HEAD_SHA = "cafe1234"

DEV_AGENT = "claude"
DEV_SESSION = "dev-sess"
FRESH_SESSION = "fresh-sess"
POISONED_SESSION = "poisoned-sess"

SHA_BEFORE = "sha-before"
SHA_AFTER = "sha-after"
SHA_SAME = "same-sha"

TRIGGER_ID = 2000
FOLLOWUP_ID = 2001
CONCURRENT_COMMENT_ID = 2500
PARKED_COMMENT_WATERMARK = 2500
HUMAN_REPLY_ID = 2600
TRANSIENT_PARK_WATERMARK = 5000
COMMAND_COMMENT_ID = 9000
INITIAL_PR_COMMENT_WATERMARK = 1999
PENDING_FIX_AT_TS = "2026-05-24T00:00:00+00:00"
EARLIER_PENDING_FIX_AT_TS = "2026-05-23T00:00:00+00:00"
INLINE_FEEDBACK_ID = 3000
REVIEW_SUMMARY_FEEDBACK_ID = 4000

ADVANCED_PR_COMMENT_WATERMARK = 8000
ADVANCED_REVIEW_COMMENT_WATERMARK = 50
ADVANCED_REVIEW_SUMMARY_WATERMARK = 10

BATCH_ISSUE_ID = 2050
BATCH_PR_CONVERSATION_ID = 2100
BATCH_INLINE_ID = 40
BATCH_INLINE_SECOND_ID = 41
BATCH_SUMMARY_ID = 7
BATCH_ISSUE_IDS = (BATCH_ISSUE_ID, BATCH_PR_CONVERSATION_ID)
BATCH_INLINE_IDS = (BATCH_INLINE_ID, BATCH_INLINE_SECOND_ID)
BATCH_SUMMARY_IDS = (BATCH_SUMMARY_ID,)
BATCH_LATER_ISSUE_ID = 9000
BATCH_ORCHESTRATOR_NOTE_ID = 2300
BATCH_INLINE_NOISE_ID = 99
BATCH_SUMMARY_NOISE_ID = 12
UNTRUSTED_ISSUE_ID = 2060
ORCHESTRATOR_PARK_COMMENT_ID = 2050
ALLOWLIST_FEEDBACK_ID = 3000

ALICE = "alice"
BOB = "bob"
CAROL = "carol"
DAVE = "dave"
ORCHESTRATOR = "orchestrator"

DEBOUNCE_SECONDS = 600

PARK_PUSH_FAILED = "push_failed"
PARK_AGENT_TIMEOUT = "agent_timeout"
PARK_AGENT_SILENT = "agent_silent"
PARK_AGENT_QUESTION = "agent_question"

AWAITING_HUMAN = "awaiting_human"
PARK_REASON = "park_reason"
REVIEW_ROUND = "review_round"
USER_CONTENT_HASH = "user_content_hash"
PRE_DEV_FIX_SHA = "pre_dev_fix_sha"
PR_LAST_COMMENT_ID = "pr_last_comment_id"
PR_LAST_REVIEW_COMMENT_ID = "pr_last_review_comment_id"
PR_LAST_REVIEW_SUMMARY_ID = "pr_last_review_summary_id"
PENDING_FIX_AT = "pending_fix_at"
PENDING_FIX_ISSUE_MAX_ID = "pending_fix_issue_max_id"
PENDING_FIX_ISSUE_IDS = "pending_fix_issue_ids"
PENDING_FIX_REVIEW_MAX_ID = "pending_fix_review_max_id"
PENDING_FIX_REVIEW_IDS = "pending_fix_review_ids"
PENDING_FIX_REVIEW_SUMMARY_MAX_ID = "pending_fix_review_summary_max_id"
PENDING_FIX_REVIEW_SUMMARY_IDS = "pending_fix_review_summary_ids"
PENDING_FIX_REVIEWER_COMMENT_ID = "pending_fix_reviewer_comment_id"

RUN_AGENT = "run_agent"
PUSH_BRANCH = "_push_branch"
WORKTREE_PATH = "_worktree_path"

CONTINUE_COMMAND = "/orchestrator continue"
DEV_SESSION_ID = "dev_session_id"
CHECK_SUCCESS = "success"
DEBOUNCE_CONFIG = "IN_REVIEW_DEBOUNCE_SECONDS"
PUSHED_FIX_MESSAGE = "pushed fix"
PUSHED_MESSAGE = "pushed"
RESUME_SESSION_ID = "resume_session_id"
CONTINUE_WORD = "continue"
FIX_FEEDBACK = "please address the typo"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
STALE_PRE_COMMENT_HASH = "stale-hash-pre-comment"
UNCHANGED_SHA = "aaa"
NOTHING_TO_DO_MESSAGE = "nothing to do"
ALLOWED_AUTHOR = "geserdugarov"
ALLOWED_AUTHORS_CONFIG = "ALLOWED_ISSUE_AUTHORS"
ID_LIST_KEY = "ids"
MAX_ID_KEY = "max"
NO_PRESERVED_MESSAGE = "no preserved"
ALLOWLIST_OUTSIDER = "mallory"
ALLOWLIST_MALICIOUS_URL = "https://example.invalid/malicious-patch.zip"
ALLOWLIST_BODY = "please tighten the integration test"
REVIEW_SUMMARY_SURFACE = "review_summary"
ALLOWLIST_SURFACES = (
    "issue_thread",
    "pr_conversation",
    "inline_review",
    REVIEW_SUMMARY_SURFACE,
)
PRESERVED_BATCH_BODIES = (
    "fix the null check",
    "handle the edge case",
    "rename the temp var",
    "please address the review",
)
TEMP_ROOT = Path("/tmp")
FRESH_COMMENT_DELAY_MINUTES = 30
HISTORICAL_COMMENT_ID = 500
MISSING_ANCHOR_ID = 999999
GUIDED_COMMENT_ID = 9001


class _InjectCommentAfterCall:
    def __init__(self, callback, issue, comment):
        self.callback = callback
        self.issue = issue
        self.comment = comment

    def __call__(self, *args, **kwargs):
        fix_result = self.callback(*args, **kwargs)
        self.issue.comments.append(self.comment)
        return fix_result


@dataclass(frozen=True)
class _ContinueSeed:
    park_reason: str | None
    command_body: str = CONTINUE_COMMAND
    command_id: int = COMMAND_COMMENT_ID
    command_on_pr_conversation: bool = False
    extra_issue_comments: tuple = ()
    with_batch_ids: bool = True
    pending_fix_at: str | None = PENDING_FIX_AT_TS
    silent_park_count: int = 2


class _FixingFixtureMixin(_PatchedWorkflowMixin):
    """Provide the common fixing-stage issue, PR, and pinned state."""

    def _seed(
        self,
        *,
        issue_number: int = ISSUE,
        pr=None,
        issue_comments=(),
        with_pr_number: bool = True,
        extra_state=None,
    ):
        gh = FakeGitHubClient()
        issue = make_issue(issue_number, label=FIXING)
        for comment in issue_comments:
            issue.comments.append(comment)
        gh.add_issue(issue)
        if pr is not None:
            gh.add_pr(pr)
        state: dict = {
            "branch": BRANCH,
            "dev_agent": DEV_AGENT,
            DEV_SESSION_ID: DEV_SESSION,
            REVIEW_ROUND: 1,
            PR_LAST_COMMENT_ID: INITIAL_PR_COMMENT_WATERMARK,
            PR_LAST_REVIEW_COMMENT_ID: 0,
            PR_LAST_REVIEW_SUMMARY_ID: 0,
            PENDING_FIX_AT: PENDING_FIX_AT_TS,
            PENDING_FIX_ISSUE_MAX_ID: TRIGGER_ID,
        }
        if with_pr_number and pr is not None:
            state["pr_number"] = pr.number
        if extra_state:
            state.update(extra_state)
        gh.seed_state(issue_number, **state)
        return gh, issue

    def _open_pr(self, **kwargs):
        defaults = {
            "number": PR_NUMBER,
            "head_branch": BRANCH,
            "head": FakePRRef(sha=PR_HEAD_SHA),
            "mergeable": True,
            "check_state": CHECK_SUCCESS,
        }
        defaults.update(kwargs)
        return FakePR(**defaults)


class _StrandedFixingFixtureMixin(_FixingFixtureMixin):
    def _seed_stranded(self, *, reply_id: int = HUMAN_REPLY_ID):
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        reply = FakeComment(
            id=reply_id,
            body=CONTINUE_WORD,
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        return self._seed(
            pr=pr,
            issue_comments=[reply],
            extra_state={
                AWAITING_HUMAN: True,
                PARK_REASON: None,
                PR_LAST_COMMENT_ID: PARKED_COMMENT_WATERMARK,
                PENDING_FIX_AT: None,
                PENDING_FIX_ISSUE_MAX_ID: None,
                REVIEW_ROUND: 2,
            },
        )
