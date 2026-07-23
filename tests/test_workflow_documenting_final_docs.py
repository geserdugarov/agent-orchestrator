# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from orchestrator import workflow

from tests.documenting_assertion_test_support import _issue_comment_text
from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _TEST_SPEC,
    _agent,
)


# --- Workflow labels this stage routes between --------------------------
from tests.documenting_test_support import (
    _branch,
)
from tests.documenting_scenario_test_support import (
    _FinalDocsFixture,
)

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


class HandleDocumentingFinalDocsHandoffTest(unittest.TestCase, _FinalDocsFixture):
    """Issue #266: when `_handle_validating` approves and relabels to
    `documenting`, the next `_handle_documenting` tick must advance to
    `in_review` (NOT back to `validating`) on every success exit.
    """

    def test_no_change_verdict_advances_to_in_review(
        self,
    ) -> None:
        gh, issue = self._seeded()
        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message=("Inspected diff; no user-facing change.\nDOCS: NO_CHANGE"),
            ),
            push_branch=True,
            # No commit landed: before_sha == after_sha == approved head.
            head_shas=["approvedSha", "approvedSha"],
            branch_ahead_behind=(0, 0),
        )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertNotIn((self.issue_number, VALIDATING), gh.label_history)
        state = gh.pinned_data(self.issue_number)
        self.assertEqual(state.get(DOCS_VERDICT), VERDICT_NO_CHANGE)

    def test_recovered_ahead_routes_to_in_review(
        self,
    ) -> None:
        # A previous final-docs tick committed but parked before the
        # push landed. The resume's no-change verdict triggers the
        # ahead-push branch; the recovered commit is now the new PR
        # head.
        gh, issue = self._seeded(awaiting_human=True, park_reason=PARK_PUSH_FAILED)
        issue.comments.append(
            FakeComment(
                id=FINAL_DOCS_REPLY_ID,
                body="retry please",
                user=FakeUser(TRUSTED_AUTHOR),
            ),
        )

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message=("Re-checked diff; the existing docs commit already covers it.\nDOCS: NO_CHANGE"),
            ),
            push_branch=True,
            # before_sha (awaiting-human resume snapshot) == after_sha
            # (no new commit), but ahead=1 (the recovered docs commit
            # from a prior tick) -- the helper pushes it and routes.
            head_shas=["recoveredDocsSha", "recoveredDocsSha"],
            branch_ahead_behind=(1, 0),
        )

        mocks[PUSH_BRANCH].assert_called_once()
        self.assertIn((self.issue_number, IN_REVIEW), gh.label_history)

    def test_drift_routes_to_validating_without_spawn(
        self,
    ) -> None:
        # A human body edit during the final-docs hop must reset
        # `review_round=0`, post the notice, and relabel to
        # `validating` so the reviewer re-evaluates on the next tick
        # -- WITHOUT spawning the docs agent (a docs commit against
        # the old body would just need to be re-reviewed alongside
        # any impl change).
        gh, issue = self._seeded(user_content_hash="oldhash")

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
        )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        # Drift posted the issue-thread notice.
        self.assertIn(
            USER_CONTENT_CHANGED,
            _issue_comment_text(gh, self.issue_number),
        )
        # Route back through `validating`.
        self.assertIn((self.issue_number, VALIDATING), gh.label_history)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        state = gh.pinned_data(self.issue_number)
        self.assertEqual(state.get(REVIEW_ROUND), 0)

    def test_consumed_reply_not_replayed_as_feedback(
        self,
    ) -> None:
        # Lifecycle: validating approves at SHA `approvedSha` and seeds
        # `pr_last_comment_id=900` (past its pickup / PR-opened /
        # approval orchestrator comments). The first documenting tick
        # asks a question and parks at id=950 (orchestrator park
        # comment). A human replies at id=1100 on the issue thread.
        # The next documenting tick's awaiting-human resume consumes
        # 1100 (advances `last_action_comment_id=1100`), the dev
        # produces a docs commit, the helper pushes and relabels to
        # `in_review`. Without the watermark ratchet,
        # `pr_last_comment_id` stays at 900, and the next in_review
        # tick scans `comments_after(900)`, sees 1100 as fresh PR
        # feedback, and bounces to `fixing` over work the docs pass
        # already addressed.
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(
            WATERMARK_ISSUE_NUMBER,
            label=DOCUMENTING,
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=":robot: orchestrator picking this up.",
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PARK_COMMENT_ID,
                    body=":sos: agent needs your input to proceed",
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=HUMAN_REPLY_ID,
                    body="please cover edge case X in README",
                    user=FakeUser(TRUSTED_AUTHOR),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        gh.add_pr(
            FakePR(
                number=WATERMARK_PR_NUMBER,
                head_branch=_branch(WATERMARK_ISSUE_NUMBER),
                head=FakePRRef(sha="docsSha"),
                mergeable=True,
                check_state="success",
            ),
        )
        gh.seed_state(
            WATERMARK_ISSUE_NUMBER,
            pr_number=WATERMARK_PR_NUMBER,
            branch=_branch(WATERMARK_ISSUE_NUMBER),
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
            review_round=1,
            pr_last_comment_id=PICKUP_COMMENT_ID,
            pickup_comment_id=PICKUP_COMMENT_ID,
            orchestrator_comment_ids=[PICKUP_COMMENT_ID, PARK_COMMENT_ID],
            awaiting_human=True,
            park_reason=PARK_AGENT_QUESTION,
            last_action_comment_id=PARK_COMMENT_ID,
        )

        # Documenting tick: awaiting-human resume consumes id=1100,
        # docs commit lands, advance to in_review.
        self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="docs: cover edge case X",
            ),
            push_branch=True,
            head_shas=["approvedSha", "docsSha"],
            branch_ahead_behind=(0, 0),
        )

        self.assertIn(
            (WATERMARK_ISSUE_NUMBER, IN_REVIEW),
            gh.label_history,
        )
        pinned_state = gh.pinned_data(WATERMARK_ISSUE_NUMBER)
        self.assertEqual(
            pinned_state.get(LAST_ACTION_COMMENT_ID),
            HUMAN_REPLY_ID,
        )
        self.assertGreaterEqual(
            pinned_state.get("pr_last_comment_id"),
            HUMAN_REPLY_ID,
            "pr_last_comment_id must ratchet past the consumed human "
            "issue-thread reply on the final-docs handoff so the next "
            "in_review tick does not replay it as fresh PR feedback",
        )

        # In_review tick: ensure the consumed reply is NOT replayed as
        # fresh feedback (the actual route-to-fixing bug the ratchet
        # guards against).
        if not any(label.name == IN_REVIEW for label in issue.labels):
            issue.labels = [FakeLabel(IN_REVIEW)]
        mocks_ir = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks_ir[RUN_AGENT].assert_not_called()
        self.assertNotIn(
            (709, "fixing"),
            gh.label_history,
            "in_review must not bounce to `fixing` over a human reply "
            "the documenting awaiting-human resume already consumed",
        )


if __name__ == "__main__":
    unittest.main()
