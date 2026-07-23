# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations


from orchestrator import workflow

from tests.fakes import (
    FakeGitHubClient,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
)


# --- Workflow labels this stage routes between --------------------------
DOCUMENTING = "documenting"
IN_REVIEW = "in_review"
VALIDATING = "validating"

# --- Dev agent identity pinned into per-issue state ---------------------
DEV_AGENT = "codex"
DEV_SESSION = "dev-sess"

# --- Worktree HEAD SHAs threaded through the docs / recovery flows ------
SHA_BEFORE = "aaa"
SHA_AFTER = "bbb"
SHA_DOCS = "docs-sha"
SHA_RECOVERED = "recovered-sha"
SHA_PR_HEAD = "pr-head-sha"

# --- Pinned-state field keys read back from `gh.pinned_data(...)` -------
DOCS_VERDICT = "docs_verdict"
DOCS_CHECKED_SHA = "docs_checked_sha"
REVIEW_ROUND = "review_round"
PARK_REASON = "park_reason"
AWAITING_HUMAN = "awaiting_human"
LAST_ACTION_COMMENT_ID = "last_action_comment_id"

# --- Awaiting-human park reasons the docs handler writes ----------------
PARK_PUSH_FAILED = "push_failed"
PARK_AGENT_SILENT = "agent_silent"
PARK_AGENT_TIMEOUT = "agent_timeout"
PARK_DIVERGED = "diverged_branch"
PARK_FETCH_FAILED = "fetch_failed"
PARK_DIRTY = "dirty_worktree"
PARK_AGENT_QUESTION = "agent_question"
PARK_RESET_FAILED = "worktree_reset_failed"

# --- Docs verdict values persisted on a successful pass -----------------
VERDICT_UPDATED = "updated"
VERDICT_NO_CHANGE = "no_change"

# --- Repo docs paths the agent edits / the dirty guard reports ----------
README = "README.md"
DOCS_ARCHITECTURE = "docs/architecture.md"

# --- Mock keys returned by `_PatchedWorkflowMixin._run` -----------------
RUN_AGENT = "run_agent"
PUSH_BRANCH = "_push_branch"

UNCOMMITTED_CHANGE = "uncommitted change"
TRUSTED_AUTHOR = "alice"
USER_CONTENT_CHANGED = "issue body changed"
AUTHED_FETCH = "_authed_fetch"
ORIGINAL_BODY = "original body"
UPDATED_BODY_AFTER_DOCS = "updated body after prior docs commit"
WORKTREE_PATH = "_worktree_path"
GIT_HARDENED = "_git_hardened"
GIT_REV_LIST = "rev-list"
GIT_RESET = "reset"
GIT_HARD_RESET = "--hard"
GIT_CLEAN = "clean"
GIT_CLEAN_FLAGS = "-fd"
DRIFT_UNWIND_PENDING = "docs_drift_unwind_pending"

MISSING_PR_ISSUE_NUMBER = 101
PARKED_MISSING_PR_ISSUE_NUMBER = 102
COMMIT_REPLY_ISSUE_NUMBER = 401
COMMIT_REPLY_PR_NUMBER = 41
COMMIT_REPLY_COMMENT_ID = 2100
COMMIT_REPLY_WATERMARK = 2000
NO_COMMIT_REPLY_ISSUE_NUMBER = 403
NO_COMMIT_REPLY_PR_NUMBER = 43
NO_COMMIT_REPLY_COMMENT_ID = 3100
NO_COMMIT_REPLY_WATERMARK = 3000
RECOVERED_REPLY_ISSUE_NUMBER = 404
RECOVERED_REPLY_PR_NUMBER = 44
RECOVERED_REPLY_COMMENT_ID = 4100
RECOVERED_REPLY_WATERMARK = 4000
FAILED_PUSH_REPLY_ISSUE_NUMBER = 405
FAILED_PUSH_REPLY_PR_NUMBER = 45
FAILED_PUSH_REPLY_COMMENT_ID = 5100
FAILED_PUSH_REPLY_WATERMARK = 5000
NO_NEW_COMMENT_ISSUE_NUMBER = 402
NO_NEW_COMMENT_PR_NUMBER = 42
NO_NEW_COMMENT_WATERMARK = 2500
FULL_PROMPT_REPLY_ISSUE_NUMBER = 406
FULL_PROMPT_REPLY_PR_NUMBER = 46
FULL_PROMPT_REPLY_COMMENT_ID = 6100
FULL_PROMPT_REPLY_WATERMARK = 6000
NO_CHANGE_REPLY_ISSUE_NUMBER = 407
NO_CHANGE_REPLY_PR_NUMBER = 47
NO_CHANGE_REPLY_COMMENT_ID = 7100
NO_CHANGE_REPLY_WATERMARK = 7000
CONTINUE_COMMENT_ID = 9000
CONTINUE_PR_NUMBER = 47
CONTINUE_WATERMARK = 8000
CONTINUE_ISSUE_NUMBER = 730
QUESTION_CONTINUE_ISSUE_NUMBER = 731
INTERRUPTED_ISSUE_NUMBER = 202
INTERRUPTED_PR_NUMBER = 21
INTERRUPTED_RESUME_ISSUE_NUMBER = 203
INTERRUPTED_RESUME_PR_NUMBER = 23
INTERRUPTED_RESUME_COMMENT_ID = 2100
INTERRUPTED_RESUME_WATERMARK = 2000
PARKED_FIXTURE_WATERMARK = 6000
GIT_FAILURE_EXIT_CODE = 128
PENDING_UNWIND_COMMENT_ID = 999
EXTERNAL_MERGE_ISSUE_NUMBER = 180
EXTERNAL_MERGE_PR_NUMBER = 18000
CLOSED_ISSUE_NUMBER = 181
CLOSED_PR_NUMBER = 18100
FINAL_DOCS_PR_WATERMARK = 999
FINAL_DOCS_REPLY_ID = 2000
WATERMARK_ISSUE_NUMBER = 709
WATERMARK_PR_NUMBER = 73
PICKUP_COMMENT_ID = 900
PARK_COMMENT_ID = 950
HUMAN_REPLY_ID = 1100


def _branch(issue_number: int) -> str:
    """The per-issue PR branch the docs handler anchors on."""
    return f"orchestrator/geserdugarov__agent-orchestrator/issue-{issue_number}"


class _DocumentingWorkflowMixin(_PatchedWorkflowMixin):
    def _run_documenting(self, gh, issue, **run_options):
        return self._run(
            lambda: workflow._handle_documenting(gh, _TEST_SPEC, issue),
            **run_options,
        )


class _BasicDocumentingFixture(_DocumentingWorkflowMixin):
    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(self.issue_number, label=DOCUMENTING)
        gh.add_issue(issue)
        defaults = dict(
            pr_number=self.pr_number,
            branch=_branch(self.issue_number),
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )
        defaults.update(state)
        gh.seed_state(self.issue_number, **defaults)
        return gh, issue


class _FreshDocumentingFixture(_BasicDocumentingFixture):
    issue_number = 201
    pr_number = 21
