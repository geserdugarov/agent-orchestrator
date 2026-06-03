# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for in_review feedback watermark handling: the park-message watermark bump and the split issue / inline-review id namespaces."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class InReviewParkWatermarkTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A park inside `_handle_in_review` posts an issue comment. The watermark
    must be bumped past that comment so the next tick does not see the
    orchestrator's own HITL park message as fresh PR feedback and route
    the issue to `fixing` against it.
    """

    def test_unmergeable_park_does_not_replay_on_next_tick(self) -> None:
        # An unmergeable PR parks awaiting human on the first tick. The
        # park message is recorded as orchestrator-authored and the
        # watermark is bumped past it; subsequent ticks must not surface
        # the park message as fresh PR feedback.
        gh = FakeGitHubClient()
        issue = make_issue(60, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=70, head_branch="orchestrator/issue-60",
            head=FakePRRef(sha="cafe1234"),
            approved=True, approval_head_sha="cafe1234",
            mergeable=False, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            60, pr_number=70, branch="orchestrator/issue-60",
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=900,  # an old watermark from validating handoff
        )

        # Tick 1: unmergeable park.
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        self.assertTrue(gh.pinned_data(60).get("awaiting_human"))
        self.assertEqual(gh.pinned_data(60).get("park_reason"), "unmergeable")
        comments_after_park = len(gh.posted_comments)
        self.assertGreater(comments_after_park, 0)
        # Watermark must have been bumped past the park comment -- which
        # means it's at or above the latest comment id on the issue.
        latest_id = gh.latest_comment_id(issue)
        self.assertEqual(gh.pinned_data(60).get("pr_last_comment_id"), latest_id)

        # Tick 2: nothing new; must NOT route the orchestrator's park
        # message back through the fixing route.
        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        mocks["run_agent"].assert_not_called()
        # No additional comments posted (no second park, no fixing route).
        self.assertEqual(len(gh.posted_comments), comments_after_park)
        self.assertNotIn((60, "fixing"), gh.label_history)


class InReviewSplitWatermarkTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Issue comments and PR inline review comments live in different id
    namespaces in GitHub's REST API. The handler tracks them with two
    independent watermarks so a high id on one side cannot eclipse newer
    comments on the other.
    """

    BRANCH = "orchestrator/issue-65"
    PR_NUMBER = 95

    def _setup(self, *, issue_comments=(), review_comments=(), state_extra=None):
        gh = FakeGitHubClient()
        issue = make_issue(65, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            issue_comments=list(issue_comments),
            review_comments=list(review_comments),
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
        )
        if state_extra:
            state.update(state_extra)
        gh.seed_state(65, **state)
        return gh, issue, pr

    def test_inline_review_comment_routes_to_fixing(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup(
            review_comments=[
                FakeComment(
                    id=42, body="line 12: rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
            # Inline-review watermark just below the comment id so it
            # surfaces as fresh feedback. An unset watermark would trip the
            # legacy in_review migration and treat id=42 as already-consumed.
            state_extra={"pr_last_review_comment_id": 41},
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertIn((65, "fixing"), gh.label_history)
        self.assertNotIn((65, "validating"), gh.label_history)
        data = gh.pinned_data(65)
        # Bookmark recorded but the inline-review watermark stays where it
        # was -- the fixing handler needs the triggering comment.
        self.assertEqual(data.get("pending_fix_review_max_id"), 42)
        self.assertEqual(data.get("pr_last_review_comment_id"), 41)

    def test_id_overlap_across_spaces_does_not_drop_comments(self) -> None:
        # Inline review comment id (5) is LOWER than the issue-comment
        # watermark (1000). With one merged-id watermark this comment would
        # be silently filtered out; with split watermarks it gets through
        # and triggers the route to `fixing`.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup(
            review_comments=[
                FakeComment(
                    id=5, body="please add a docstring",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
            # Issue-side watermark high (1000), inline-review watermark low (4)
            # -- the two ratchet independently, and id=5 must still surface.
            state_extra={
                "pr_last_comment_id": 1000,
                "pr_last_review_comment_id": 4,
            },
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # The inline comment surfaces and routes to fixing even though
        # id=5 < pr_last_comment_id=1000.
        mocks["run_agent"].assert_not_called()
        self.assertIn((65, "fixing"), gh.label_history)
        self.assertEqual(gh.pinned_data(65).get("pending_fix_review_max_id"), 5)
