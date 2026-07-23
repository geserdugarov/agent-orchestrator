# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow drift comment-watermark tests."""
from __future__ import annotations

import unittest

from orchestrator import workflow

from tests import workflow_drift_test_support as support


class DriftMarksCommentsConsumedTest(
    unittest.TestCase, support._PatchedWorkflowMixin,
):
    """Reviewer point 1: the drift paths feed the dev session the full
    issue thread via `_recent_comments_text`, so `last_action_comment_id`
    must advance past every visible comment. Otherwise the next
    validating->in_review handoff's `_seed_watermark_past_self` stops at
    the same human comment and replays it as fresh PR feedback,
    triggering a duplicate dev resume."""

    def test_validating_bumps_past_human_comment(
        self,
    ) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._VALIDATING_WATERMARK_ISSUE_NUMBER,
            label=support.LABEL_VALIDATING,
            body=support.NEW_BODY,
        )
        # Pre-existing human comment with a high id -- representing the
        # comment that arrived at the same time as the body edit.
        human = support.FakeComment(
            id=support._VALIDATING_WATERMARK_COMMENT_ID,
            body="add this acceptance criterion",
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._VALIDATING_WATERMARK_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-900",
        )
        gh.add_pr(pr)
        gh.seed_state(
            support._VALIDATING_WATERMARK_ISSUE_NUMBER,
            pr_number=pr.number,
            dev_agent=support.BACKEND_CLAUDE,
            dev_session_id=support.DEV_SESSION,
            user_content_hash=support.STALE_HASH,
            review_round=1,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-900",
            last_action_comment_id=100,
        )

        self._run(
            lambda: workflow._handle_validating(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(
                session_id=support.DEV_SESSION, last_message="fixed"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", support.SHA_AFTER],
        )

        state = gh.pinned_data(support._VALIDATING_WATERMARK_ISSUE_NUMBER)
        # last_action_comment_id advanced past the human comment so the
        # eventual handoff to in_review does not classify it as fresh
        # feedback.
        self.assertGreaterEqual(
            int(state.get(support.KEY_LAST_ACTION_COMMENT_ID)),
            support._VALIDATING_WATERMARK_COMMENT_ID,
        )

    def test_in_review_human_comment_routes_to_fixing(
        self,
    ) -> None:
        # Regression for the reviewer's bug: a fresh issue-thread human
        # comment used to trip `user_content_hash` (which covers comments
        # too) and the drift path would resume the dev + bounce to
        # `validating` instead of the contracted route to `fixing`. With
        # the in_review handler scanning fresh feedback BEFORE the drift
        # check, the issue-thread comment now routes to `fixing` and the
        # hash is recomputed so the drift path does not double-fire on the
        # same comment changes next tick.
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._IN_REVIEW_WATERMARK_ISSUE_NUMBER,
            label=support.LABEL_IN_REVIEW,
            body=support.NEW_BODY,
        )
        issue.comments.append(
            support.FakeComment(
                id=support._IN_REVIEW_WATERMARK_COMMENT_ID,
                body="please also handle X",
                user=support.FakeUser(support.TRUSTED_AUTHOR),
            ),
        )
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._IN_REVIEW_WATERMARK_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-910",
        )
        gh.add_pr(pr)
        gh.seed_state(
            support._IN_REVIEW_WATERMARK_ISSUE_NUMBER,
            pr_number=pr.number,
            dev_agent=support.BACKEND_CLAUDE,
            dev_session_id=support.DEV_SESSION,
            user_content_hash=support.STALE_HASH,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-910",
            last_action_comment_id=100,
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(),
        )

        # No dev spawn, no bounce to `validating`: the fixing route owns
        # this signal.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(
            (
                (
                    support._IN_REVIEW_WATERMARK_ISSUE_NUMBER,
                    "fixing",
                ) in gh.label_history,
                (
                    support._IN_REVIEW_WATERMARK_ISSUE_NUMBER,
                    support.LABEL_VALIDATING,
                ) in gh.label_history,
            ),
            (True, False),
        )
        state = gh.pinned_data(support._IN_REVIEW_WATERMARK_ISSUE_NUMBER)
        # The triggering comment is bookmarked for the fixing handler.
        self.assertEqual(
            state.get("pending_fix_issue_max_id"),
            support._IN_REVIEW_WATERMARK_COMMENT_ID,
        )
        # Hash is updated so the drift check does not re-fire on the
        # same comment change after the fixing handler (or an operator
        # relabel) bounces the issue back to `in_review`.
        self.assertNotEqual(state.get(support.KEY_USER_CONTENT_HASH), support.STALE_HASH)
        # Watermark is deliberately left at the route-time value so the
        # fixing handler can read the triggering comment to build its
        # dev-resume prompt (the bookmark above tells it where to start).
        # The fixing handler advances this watermark itself once the
        # consumed feedback has been fed to the dev.
        self.assertEqual(state.get("pr_last_comment_id"), 0)

    def test_implementing_bumps_past_comment(
        self,
    ) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._IMPLEMENTING_WATERMARK_ISSUE_NUMBER,
            label=support.LABEL_IMPLEMENTING,
            body=support.NEW_BODY,
        )
        human = support.FakeComment(
            id=support._IMPLEMENTING_WATERMARK_COMMENT_ID,
            body="here are more requirements",
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        gh.seed_state(
            support._IMPLEMENTING_WATERMARK_ISSUE_NUMBER,
            dev_agent=support.BACKEND_CLAUDE,
            dev_session_id=support.DEV_SESSION,
            user_content_hash=support.STALE_HASH,
            awaiting_human=True,
            last_action_comment_id=100,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-920",
        )

        self._run(
            lambda: workflow._handle_implementing(gh, support._TEST_SPEC, issue),
            run_agent=support._agent(
                session_id=support.DEV_SESSION, last_message="implemented"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before-resume", "after-resume"],
        )

        state = gh.pinned_data(support._IMPLEMENTING_WATERMARK_ISSUE_NUMBER)
        # The dev's commit goes through `_on_commits` which flips to
        # validating; the validating->in_review handoff later reads
        # last_action_comment_id, so we must have bumped past 7000.
        self.assertGreaterEqual(
            int(state.get(support.KEY_LAST_ACTION_COMMENT_ID)),
            support._IMPLEMENTING_WATERMARK_COMMENT_ID,
        )

    def test_conflict_drift_bumps_last_action(self) -> None:
        gh = support.FakeGitHubClient()
        issue = support.make_issue(
            support._CONFLICT_WATERMARK_ISSUE_NUMBER,
            label=support.LABEL_RESOLVING_CONFLICT,
            body=support.NEW_BODY,
        )
        human = support.FakeComment(
            id=support._CONFLICT_WATERMARK_COMMENT_ID,
            body="more context",
            user=support.FakeUser(support.TRUSTED_AUTHOR),
        )
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = support.FakePR(
            number=support._CONFLICT_WATERMARK_PR_NUMBER,
            head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-930",
        )
        gh.add_pr(pr)
        gh.seed_state(
            support._CONFLICT_WATERMARK_ISSUE_NUMBER,
            pr_number=pr.number,
            dev_agent=support.BACKEND_CLAUDE,
            dev_session_id=support.DEV_SESSION,
            user_content_hash=support.STALE_HASH,
            conflict_round=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-930",
            last_action_comment_id=100,
        )

        self._run(
            lambda: workflow._handle_resolving_conflict(
                gh, support._TEST_SPEC, issue,
            ),
            run_agent=support._agent(
                session_id=support.DEV_SESSION, last_message="resolved"
            ),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            head_shas=["before", support.SHA_AFTER, support.SHA_AFTER],
        )

        state = gh.pinned_data(support._CONFLICT_WATERMARK_ISSUE_NUMBER)
        # After the pushed resolution flips to validating, the
        # subsequent handoff back to in_review must not replay the human
        # comment that arrived during conflict resolution.
        self.assertGreaterEqual(
            int(state.get(support.KEY_LAST_ACTION_COMMENT_ID)),
            support._CONFLICT_WATERMARK_COMMENT_ID,
        )
