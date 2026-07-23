# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for implementing pr reuse behavior."""

from __future__ import annotations

import unittest

from tests import implementing_pr_test_support as support

BRANCHLESS_ISSUE = support.BRANCHLESS_ISSUE
BRANCHLESS_REPLY_ID = support.BRANCHLESS_REPLY_ID
BRANCHLESS_WATERMARK = support.BRANCHLESS_WATERMARK
DEV_SESSION = support.DEV_SESSION
DONE_MESSAGE = support.DONE_MESSAGE
EXISTING_PR_NUMBER = support.EXISTING_PR_NUMBER
FakeComment = support.FakeComment
FakeGitHubClient = support.FakeGitHubClient
FakePR = support.FakePR
FakeUser = support.FakeUser
LABEL_IMPLEMENTING = support.LABEL_IMPLEMENTING
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_agent = support._agent
make_issue = support.make_issue
posted_comment_contains = support.posted_comment_contains


class OnCommitsPRReuseTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_existing_open_pr_is_reused(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(4, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        existing = FakePR(
            number=EXISTING_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-4",
        )
        gh.existing_open_pr["orchestrator/geserdugarov__agent-orchestrator/issue-4"] = existing

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        # No new PR opened, no sparkles comment posted.
        self.assertEqual(gh.opened_prs, [])
        self.assertFalse(
            posted_comment_contains(gh, ":sparkles: PR opened"),
        )
        self.assertIn((4, "validating"), gh.label_history)
        self.assertEqual(gh.pinned_data(4).get("pr_number"), EXISTING_PR_NUMBER)

    def test_legacy_branch_anchors_lookup_and_push(self) -> None:
        # Regression: an in-flight issue that was already running before
        # branches were slug-namespaced has `state["branch"]` pinned to
        # the legacy `orchestrator/issue-<n>` form and a live PR whose
        # head is that legacy ref. The orchestrator must keep using the
        # pinned branch -- otherwise the PR lookup misses, a fresh
        # slug-namespaced branch gets pushed, and a duplicate PR opens
        # against the new branch while the original PR is orphaned.
        LEGACY = "orchestrator/issue-4"
        gh = FakeGitHubClient()
        issue = make_issue(4, label=LABEL_IMPLEMENTING)
        gh.add_issue(issue)
        self._existing = FakePR(number=EXISTING_PR_NUMBER, head_branch=LEGACY)
        gh.existing_open_pr[LEGACY] = self._existing
        # Pinned state mirrors what an issue picked up before this
        # change would carry.
        gh.seed_state(4, branch=LEGACY)

        self._mocks = self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=DONE_MESSAGE),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        # PR lookup hit the legacy ref -- no duplicate PR opened, no
        # `:sparkles: PR opened` comment.
        self.assertEqual(gh.opened_prs, [])
        self.assertFalse(
            posted_comment_contains(gh, ":sparkles: PR opened"),
        )
        self.assertEqual(gh.pinned_data(4).get("pr_number"), EXISTING_PR_NUMBER)
        # Push targeted the legacy branch, not the new namespaced one.
        push_call = self._mocks["_push_branch"].call_args
        self.assertEqual(push_call.args[2], LEGACY)
        # State stays pinned to the legacy branch.
        self.assertEqual(gh.pinned_data(4).get("branch"), LEGACY)

    def test_persists_branch_for_branchless_resume(self) -> None:
        # Regression: a state that lacks `branch` going into `_on_commits`
        # (the awaiting-human resume path skips the fresh-spawn
        # `state.set("branch", ...)` block) would, before this fix, leave
        # `pr_number` persisted with `branch` absent. The next tick's
        # `_resolve_branch_name` then takes the legacy-PR fallback and
        # routes validation / base-sync / cleanup to
        # `orchestrator/issue-N` while the live PR is actually on the
        # slug-namespaced ref this push just published. `_on_commits`
        # must persist the pushed branch alongside `pr_number` so the
        # resolver recovers it directly.
        gh = FakeGitHubClient()
        issue = make_issue(BRANCHLESS_ISSUE, label=LABEL_IMPLEMENTING)
        # Pending human comment that triggers the awaiting-human resume.
        reply = FakeComment(
            id=BRANCHLESS_REPLY_ID,
            body="please retry",
            user=FakeUser("alice"),
        )
        issue.comments.append(reply)
        gh.add_issue(issue)
        # State carries `awaiting_human=True` and a dev session id but
        # NO `branch` -- the pre-existing shape for a relabel-from-
        # question or any park whose pre-spawn site never persisted
        # `branch`. `pr_number` is also absent because no PR exists
        # yet; the resume produces the first commit.
        gh.seed_state(
            BRANCHLESS_ISSUE,
            awaiting_human=True,
            dev_agent="claude",
            dev_session_id="dev-sess",
            last_action_comment_id=BRANCHLESS_WATERMARK,
        )

        self._run_implementing(
            gh,
            issue,
            run_agent=_agent(session_id="dev-sess", last_message=DONE_MESSAGE),
            # Resume path: no recovered-worktree shortcut, post-agent
            # check sees the new commit.
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        # A PR was opened and persisted to state.
        self.assertEqual(len(gh.opened_prs), 1)
        pinned_data = gh.pinned_data(BRANCHLESS_ISSUE)
        self.assertEqual(pinned_data.get("pr_number"), gh.opened_prs[0].number)
        # The branch was persisted alongside `pr_number` so the next
        # tick's `_resolve_branch_name` recovers the slug-namespaced
        # form directly instead of mis-inferring the legacy ref.
        self.assertEqual(
            pinned_data.get("branch"),
            "orchestrator/geserdugarov__agent-orchestrator/issue-11",
        )
