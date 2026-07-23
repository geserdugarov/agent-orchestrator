# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tests.documenting_drift_recovery_test_support import (
    _assert_pending_state,
    _assert_reset_failure_park,
    _assert_silent_pending,
    _run_drift_failure,
    _seed_pending_unwind,
)
from tests.documenting_drift_test_support import (
    _run_with_git,
)


# --- Workflow labels this stage routes between --------------------------
from tests.documenting_scenario_test_support import (
    _DocumentingDriftFixture,
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


class HandleDocumentingDriftRecoveryTest(unittest.TestCase, _DocumentingDriftFixture):
    def test_body_edit_parks_on_clean_failure(self) -> None:
        # Regression: `git clean -fd` is the final step of the drift
        # reconcile (after `reset --hard`) and removes untracked
        # files / directories that `reset --hard` does not touch. If
        # it fails, untracked docs edits authored against the OLD
        # body remain on disk; the next reviewer or docs run could
        # see them. Park with `worktree_reset_failed` rather than
        # relabeling.
        gh, issue = self._seeded(park_reason=PARK_PUSH_FAILED)
        issue.body = UPDATED_BODY_AFTER_DOCS

        capture = _run_drift_failure(
            self,
            gh,
            issue,
            GIT_CLEAN,
        )

        # All three calls fired: probe, reset, clean (which failed).
        self.assertEqual(capture.git_hardened.call_count, 3)
        self.assertEqual(
            capture.git_hardened.call_args_list[-1].args,
            (GIT_CLEAN, GIT_CLEAN_FLAGS),
        )
        _assert_reset_failure_park(self, capture, gh)

    def test_body_edit_parks_on_ahead_probe_error(self) -> None:
        # Regression: `_branch_ahead_behind` swallows git errors as
        # `(0, 0)` ("in sync"), which would let a stale local docs
        # commit silently survive into the next final-docs hop's
        # recovered-commit shortcut. The drift block now probes
        # inline and parks with `worktree_reset_failed` when the
        # probe cannot be confirmed.
        gh, issue = self._seeded(park_reason=PARK_PUSH_FAILED)
        issue.body = UPDATED_BODY_AFTER_DOCS

        capture = _run_drift_failure(
            self,
            gh,
            issue,
            GIT_REV_LIST,
        )

        # Only the probe ran; no reset attempted.
        self.assertEqual(capture.git_hardened.call_count, 1)
        self.assertEqual(
            capture.git_hardened.call_args.args[0],
            GIT_REV_LIST,
        )
        _assert_reset_failure_park(self, capture, gh)

    def test_body_edit_parks_on_reset_failure(self) -> None:
        # Regression: the `git reset --hard <remote>/<branch>` is
        # rare-but-possible to fail (in-progress operation, fs
        # transient, etc.). If it fails, the stale local docs commit
        # is still on disk -- the next final-docs hop's recovered-
        # commit shortcut would push it. Park instead of relabeling.
        gh, issue = self._seeded(park_reason=PARK_PUSH_FAILED)
        issue.body = UPDATED_BODY_AFTER_DOCS

        capture = _run_drift_failure(
            self,
            gh,
            issue,
            GIT_RESET,
        )

        # Probe + reset both ran.
        self.assertEqual(capture.git_hardened.call_count, 2)
        probe_call, reset_call = capture.git_hardened.call_args_list
        self.assertEqual(probe_call.args[0], GIT_REV_LIST)
        self.assertEqual(reset_call.args[:2], (GIT_RESET, GIT_HARD_RESET))
        _assert_reset_failure_park(self, capture, gh)

    def test_operator_unpark_retries_pending_cleanup(
        self,
    ) -> None:
        # Regression for the operator-unpark gap: a prior tick's
        # drift unwind failed cleanup and parked, leaving the issue
        # on `documenting` with `docs_drift_unwind_pending=True`. If
        # the operator clears `awaiting_human` (manual unpark) and
        # the issue retains the marker, the next documenting tick
        # MUST retry the reconcile + relabel to `validating` -- not
        # fall through to the normal docs-spawn / recovered-commit
        # path, which would advance to `in_review` and skip the
        # required reviewer re-review of the edited body.
        gh, issue = _seed_pending_unwind(self, parked=False)
        capture = _run_with_git(
            self,
            gh,
            issue,
            MagicMock(
                side_effect=[
                    MagicMock(returncode=0, stdout="0\t0\n", stderr=""),
                ],
            ),
        )

        # The retry path ran: probe fired, no reset needed (ahead=0,
        # behind=0, no dirty), relabeled to validating.
        capture.mocks[AUTHED_FETCH].assert_called()
        self.assertEqual(capture.git_hardened.call_count, 1)
        self.assertEqual(
            capture.git_hardened.call_args.args[0],
            GIT_REV_LIST,
        )
        # No agent run; no push.
        capture.mocks[RUN_AGENT].assert_not_called()
        capture.mocks[PUSH_BRANCH].assert_not_called()
        # Relabeled to validating; marker cleared.
        self.assertIn((self.issue_number, VALIDATING), gh.label_history)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        state = gh.pinned_data(self.issue_number)
        self.assertFalse(state.get(DRIFT_UNWIND_PENDING))

    def test_parked_pending_unwind_is_silent(
        self,
    ) -> None:
        # The drift-unwind retry MUST NOT fire on every tick while
        # the issue is parked with no new human input: that would
        # re-post the same park comment every tick and spam the
        # operator. The retry only re-engages when something has
        # changed (operator unpark OR fresh human comment).
        gh, issue = _seed_pending_unwind(self, parked=True)
        capture = _run_with_git(
            self,
            gh,
            issue,
            MagicMock(),
        )

        # Silent: no fetch, no reset, no posted comments, no relabel.
        _assert_silent_pending(self, capture, gh)
        _assert_pending_state(self, gh)

    def test_recovered_body_edit_parks_on_fetch_error(
        self,
    ) -> None:
        # Regression: when the drift fetch fails AND the worktree
        # exists on disk, the handler cannot safely confirm whether
        # the local branch is ahead of remote. Park awaiting human
        # with `fetch_failed` rather than relabeling to `validating`
        # -- a stale local commit silently riding into the next
        # approval is worse than a park the operator can resolve.
        gh, issue = self._seeded(park_reason=PARK_PUSH_FAILED)
        issue.body = UPDATED_BODY_AFTER_DOCS

        capture = _run_with_git(
            self,
            gh,
            issue,
            MagicMock(),
            branch_ahead_behind=(1, 0),
            authed_fetch_result=MagicMock(
                returncode=1,
                stdout="",
                stderr="fetch boom",
            ),
        )

        # No relabel; parked.
        self.assertNotIn((self.issue_number, VALIDATING), gh.label_history)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        # No reset was attempted because the fetch failed.
        capture.git_hardened.assert_not_called()
        # No push, no agent.
        capture.mocks[RUN_AGENT].assert_not_called()
        capture.mocks[PUSH_BRANCH].assert_not_called()
        state = gh.pinned_data(self.issue_number)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), PARK_FETCH_FAILED)
        self.assertEqual(state.get(REVIEW_ROUND), 0)
        # Drift-unwind sentinel persists across the park.
        self.assertTrue(state.get(DRIFT_UNWIND_PENDING))
