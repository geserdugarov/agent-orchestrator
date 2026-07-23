# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the legacy in_review watermark migration and the zero-watermark
fallback that keeps a legacy '0' from being displaced by a higher
last_action_comment_id."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestrator import config

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    FakePRReview,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _agent,
    _issue_branch,
)

LEGACY_ISSUE = 150
LEGACY_PR = 300
EMPTY_WATERMARK_ISSUE = 400
EMPTY_WATERMARK_PR = 900
ZERO_WATERMARK_ISSUE = 600
ZERO_WATERMARK_PR = 1100
EARLIER_COMMENT_ID = 910
PR_OPEN_COMMENT_ID = 911
LATER_COMMENT_ID = 920
INLINE_REVIEW_ID = 30
REVIEW_SUMMARY_ID = 4000
FIRST_INLINE_REVIEW_ID = 42
FIRST_REVIEW_SUMMARY_ID = 5050
REVIEW_DEBOUNCE_SECONDS = 600
BOT_LOGIN = "orchestrator"
REVIEWED_SHA = "cafe1234"
HUMAN_LOGIN = "alice"
BACKEND_CLAUDE = "claude"
DEV_SESSION = "dev-sess"
DEBOUNCE_SETTING = "IN_REVIEW_DEBOUNCE_SECONDS"
RUN_AGENT = "run_agent"


class _LegacyWatermarkFixtureMixin(_PatchedWorkflowMixin):
    def _legacy_setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Three historical orchestrator comments on the issue thread plus
        # one historical PR conversation comment (the validating handoff
        # approval) -- exactly the shape of an in-flight in_review issue
        # whose state was written before pr_last_comment_id existed.
        issue = make_issue(
            LEGACY_ISSUE,
            label="in_review",
            comments=[
                FakeComment(
                    id=EARLIER_COMMENT_ID,
                    body=":robot: orchestrator picking this up.",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=PR_OPEN_COMMENT_ID,
                    body=":sparkles: PR opened: #300",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=LEGACY_PR,
            head_branch=_issue_branch(LEGACY_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state="success",
            issue_comments=[
                FakeComment(
                    id=LATER_COMMENT_ID,
                    body=":white_check_mark: codex review approved.",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
            review_comments=[
                FakeComment(
                    id=INLINE_REVIEW_ID,
                    body="line 5: drop the trailing newline",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
                ),
            ],
            reviews=[
                FakePRReview(
                    id=REVIEW_SUMMARY_ID,
                    body="please rename foo to bar",
                    state="CHANGES_REQUESTED",
                    user=FakeUser(HUMAN_LOGIN),
                    submitted_at=long_ago,
                    commit_id=REVIEWED_SHA,
                ),
            ],
        )
        gh.add_pr(pr)
        # Legacy state: pr_number is set, but no watermarks AND no recorded
        # orchestrator_comment_ids. This is the state shape the migration
        # has to handle without replaying every historical comment.
        gh.seed_state(
            LEGACY_ISSUE,
            pr_number=LEGACY_PR,
            branch=_issue_branch(LEGACY_ISSUE),
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
        )
        return gh, issue, pr


class LegacyInReviewWatermarkSeedTest(
    unittest.TestCase,
    _LegacyWatermarkFixtureMixin,
):
    """Seed legacy watermarks without replaying historical feedback."""

    def test_first_tick_does_not_replay_history(self) -> None:
        gh, issue, pr = self._legacy_setup()

        with patch.object(
            config,
            DEBOUNCE_SETTING,
            REVIEW_DEBOUNCE_SECONDS,
        ):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        # No dev resume despite historical comments / inline review / review
        # summary all sitting visible: the migration seeded each watermark
        # past the latest visible id on its surface.
        mocks[RUN_AGENT].assert_not_called()
        self.assertNotIn((LEGACY_ISSUE, "validating"), gh.label_history)
        # Watermarks were persisted so subsequent ticks see only newer ids.
        state = gh.pinned_data(LEGACY_ISSUE)
        self.assertGreaterEqual(state.get("pr_last_comment_id"), LATER_COMMENT_ID)
        self.assertEqual(state.get("pr_last_review_comment_id"), INLINE_REVIEW_ID)
        self.assertEqual(state.get("pr_last_review_summary_id"), REVIEW_SUMMARY_ID)

    def test_first_tick_pings_for_mergeable_pr(self) -> None:
        # All gates passing: the migration must not park or otherwise
        # block the handler from posting the HITL ping -- it only treats
        # already-visible comments as consumed.
        gh, issue, pr = self._legacy_setup()
        # Drop the historical CHANGES_REQUESTED review and mark the PR
        # as approved on the current head so the ping gate passes.
        pr.reviews = []
        pr.approved = True

        with patch.object(
            config,
            DEBOUNCE_SETTING,
            REVIEW_DEBOUNCE_SECONDS,
        ):
            self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        # No merge (humans drive the merge); HITL ping fires for the
        # mergeable PR.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((LEGACY_ISSUE, "done"), gh.label_history)
        ping_comments = [body for _, body in gh.posted_comments if "ready for review/merge" in body]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(LEGACY_ISSUE).get("ready_ping_sha"),
            REVIEWED_SHA,
        )


class _EmptyWatermarkMigrationFixtureMixin(_PatchedWorkflowMixin):
    def _legacy_setup(self):
        gh = FakeGitHubClient()
        # Make 'truly legacy': no watermarks at all on any surface, no
        # comments anywhere. This is the shape the reviewer flagged --
        # snapshot-failed handoff or pre-feature in_review state with an
        # empty PR.
        issue = make_issue(EMPTY_WATERMARK_ISSUE, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=EMPTY_WATERMARK_PR,
            head_branch=_issue_branch(EMPTY_WATERMARK_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            EMPTY_WATERMARK_ISSUE,
            pr_number=EMPTY_WATERMARK_PR,
            branch=_issue_branch(EMPTY_WATERMARK_ISSUE),
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
        )
        return gh, issue, pr


class LegacyMigrationPersistsEmptyWatermarksTest(
    unittest.TestCase,
    _EmptyWatermarkMigrationFixtureMixin,
):
    """Persist zero watermarks so first feedback remains visible."""

    def test_first_inline_review_surfaces(self) -> None:
        gh, issue, pr = self._legacy_setup()

        # Tick 1: legacy migration runs, surfaces have nothing to seed past.
        # The migration must persist 0 on every namespace anyway.
        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )
        state = gh.pinned_data(EMPTY_WATERMARK_ISSUE)
        self.assertEqual(state.get("pr_last_review_comment_id"), 0)
        self.assertEqual(state.get("pr_last_review_summary_id"), 0)
        self.assertEqual(state.get("pr_last_comment_id"), 0)

        # Now a human posts the first inline review comment. With the fix,
        # the next tick sees pr_last_review_comment_id=0 (already set) and
        # surfaces id=42 instead of re-running migration past it.
        pr.review_comments.append(
            FakeComment(
                id=FIRST_INLINE_REVIEW_ID,
                body="line 7: rename foo to bar",
                user=FakeUser(HUMAN_LOGIN),
                created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            ),
        )

        with patch.object(
            config,
            DEBOUNCE_SETTING,
            REVIEW_DEBOUNCE_SECONDS,
        ):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        # The first inline review comment after migration is treated as
        # fresh feedback and routes the issue to `fixing` (no dev spawn
        # here; the fixing handler owns that step).
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((EMPTY_WATERMARK_ISSUE, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(EMPTY_WATERMARK_ISSUE).get("pending_fix_review_max_id"),
            FIRST_INLINE_REVIEW_ID,
        )

    def test_first_review_summary_surfaces(self) -> None:
        # Same shape on the review-summary surface. A COMMENTED summary
        # body must still surface through the fresh-feedback scan; without
        # the migration persisting 0, the body would be migrated past and
        # the human would never reach the dev.
        gh, issue, pr = self._legacy_setup()
        gh.seed_state(
            EMPTY_WATERMARK_ISSUE,
            pr_number=EMPTY_WATERMARK_PR,
            branch=_issue_branch(EMPTY_WATERMARK_ISSUE),
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
        )

        self._run_in_review(
            gh,
            issue,
            run_agent=_agent(),
        )
        state = gh.pinned_data(EMPTY_WATERMARK_ISSUE)
        self.assertEqual(state.get("pr_last_review_summary_id"), 0)

        pr.reviews.append(
            FakePRReview(
                id=FIRST_REVIEW_SUMMARY_ID,
                body="please tighten the spec",
                state="COMMENTED",
                user=FakeUser(HUMAN_LOGIN),
                submitted_at=datetime.now(timezone.utc) - timedelta(hours=1),
                commit_id=REVIEWED_SHA,
            ),
        )

        with patch.object(
            config,
            DEBOUNCE_SETTING,
            REVIEW_DEBOUNCE_SECONDS,
        ):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((EMPTY_WATERMARK_ISSUE, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(EMPTY_WATERMARK_ISSUE).get("pending_fix_review_summary_max_id"),
            FIRST_REVIEW_SUMMARY_ID,
        )


class ZeroWatermarkSurvivesFallbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A legacy validating handoff stores `pr_last_comment_id = 0` to mean
    "scan all from the beginning". The in_review fallback to
    `last_action_comment_id` must not discard 0 in favor of a higher prior
    park-comment id; otherwise lower-id human feedback (e.g. an implementing-
    time "do not merge yet") sits below the watermark and the in_review ->
    fixing route would silently skip it.
    """

    def test_zero_does_not_use_last_action(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # The implementing-time park comment (id 920) sits between a human
        # "do not merge yet" comment (id 910) and the validating-handoff
        # state. last_action_comment_id was set to 920 by the prior park.
        # If the in_review handler falls back to that for the watermark,
        # comment 910 is below it and gets dropped.
        issue = make_issue(
            ZERO_WATERMARK_ISSUE,
            label="in_review",
            comments=[
                FakeComment(
                    id=EARLIER_COMMENT_ID,
                    body="please do not merge yet",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=long_ago,
                ),
                FakeComment(
                    id=LATER_COMMENT_ID,
                    body=":robot: park message from a prior tick",
                    user=FakeUser(BOT_LOGIN),
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=ZERO_WATERMARK_PR,
            head_branch=_issue_branch(ZERO_WATERMARK_ISSUE),
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            ZERO_WATERMARK_ISSUE,
            pr_number=ZERO_WATERMARK_PR,
            branch=_issue_branch(ZERO_WATERMARK_ISSUE),
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            # Legacy default: 0 means "scan everything".
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # ALSO populated from the prior park; must NOT take precedence
            # over the legacy 0 watermark.
            last_action_comment_id=LATER_COMMENT_ID,
            # Park the bot's own message id so the id-set filter drops it.
            orchestrator_comment_ids=[LATER_COMMENT_ID],
        )

        with patch.object(
            config,
            DEBOUNCE_SETTING,
            REVIEW_DEBOUNCE_SECONDS,
        ):
            mocks = self._run_in_review(
                gh,
                issue,
                run_agent=_agent(),
            )

        # No merge attempt; the human's id=910 comment surfaces as fresh
        # feedback and routes the issue to `fixing` (the in_review handler
        # no longer drives the dev resume itself).
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((ZERO_WATERMARK_ISSUE, "done"), gh.label_history)
        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((ZERO_WATERMARK_ISSUE, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(ZERO_WATERMARK_ISSUE).get(
                "pending_fix_issue_max_id",
            ),
            EARLIER_COMMENT_ID,
        )
