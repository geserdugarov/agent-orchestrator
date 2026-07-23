# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for in_review feedback watermark handling: the park-message
watermark bump and the split issue / inline-review id namespaces."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone


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
    _agent,
)

PARK_ISSUE = 60
PARK_PR = 70
PARK_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-60"
HANDOFF_WATERMARK = 900
SPLIT_WATERMARK_ISSUE = 65
SPLIT_WATERMARK_PR = 95
SPLIT_WATERMARK_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-65"
INLINE_COMMENT_ID = 42
INLINE_COMMENT_WATERMARK = 41


class InReviewParkWatermarkTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A park inside `_handle_in_review` posts an issue comment. The watermark
    must be bumped past that comment so the next tick does not see the
    orchestrator's own HITL park message as fresh PR feedback and route
    the issue to `fixing` against it.
    """

    def assert_initial_park(self, github, issue) -> int:
        state = github.pinned_data(PARK_ISSUE)
        self.assertTrue(state.get("awaiting_human"))
        self.assertEqual(state.get("park_reason"), "unmergeable")
        comments_after_park = len(github.posted_comments)
        self.assertGreater(comments_after_park, 0)
        self.assertEqual(
            state.get("pr_last_comment_id"),
            github.latest_comment_id(issue),
        )
        return comments_after_park

    def test_park_does_not_replay_next_tick(self) -> None:
        # An unmergeable PR parks awaiting human on the first tick. The
        # park message is recorded as orchestrator-authored and the
        # watermark is bumped past it; subsequent ticks must not surface
        # the park message as fresh PR feedback.
        gh = FakeGitHubClient()
        issue = make_issue(PARK_ISSUE, label="in_review")
        gh.add_issue(issue)
        gh.add_pr(
            FakePR(
                number=PARK_PR,
                head_branch=PARK_BRANCH,
                head=FakePRRef(sha="cafe1234"),
                approved=True,
                approval_head_sha="cafe1234",
                mergeable=False,
                check_state="success",
            ),
        )
        gh.seed_state(
            PARK_ISSUE,
            pr_number=PARK_PR,
            branch=PARK_BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_last_comment_id=HANDOFF_WATERMARK,  # an old watermark from validating handoff
        )

        # Tick 1: unmergeable park.
        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )
        comments_after_park = self.assert_initial_park(gh, issue)

        # Tick 2: nothing new; must NOT route the orchestrator's park
        # message back through the fixing route.
        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )
        mocks["run_agent"].assert_not_called()
        # No additional comments posted (no second park, no fixing route).
        self.assertEqual(len(gh.posted_comments), comments_after_park)
        self.assertNotIn((PARK_ISSUE, "fixing"), gh.label_history)


class _SplitWatermarkFixtureMixin(_PatchedWorkflowMixin):
    def _setup(self, *, issue_comments=(), review_comments=(), state_extra=None):
        gh = FakeGitHubClient()
        issue = make_issue(SPLIT_WATERMARK_ISSUE, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=SPLIT_WATERMARK_PR,
            head_branch=SPLIT_WATERMARK_BRANCH,
            head=FakePRRef(sha="cafe1234"),
            issue_comments=list(issue_comments),
            review_comments=list(review_comments),
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=SPLIT_WATERMARK_PR,
            branch=SPLIT_WATERMARK_BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
        )
        if state_extra:
            state.update(state_extra)
        gh.seed_state(SPLIT_WATERMARK_ISSUE, **state)
        return gh, issue, pr


class InReviewSplitWatermarkTest(
    unittest.TestCase,
    _SplitWatermarkFixtureMixin,
):
    """Track issue and inline-review ids in independent namespaces."""

    def test_inline_review_comment_routes_to_fixing(self) -> None:
        gh, issue = self._setup(
            review_comments=[
                FakeComment(
                    id=INLINE_COMMENT_ID,
                    body="line 12: rename foo to bar",
                    user=FakeUser("alice"),
                    created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                ),
            ],
            # Inline-review watermark just below the comment id so it
            # surfaces as fresh feedback. An unset watermark would trip the
            # legacy in_review migration and treat id=42 as already-consumed.
            state_extra={"pr_last_review_comment_id": INLINE_COMMENT_WATERMARK},
        )[:2]

        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertIn((SPLIT_WATERMARK_ISSUE, "fixing"), gh.label_history)
        self.assertNotIn((SPLIT_WATERMARK_ISSUE, "validating"), gh.label_history)
        state = gh.pinned_data(SPLIT_WATERMARK_ISSUE)
        # Bookmark recorded but the inline-review watermark stays where it
        # was -- the fixing handler needs the triggering comment.
        self.assertEqual(state.get("pending_fix_review_max_id"), INLINE_COMMENT_ID)
        self.assertEqual(state.get("pr_last_review_comment_id"), INLINE_COMMENT_WATERMARK)

    def test_cross_space_id_overlap_keeps_comments(self) -> None:
        # Inline review comment id (5) is LOWER than the issue-comment
        # watermark (1000). With one merged-id watermark this comment would
        # be silently filtered out; with split watermarks it gets through
        # and triggers the route to `fixing`.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup(
            review_comments=[
                FakeComment(
                    id=5,
                    body="please add a docstring",
                    user=FakeUser("alice"),
                    created_at=long_ago,
                ),
            ],
            # Issue-side watermark high (1000), inline-review watermark low (4)
            # -- the two ratchet independently, and id=5 must still surface.
            state_extra={
                "pr_last_comment_id": 1000,
                "pr_last_review_comment_id": 4,
            },
        )

        mocks = self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )

        # The inline comment surfaces and routes to fixing even though
        # id=5 < pr_last_comment_id=1000.
        mocks["run_agent"].assert_not_called()
        self.assertIn((SPLIT_WATERMARK_ISSUE, "fixing"), gh.label_history)
        self.assertEqual(gh.pinned_data(SPLIT_WATERMARK_ISSUE).get("pending_fix_review_max_id"), 5)
