# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.documenting_assertion_test_support import (
    _agent_prompt,
    _pr_comment_text,
)
from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
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
    _DocumentingWorkflowMixin,
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


class HandleDocumentingAwaitingHumanResumeTest(unittest.TestCase, _DocumentingWorkflowMixin):
    """Awaiting-human resume: a human reply re-runs the full
    documentation prompt (NOT the short human-reply followup that
    implementing/validating use). Documenting's stage instructions
    (`DOCS: NO_CHANGE` marker, what files to inspect, what to commit)
    are part of the prompt itself, so a resume that skips them would
    let a `fetch_failed` / `agent_timeout` / `agent_silent` retry
    advance via a stray no-change verdict without ever doing a real
    docs pass."""

    def test_commit_reply_resumes_and_advances(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(COMMIT_REPLY_ISSUE_NUMBER, label=DOCUMENTING)
        issue.comments.append(
            FakeComment(
                id=COMMIT_REPLY_COMMENT_ID,
                body="add a note about flag X",
                user=FakeUser(TRUSTED_AUTHOR),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            COMMIT_REPLY_ISSUE_NUMBER,
            pr_number=COMMIT_REPLY_PR_NUMBER,
            branch=_branch(COMMIT_REPLY_ISSUE_NUMBER),
            awaiting_human=True,
            last_action_comment_id=COMMIT_REPLY_WATERMARK,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="docs: flag X explained",
            ),
            push_branch=True,
            # The awaiting-human path captures `before_sha` from the PR
            # worktree BEFORE the resume, then reads `after_sha` post-
            # spawn. before_sha != after_sha means a docs commit
            # landed.
            head_shas=[SHA_BEFORE, SHA_AFTER],
            branch_ahead_behind=(0, 0),
        )

        # The resumed run is the only agent spawn.
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        # The PR worktree is anchored BEFORE the resume helper runs so
        # the helper's `_ensure_worktree` fallback cannot restore the
        # per-issue branch from `<remote>/<base>` and lose the dev's
        # PR commits.
        mocks["_ensure_pr_worktree"].assert_called_once_with(
            _TEST_SPEC,
            COMMIT_REPLY_ISSUE_NUMBER,
            branch=_branch(COMMIT_REPLY_ISSUE_NUMBER),
        )
        mocks[PUSH_BRANCH].assert_called_once()
        self.assertIn(
            (COMMIT_REPLY_ISSUE_NUMBER, IN_REVIEW),
            gh.label_history,
        )
        state = gh.pinned_data(COMMIT_REPLY_ISSUE_NUMBER)
        self.assertEqual(state.get(DOCS_VERDICT), VERDICT_UPDATED)
        # The pre-park comment id was consumed by the resume.
        self.assertEqual(
            state.get(LAST_ACTION_COMMENT_ID),
            COMMIT_REPLY_COMMENT_ID,
        )

    def test_human_reply_no_commit_does_not_advance(self) -> None:
        # The resume produces no new commit (the dev replied with a
        # clarification or the agent did nothing). We MUST NOT treat
        # the PR's pre-existing implementation HEAD as a "new docs
        # commit" and advance -- that would push an undocumented PR
        # forward.
        gh = FakeGitHubClient()
        issue = make_issue(NO_COMMIT_REPLY_ISSUE_NUMBER, label=DOCUMENTING)
        issue.comments.append(
            FakeComment(
                id=NO_COMMIT_REPLY_COMMENT_ID,
                body="why?",
                user=FakeUser(TRUSTED_AUTHOR),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            NO_COMMIT_REPLY_ISSUE_NUMBER,
            pr_number=NO_COMMIT_REPLY_PR_NUMBER,
            branch=_branch(NO_COMMIT_REPLY_ISSUE_NUMBER),
            awaiting_human=True,
            last_action_comment_id=NO_COMMIT_REPLY_WATERMARK,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
            # NB: no `docs_checked_sha` -- the prior tick parked before
            # snapshotting one. The fix must capture a fresh
            # `before_sha` from the PR worktree at this tick.
        )

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="should I also update README?",
            ),
            push_branch=True,
            # Same SHA before/after -- nothing new committed even
            # though HEAD is non-empty (the dev's implementation
            # commit).
            head_shas=[SHA_PR_HEAD, SHA_PR_HEAD],
            branch_ahead_behind=(0, 0),
        )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertNotIn(
            (NO_COMMIT_REPLY_ISSUE_NUMBER, IN_REVIEW),
            gh.label_history,
        )
        self.assertNotIn(
            (NO_COMMIT_REPLY_ISSUE_NUMBER, VALIDATING),
            gh.label_history,
        )
        state = gh.pinned_data(NO_COMMIT_REPLY_ISSUE_NUMBER)
        # Still parked: no commit means the docs pass did not land
        # anything and the issue must stay awaiting human input.
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertNotIn(DOCS_VERDICT, state)

    def test_no_change_reply_pushes_local_commit(self) -> None:
        # A previous tick committed docs and then parked (push_failed
        # / agent_timeout / dirty) -- the worktree carries an unpushed
        # docs commit (ahead == 1). The human's retry resumes the dev
        # which returns DOCS: NO_CHANGE without committing further.
        # The handler MUST push the pre-existing local commit before
        # advancing: a NO_CHANGE verdict only certifies the local
        # tree, not the remote PR head. Without the push the issue
        # would advance with the docs commit invisible to the human
        # who eventually clicks Merge on the PR (the commit would
        # still be sitting locally, unpushed).
        gh = FakeGitHubClient()
        issue = make_issue(RECOVERED_REPLY_ISSUE_NUMBER, label=DOCUMENTING)
        issue.comments.append(
            FakeComment(
                id=RECOVERED_REPLY_COMMENT_ID,
                body="try again",
                user=FakeUser(TRUSTED_AUTHOR),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            RECOVERED_REPLY_ISSUE_NUMBER,
            pr_number=RECOVERED_REPLY_PR_NUMBER,
            branch=_branch(RECOVERED_REPLY_ISSUE_NUMBER),
            awaiting_human=True,
            last_action_comment_id=RECOVERED_REPLY_WATERMARK,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
            park_reason=PARK_PUSH_FAILED,
        )

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="No further docs needed.\nDOCS: NO_CHANGE",
            ),
            push_branch=True,
            # Same SHA before/after -- dev added nothing. The SHA
            # holds the prior tick's docs commit (which the remote
            # does not yet have).
            head_shas=[SHA_DOCS, SHA_DOCS],
            # ahead = 1 means the unpushed docs commit is still
            # waiting to land on the PR.
            branch_ahead_behind=(1, 0),
        )

        mocks[PUSH_BRANCH].assert_called_once()
        self.assertIn(
            (RECOVERED_REPLY_ISSUE_NUMBER, IN_REVIEW),
            gh.label_history,
        )
        state = gh.pinned_data(RECOVERED_REPLY_ISSUE_NUMBER)
        self.assertEqual(state.get(DOCS_VERDICT), VERDICT_UPDATED)
        self.assertEqual(state.get(DOCS_CHECKED_SHA), SHA_DOCS)
        # The PR comment names the recovery-on-no-change path so a
        # reviewer scanning the PR can see why we advanced.
        self.assertIn("recovered docs commit", _pr_comment_text(gh))

    def test_no_change_reply_parks_on_push_error(self) -> None:
        # Same shape as the previous test but the recovery push
        # itself fails. The issue must park with `push_failed` and
        # NOT advance -- the docs commit is still local-only.
        gh = FakeGitHubClient()
        issue = make_issue(FAILED_PUSH_REPLY_ISSUE_NUMBER, label=DOCUMENTING)
        issue.comments.append(
            FakeComment(
                id=FAILED_PUSH_REPLY_COMMENT_ID,
                body="retry",
                user=FakeUser(TRUSTED_AUTHOR),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            FAILED_PUSH_REPLY_ISSUE_NUMBER,
            pr_number=FAILED_PUSH_REPLY_PR_NUMBER,
            branch=_branch(FAILED_PUSH_REPLY_ISSUE_NUMBER),
            awaiting_human=True,
            last_action_comment_id=FAILED_PUSH_REPLY_WATERMARK,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="Reviewed; no change.\nDOCS: NO_CHANGE",
            ),
            push_branch=False,
            head_shas=[SHA_DOCS, SHA_DOCS],
            branch_ahead_behind=(1, 0),
        )

        mocks[PUSH_BRANCH].assert_called_once()
        self.assertNotIn(
            (FAILED_PUSH_REPLY_ISSUE_NUMBER, IN_REVIEW),
            gh.label_history,
        )
        self.assertNotIn(
            (FAILED_PUSH_REPLY_ISSUE_NUMBER, VALIDATING),
            gh.label_history,
        )
        state = gh.pinned_data(FAILED_PUSH_REPLY_ISSUE_NUMBER)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_PUSH_FAILED)

    def test_no_new_comments_keeps_parked(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(NO_NEW_COMMENT_ISSUE_NUMBER, label=DOCUMENTING)
        gh.add_issue(issue)
        gh.seed_state(
            NO_NEW_COMMENT_ISSUE_NUMBER,
            pr_number=NO_NEW_COMMENT_PR_NUMBER,
            branch=_branch(NO_NEW_COMMENT_ISSUE_NUMBER),
            awaiting_human=True,
            last_action_comment_id=NO_NEW_COMMENT_WATERMARK,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
        )

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(),
            push_branch=True,
            head_shas=[SHA_BEFORE],
            branch_ahead_behind=(0, 0),
        )

        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertNotIn(
            (NO_NEW_COMMENT_ISSUE_NUMBER, IN_REVIEW),
            gh.label_history,
        )
        self.assertNotIn(
            (NO_NEW_COMMENT_ISSUE_NUMBER, VALIDATING),
            gh.label_history,
        )
        # Still parked; nothing changed.
        self.assertTrue(gh.pinned_data(NO_NEW_COMMENT_ISSUE_NUMBER).get(AWAITING_HUMAN))

    def test_reply_uses_full_documentation_prompt(self) -> None:
        # Regression: a `fetch_failed` / `agent_timeout` /
        # `agent_silent` resume cannot use the generic
        # `_resume_developer_on_human_reply` followup (which
        # contains ONLY the human's new comment text) -- the
        # documentation prompt's instructions
        # (DOCS: NO_CHANGE marker, files to inspect, what to
        # commit) must be reissued each resume. Otherwise the dev
        # could emit a stray no-change verdict learned from an
        # earlier spawn and advance without doing a real docs
        # pass.
        gh = FakeGitHubClient()
        issue = make_issue(
            FULL_PROMPT_REPLY_ISSUE_NUMBER,
            label=DOCUMENTING,
            body="implement helpful_function(x)",
        )
        issue.comments.append(
            FakeComment(
                id=FULL_PROMPT_REPLY_COMMENT_ID,
                body="please retry",
                user=FakeUser(TRUSTED_AUTHOR),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            FULL_PROMPT_REPLY_ISSUE_NUMBER,
            pr_number=FULL_PROMPT_REPLY_PR_NUMBER,
            branch=_branch(FULL_PROMPT_REPLY_ISSUE_NUMBER),
            awaiting_human=True,
            last_action_comment_id=FULL_PROMPT_REPLY_WATERMARK,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
            park_reason=PARK_AGENT_TIMEOUT,
        )

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="docs: documented helpful_function",
            ),
            push_branch=True,
            head_shas=[SHA_BEFORE, SHA_AFTER],
            branch_ahead_behind=(0, 0),
        )

        # The prompt MUST be the full docs prompt, not just the
        # human's "please retry" comment.
        prompt = _agent_prompt(mocks)
        # Hallmarks of `_build_documentation_prompt`:
        self.assertIn("documentation pass", prompt)
        self.assertIn("DOCS: NO_CHANGE", prompt)
        # The issue body is embedded so the dev re-reads the
        # current requirements.
        self.assertIn("implement helpful_function(x)", prompt)
        # The human's reply still surfaces (via the
        # recent-comments thread that the prompt embeds).
        self.assertIn("please retry", prompt)
        # Comment was consumed.
        state = gh.pinned_data(FULL_PROMPT_REPLY_ISSUE_NUMBER)
        self.assertEqual(
            state.get(LAST_ACTION_COMMENT_ID),
            FULL_PROMPT_REPLY_COMMENT_ID,
        )

    def test_no_change_reply_persists_docs_sha(self) -> None:
        # Regression: a NO_CHANGE outcome on a resume (no prior
        # fresh-spawn ran on this issue this lifecycle) must
        # still persist `docs_checked_sha` to the SHA the dev
        # evaluated. Without it, a subsequent no-change retry
        # after a transient park (`fetch_failed`,
        # `diverged_branch`, timeout) would leave the watermark
        # unset and downstream consumers could not tell which
        # commit was verified.
        gh = FakeGitHubClient()
        issue = make_issue(NO_CHANGE_REPLY_ISSUE_NUMBER, label=DOCUMENTING)
        issue.comments.append(
            FakeComment(
                id=NO_CHANGE_REPLY_COMMENT_ID,
                body="retry",
                user=FakeUser(TRUSTED_AUTHOR),
            )
        )
        gh.add_issue(issue)
        gh.seed_state(
            NO_CHANGE_REPLY_ISSUE_NUMBER,
            pr_number=NO_CHANGE_REPLY_PR_NUMBER,
            branch=_branch(NO_CHANGE_REPLY_ISSUE_NUMBER),
            awaiting_human=True,
            last_action_comment_id=NO_CHANGE_REPLY_WATERMARK,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
            park_reason=PARK_FETCH_FAILED,
            # No docs_checked_sha seeded -- this is the first
            # successful no-change for this issue.
        )

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="Reviewed; no change.\nDOCS: NO_CHANGE",
            ),
            push_branch=True,
            head_shas=[SHA_PR_HEAD, SHA_PR_HEAD],
            branch_ahead_behind=(0, 0),
        )

        # NO_CHANGE outcome on a remote-clean branch -- advance
        # without push and record the SHA the dev verified.
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertIn(
            (NO_CHANGE_REPLY_ISSUE_NUMBER, IN_REVIEW),
            gh.label_history,
        )
        state = gh.pinned_data(NO_CHANGE_REPLY_ISSUE_NUMBER)
        self.assertEqual(state.get(DOCS_VERDICT), VERDICT_NO_CHANGE)
        self.assertEqual(state.get(DOCS_CHECKED_SHA), SHA_PR_HEAD)
