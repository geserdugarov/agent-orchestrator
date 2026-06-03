# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the legacy in_review watermark migration and the zero-watermark fallback that keeps a legacy '0' from being displaced by a higher last_action_comment_id."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow

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
    _TEST_SPEC,
    _agent,
)


class LegacyInReviewWatermarkSeedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An issue that reached `in_review` before validating started seeding
    watermarks (or that was manually relabeled, or whose handoff failed to
    snapshot the PR) sits on the in_review handler with all three watermarks
    unset. Without the first-tick migration, every historical comment --
    including the orchestrator's own pickup / PR-opened / approval messages
    -- would surface as fresh PR feedback once the debounce expired,
    routing the issue to `fixing` (and back to `validating` on the
    eventual pushed fix).
    """

    PR_NUMBER = 300
    BRANCH = "orchestrator/issue-150"

    def _legacy_setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Three historical orchestrator comments on the issue thread plus
        # one historical PR conversation comment (the validating handoff
        # approval) -- exactly the shape of an in-flight in_review issue
        # whose state was written before pr_last_comment_id existed.
        issue = make_issue(150, label="in_review", comments=[
            FakeComment(
                id=910, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=911, body=":sparkles: PR opened: #300",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=920,
                    body=":white_check_mark: codex review approved.",
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
            ],
            review_comments=[
                FakeComment(
                    id=30, body="line 5: drop the trailing newline",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
            reviews=[
                FakePRReview(
                    id=4000, body="please rename foo to bar",
                    state="CHANGES_REQUESTED",
                    user=FakeUser("alice"),
                    submitted_at=long_ago,
                    commit_id="cafe1234",
                ),
            ],
        )
        gh.add_pr(pr)
        # Legacy state: pr_number is set, but no watermarks AND no recorded
        # orchestrator_comment_ids. This is the state shape the migration
        # has to handle without replaying every historical comment.
        gh.seed_state(
            150, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
        )
        return gh, issue, pr

    def test_legacy_first_tick_does_not_replay_history(self) -> None:
        gh, issue, pr = self._legacy_setup()

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No dev resume despite historical comments / inline review / review
        # summary all sitting visible: the migration seeded each watermark
        # past the latest visible id on its surface.
        mocks["run_agent"].assert_not_called()
        self.assertNotIn((150, "validating"), gh.label_history)
        # Watermarks were persisted so subsequent ticks see only newer ids.
        data = gh.pinned_data(150)
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 920)
        self.assertEqual(data.get("pr_last_review_comment_id"), 30)
        self.assertEqual(data.get("pr_last_review_summary_id"), 4000)

    def test_legacy_first_tick_pings_hitl_for_mergeable_pr(self) -> None:
        # All gates passing: the migration must not park or otherwise
        # block the handler from posting the HITL ping -- it only treats
        # already-visible comments as consumed.
        gh, issue, pr = self._legacy_setup()
        # Drop the historical CHANGES_REQUESTED review and mark the PR
        # as approved on the current head so the ping gate passes.
        pr.reviews = []
        pr.approved = True

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No merge (humans drive the merge); HITL ping fires for the
        # mergeable PR.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((150, "done"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)
        self.assertEqual(
            gh.pinned_data(150).get("ready_ping_sha"), "cafe1234",
        )


class LegacyMigrationPersistsEmptyWatermarksTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The legacy in_review migration runs on every tick where any of the
    three watermarks is unset. If the surface has no content yet, the
    migration would previously leave the watermark unset and re-fire next
    tick -- the FIRST human inline / summary review added in between would
    then be consumed by the migration before _handle_in_review built
    new_comments, silently swallowing that first review and skipping the
    `fixing` route. The migration must persist 0 even on empty surfaces
    so the next tick scans new comments instead of re-migrating.
    """

    PR_NUMBER = 900
    BRANCH = "orchestrator/issue-400"

    def _legacy_setup(self):
        gh = FakeGitHubClient()
        # Make 'truly legacy': no watermarks at all on any surface, no
        # comments anywhere. This is the shape the reviewer flagged --
        # snapshot-failed handoff or pre-feature in_review state with an
        # empty PR.
        issue = make_issue(400, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            400, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
        )
        return gh, issue, pr

    def test_first_inline_review_after_migration_surfaces(self) -> None:
        gh, issue, pr = self._legacy_setup()

        # Tick 1: legacy migration runs, surfaces have nothing to seed past.
        # The migration must persist 0 on every namespace anyway.
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        data = gh.pinned_data(400)
        self.assertEqual(data.get("pr_last_review_comment_id"), 0)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)
        self.assertEqual(data.get("pr_last_comment_id"), 0)

        # Now a human posts the first inline review comment. With the fix,
        # the next tick sees pr_last_review_comment_id=0 (already set) and
        # surfaces id=42 instead of re-running migration past it.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr.review_comments.append(
            FakeComment(
                id=42, body="line 7: rename foo to bar",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # The first inline review comment after migration is treated as
        # fresh feedback and routes the issue to `fixing` (no dev spawn
        # here; the fixing handler owns that step).
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((400, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(400).get("pending_fix_review_max_id"), 42,
        )

    def test_first_review_summary_after_migration_surfaces(self) -> None:
        # Same shape on the review-summary surface. A COMMENTED summary
        # body must still surface through the fresh-feedback scan; without
        # the migration persisting 0, the body would be migrated past and
        # the human would never reach the dev.
        gh, issue, pr = self._legacy_setup()
        gh.seed_state(
            400, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
        )

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        data = gh.pinned_data(400)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)

        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr.reviews.append(
            FakePRReview(
                id=5050, body="please tighten the spec",
                state="COMMENTED",
                user=FakeUser("alice"),
                submitted_at=long_ago,
                commit_id="cafe1234",
            ),
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((400, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(400).get("pending_fix_review_summary_max_id"),
            5050,
        )


class ZeroWatermarkSurvivesFallbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A legacy validating handoff stores `pr_last_comment_id = 0` to mean
    "scan all from the beginning". The in_review fallback to
    `last_action_comment_id` must not discard 0 in favor of a higher prior
    park-comment id; otherwise lower-id human feedback (e.g. an implementing-
    time "do not merge yet") sits below the watermark and the in_review ->
    fixing route would silently skip it.
    """

    PR_NUMBER = 1100
    BRANCH = "orchestrator/issue-600"

    def test_zero_watermark_does_not_fall_back_to_last_action(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # The implementing-time park comment (id 920) sits between a human
        # "do not merge yet" comment (id 910) and the validating-handoff
        # state. last_action_comment_id was set to 920 by the prior park.
        # If the in_review handler falls back to that for the watermark,
        # comment 910 is below it and gets dropped.
        issue = make_issue(600, label="in_review", comments=[
            FakeComment(
                id=910, body="please do not merge yet",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=920, body=":robot: park message from a prior tick",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            600,
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            # Legacy default: 0 means "scan everything".
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # ALSO populated from the prior park; must NOT take precedence
            # over the legacy 0 watermark.
            last_action_comment_id=920,
            # Park the bot's own message id so the id-set filter drops it.
            orchestrator_comment_ids=[920],
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No merge attempt; the human's id=910 comment surfaces as fresh
        # feedback and routes the issue to `fixing` (the in_review handler
        # no longer drives the dev resume itself).
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((600, "done"), gh.label_history)
        mocks["run_agent"].assert_not_called()
        self.assertIn((600, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(600).get("pending_fix_issue_max_id"), 910,
        )
