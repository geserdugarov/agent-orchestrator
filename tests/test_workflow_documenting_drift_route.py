# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest

from tests.documenting_assertion_test_support import _issue_comment_text
from tests.documenting_drift_test_support import (
    _assert_reconcile_calls,
    _run_drift_reconcile,
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


class HandleDocumentingDriftRouteTest(unittest.TestCase, _DocumentingDriftFixture):
    """A user-content drift mid-final-docs-hop posts a notice and
    relabels back to `validating` for re-review -- no docs spawn,
    no push."""

    def test_body_edit_routes_to_validating_no_spawn(self) -> None:
        # A body edit during the final-docs hop must reset
        # `review_round=0`, post the notice, and relabel to
        # `validating` so the reviewer re-evaluates on the next tick.
        # No docs agent runs.
        gh, issue = self._seeded(
            awaiting_human=True,
            park_reason=PARK_AGENT_QUESTION,
        )
        issue.body = "updated body with new docs requirements"

        mocks = self._run_documenting(
            gh,
            issue,
            run_agent=_agent(),
            push_branch=True,
            head_shas=[],
            branch_ahead_behind=(0, 0),
        )

        # The drift case routes WITHOUT spawning the docs agent or
        # pushing -- a docs commit would just need to be re-reviewed
        # alongside any impl change.
        mocks[RUN_AGENT].assert_not_called()
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertIn((self.issue_number, VALIDATING), gh.label_history)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertIn(
            USER_CONTENT_CHANGED,
            _issue_comment_text(gh, self.issue_number),
        )
        state = gh.pinned_data(self.issue_number)
        # Park flags cleared.
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        self.assertEqual(state.get(REVIEW_ROUND), 0)
        # Drift hash updated -- a second tick would not re-fire drift.
        self.assertNotEqual(
            state.get("user_content_hash"),
            "stale-hash-from-original-body",
        )

    def test_unparked_body_edit_routes_to_validating(self) -> None:
        # An in-flight tick (not parked) sees a body edit: same drift
        # invalidation as the parked case -- relabel to `validating`,
        # no docs spawn.
        gh, issue = self._seeded()
        issue.body = "in-flight body edit"

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
        # Hash updated; notice posted; relabel to validating.
        pinned_state = gh.pinned_data(self.issue_number)
        self.assertNotEqual(
            pinned_state.get("user_content_hash"),
            "stale-hash-from-original-body",
        )
        self.assertIn(
            USER_CONTENT_CHANGED,
            _issue_comment_text(gh, self.issue_number),
        )
        self.assertIn((self.issue_number, VALIDATING), gh.label_history)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertEqual(pinned_state.get(REVIEW_ROUND), 0)

    def test_recovered_body_edit_routes_without_push(
        self,
    ) -> None:
        # A prior final-docs tick committed docs and parked before
        # pushing; on this tick a body edit lands AND the worktree is
        # still ahead of remote (ahead=1). The recovered commit was
        # authored against the OLD body, so the handler MUST NOT push
        # it on this tick. Relabel to `validating`; the on-disk reset
        # is covered by `test_body_edit_resets_unpushed_local_docs_commit`
        # below (this test uses the default `_FAKE_WT` path that
        # doesn't exist, so the worktree-reset branch is a no-op here).
        gh, issue = self._seeded(park_reason=PARK_PUSH_FAILED)
        issue.body = UPDATED_BODY_AFTER_DOCS

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
        self.assertIn((self.issue_number, VALIDATING), gh.label_history)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        self.assertIn(
            USER_CONTENT_CHANGED,
            _issue_comment_text(gh, self.issue_number),
        )
        pinned_state = gh.pinned_data(self.issue_number)
        self.assertEqual(pinned_state.get(REVIEW_ROUND), 0)

    def test_body_edit_resets_local_docs_commit(self) -> None:
        # Regression: drift mid-final-docs-hop must discard any
        # unpushed local docs commit before relabeling to `validating`.
        # Otherwise the recovered-commit shortcut on a future
        # final-docs hop (driven by `ahead > 0` vs.
        # `<remote>/<branch>`) would push the stale commit -- authored
        # against the OLD body -- without spawning a fresh docs agent
        # against the new requirements. With `SQUASH_ON_APPROVAL=off`
        # this is particularly dangerous because the reviewer-approved
        # head is still the dev's PR head (no rewrite gap) so the
        # stale commit applies cleanly.
        gh, issue = self._seeded(park_reason=PARK_PUSH_FAILED)
        issue.body = UPDATED_BODY_AFTER_DOCS

        capture = _run_drift_reconcile(
            self,
            gh,
            issue,
            probe_stdout="0\t1\n",
        )

        # No docs agent ran; no push happened. Routed to validating.
        capture.mocks[RUN_AGENT].assert_not_called()
        capture.mocks[PUSH_BRANCH].assert_not_called()
        self.assertIn((self.issue_number, VALIDATING), gh.label_history)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)

        # Inline probe ran first, then reset, then clean.
        _assert_reconcile_calls(
            self,
            capture,
            f"{_TEST_SPEC.remote_name}/{_branch(self.issue_number)}",
        )

        # Drift fetch was attempted before the probe + reset.
        capture.mocks[AUTHED_FETCH].assert_called()

        pinned_state = gh.pinned_data(self.issue_number)
        self.assertEqual(pinned_state.get(REVIEW_ROUND), 0)

    def test_body_edit_resets_dirty_without_commit(
        self,
    ) -> None:
        # Regression: a prior docs run may have edited files without
        # committing (parked via `_on_dirty_worktree` /
        # `_on_question` / `agent_timeout`) before the body edit
        # landed. Even when the local branch is in sync with remote
        # (`ahead == 0`), those uncommitted edits are docs work
        # against the OLD body and must be discarded before relabel.
        # The drift block must trigger `reset --hard` + `clean -fd`
        # on the dirty-only path.
        gh, issue = self._seeded(park_reason=PARK_DIRTY)
        issue.body = "updated body wants different docs"

        capture = _run_drift_reconcile(
            self,
            gh,
            issue,
            probe_stdout="0\t0\n",
            # Stale modified-tracked AND untracked paths from
            # the prior dirty park.
            dirty_files=(README, "docs/new-section.md"),
        )

        # The dirty list was non-empty, so reset + clean fired even
        # though ahead == 0.
        _assert_reconcile_calls(
            self,
            capture,
            f"{_TEST_SPEC.remote_name}/{_branch(self.issue_number)}",
        )

        # Issue relabeled to validating, no agent run, no push.
        self.assertIn((self.issue_number, VALIDATING), gh.label_history)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        capture.mocks[RUN_AGENT].assert_not_called()
        capture.mocks[PUSH_BRANCH].assert_not_called()
        state = gh.pinned_data(self.issue_number)
        self.assertEqual(state.get(REVIEW_ROUND), 0)

    def test_remote_advance_body_edit_resets_local(
        self,
    ) -> None:
        # Regression: if the remote PR head advanced past local HEAD
        # while documenting was in flight (`behind > 0`) and then a
        # body edit fires drift, the handler must reset the worktree
        # to the freshly-fetched `<remote>/<branch>`. Without this,
        # the next reviewer round would `git diff` against the un-
        # fetched local HEAD and silently miss commits the remote
        # already has, breaking the "reviewer re-evaluates the
        # updated body against the current branch" contract.
        gh, issue = self._seeded()
        issue.body = "updated body with new docs requirements"

        # ahead=0, behind=2 ("remote moved past local").
        capture = _run_drift_reconcile(
            self,
            gh,
            issue,
            probe_stdout="2\t0\n",
        )

        # Probe + reset + clean all fired -- the behind>0 case must
        # trigger the same reconcile shape as ahead>0 / dirty.
        _assert_reconcile_calls(
            self,
            capture,
            f"{_TEST_SPEC.remote_name}/{_branch(self.issue_number)}",
        )

        # Relabeled to validating; no agent / push.
        self.assertIn((self.issue_number, VALIDATING), gh.label_history)
        self.assertNotIn((self.issue_number, IN_REVIEW), gh.label_history)
        capture.mocks[RUN_AGENT].assert_not_called()
        capture.mocks[PUSH_BRANCH].assert_not_called()
        state = gh.pinned_data(self.issue_number)
        self.assertEqual(state.get(REVIEW_ROUND), 0)
