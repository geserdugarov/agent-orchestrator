# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
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
    FakeLabel,
    FakePR,
    FakePRRef,
    FakePRReview,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class ValidatingHandoffPreservesHumanFeedbackTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human review comment posted while validating is still running must
    not be silently consumed when the validating handler approves and seeds
    the in_review watermarks. Otherwise the dev would never see the
    human's feedback before in_review pings HITL for the manual merge.
    """

    PR_NUMBER = 22
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-15"

    def _setup(self):
        gh = FakeGitHubClient()
        issue = make_issue(15, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #22",
                user=FakeUser("orchestrator"),
            ),
        ])
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            # Human posted a review comment during validating, BEFORE the
            # orchestrator's approval comment lands. Without the watermark
            # fix, the validating handler would seed pr_last_comment_id past
            # this comment and the next in_review tick would never see it.
            issue_comments=[
                FakeComment(
                    id=950, body="please add a docstring",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            15, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr

    def test_pre_handoff_human_pr_comment_is_processed_in_in_review(self) -> None:
        gh, issue, pr = self._setup()

        # Step 1: validating approves. The orchestrator's approval comment
        # lands AFTER the human's. With the fix, the watermark stops at
        # the first human comment instead of swallowing it.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        # Validating's approval flips through `documenting` first (the
        # final-docs hop); the watermark must already be seeded past the
        # human's pre-handoff PR comment by the time the docs pass runs.
        self.assertIn((15, "documenting"), gh.label_history)
        wm = gh.pinned_data(15).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 950,
            f"watermark must stop before human comment id=950 (got {wm})",
        )

        # Step 2: in_review tick. The human comment is visible past the
        # watermark and the handler routes the issue to `fixing` (no dev
        # spawn here; the fixing handler drives the resume). Without the
        # surfacing, the handler would ping HITL for the manual merge
        # over the human's unaddressed feedback.
        from tests.fakes import FakeLabel
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # No merge happened; issue routed to `fixing` so the human's
        # feedback is owned by the fix loop.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((15, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(15).get("pending_fix_issue_max_id"), 950,
        )


class PrePickupChatterHandoffTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Pre-pickup human comments on the issue (the original discussion that
    landed in the dev agent's spawn context) must be advanced past at
    validating -> in_review handoff. If the watermark stops at the first
    non-self comment, those same already-consumed comments replay as fresh
    PR feedback once the in_review debounce expires -- a ready-for-merge
    candidate would instead bounce back through validating in a loop.
    """

    PR_NUMBER = 25
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-20"

    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(20, label="validating", comments=[
            FakeComment(
                id=850,
                body="original issue clarification posted before pickup",
                user=FakeUser("alice"),
                created_at=long_ago,
            ),
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #25",
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
            20, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr, long_ago

    def test_pre_pickup_chatter_does_not_replay_at_in_review(self) -> None:
        gh, issue, pr, long_ago = self._setup()

        # Step 1: validating approves. Watermark must include id 850 so the
        # pre-pickup human comment is treated as consumed.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("cafe1234",),
        )
        wm = gh.pinned_data(20).get("pr_last_comment_id")
        self.assertIsNotNone(wm, "watermark must be seeded past pre-pickup")
        self.assertGreaterEqual(
            wm, 901,
            f"watermark must advance past pre-pickup chatter and self-run; "
            f"got {wm}",
        )

        # Backdate the approval comment too so debounce wouldn't filter it
        # out as a confound (it shouldn't matter because the watermark
        # already covers it, but be explicit).
        for c in list(pr.issue_comments):
            if c.created_at is None:
                c.created_at = long_ago

        # Step 2: in_review tick. With the fix, no comment is past the
        # watermark, so the handler reaches the mergeable / HITL-ping
        # path. Without the fix, the human comment id=850 surfaces as
        # "new" and the issue routes to `fixing`.
        pr.approved = True
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # Manual-merge-only: no orchestrator merge, but the HITL ping
        # fires because the watermark fix kept the pre-pickup chatter
        # out of `new_comments`.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((20, "done"), gh.label_history)
        self.assertNotIn((20, "fixing"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)


class ValidatingHandoffSeedsAllWatermarksTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The validating -> in_review handoff has to seed every comment-surface
    watermark. The orchestrator never posts inline review comments or PR
    review summaries, so `_seed_watermark_past_self` returns None for those
    surfaces; without an explicit default seed, the in_review legacy
    migration would advance past human feedback submitted on those surfaces
    during validate (the COMMENTED PR review summary case is the worst:
    `pr_has_changes_requested` does not veto the HITL ping, so the manual
    merge could land the PR over the human's note without surfacing it to
    the dev).
    """

    PR_NUMBER = 600
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-200"

    def _setup(self, *, reviews=(), review_comments=()):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(200, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #600",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            review_comments=list(review_comments),
            reviews=list(reviews),
        )
        gh.add_pr(pr)
        gh.seed_state(
            200, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr, long_ago

    def test_pre_handoff_review_summary_surfaces_in_in_review(self) -> None:
        # A "Comment" review without `CHANGES_REQUESTED` is the dangerous
        # case: it doesn't trip `pr_has_changes_requested` so the HITL
        # ping would happily advertise the PR as ready if the in_review
        # tick advanced its watermark past the body.
        long_ago_review = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4242, body="please tighten the docstring",
            state="COMMENTED",
            user=FakeUser("alice"),
            submitted_at=long_ago_review,
            commit_id="cafe1234",
        )
        gh, issue, pr, _ = self._setup(reviews=[review])

        # Step 1: validating approves. Handoff must seed
        # pr_last_review_summary_id so the legacy in_review migration cannot
        # accidentally advance past the human review.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        data = gh.pinned_data(200)
        self.assertIn("pr_last_review_summary_id", data)
        # Seeded to 0 (or any value below the review id) -- not None and not
        # past the review.
        self.assertLess(data["pr_last_review_summary_id"], 4242)

        # Step 2: in_review tick. The summary surfaces and the handler
        # routes the issue to `fixing` (the fixing handler owns the dev
        # resume cycle, not the in_review handler).
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((200, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(200).get("pending_fix_review_summary_max_id"),
            4242,
        )

    def test_pre_handoff_inline_review_comment_surfaces(self) -> None:
        # Same shape, inline-review surface. The orchestrator never posts
        # there either, so handoff has to seed pr_last_review_comment_id
        # explicitly.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr, _ = self._setup(
            review_comments=[
                FakeComment(
                    id=77, body="line 4: rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        data = gh.pinned_data(200)
        self.assertIn("pr_last_review_comment_id", data)
        self.assertLess(data["pr_last_review_comment_id"], 77)

        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((200, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(200).get("pending_fix_review_max_id"), 77,
        )


class HandoffInlineIdCollisionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """orchestrator_comment_ids records IDs from the IssueComment namespace
    only. The validating handoff must NOT use that set to seed the inline
    review-comment watermark -- inline comments are PullRequestComment
    objects, with their own id space, where numeric collisions with bot
    issue/PR comment ids are possible. Otherwise a human inline comment
    whose id happens to match a recorded bot issue comment id would be
    treated as self-authored and consumed at handoff.
    """

    PR_NUMBER = 800
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-300"

    def test_inline_comment_with_bot_issue_id_survives_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(300, label="validating", comments=[
            FakeComment(
                id=4242, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            review_comments=[
                # Same numeric id as the bot's issue comment above, but a
                # different namespace (PullRequestComment). The handoff must
                # not treat this as self-authored.
                FakeComment(
                    id=4242, body="please rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            300, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[4242],
            pickup_comment_id=4242,
        )

        # Step 1: validating handoff. The inline comment must NOT bump
        # pr_last_review_comment_id past 4242.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        data = gh.pinned_data(300)
        self.assertLess(
            data.get("pr_last_review_comment_id"), 4242,
            "id collision must not advance the inline-review watermark",
        )

        # Step 2: in_review tick. The human's inline comment surfaces and
        # routes the issue to `fixing` -- no ready-for-merge ping. The
        # fixing handler owns the dev resume on the next tick.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((300, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(300).get("pending_fix_review_max_id"), 4242,
        )


class HandoffWithoutPickupIdLegacyStateTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """For an issue picked up under an older orchestrator version that did
    not record `pickup_comment_id`, the validating handoff cannot tell
    pre-pickup chatter (safe to skip) from human feedback posted during
    implementing/validating (must preserve). The seed-watermark function
    must refuse to advance past anything in that legacy state, defaulting
    pr_last_comment_id to 0; the orchestrator_comment_ids id-set filter in
    `_handle_in_review` then drops the recorded bot comments at scan time
    while leaving every human comment visible.
    """

    PR_NUMBER = 1000
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-500"

    def test_legacy_human_during_implementing_survives_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Comment id ordering models a real legacy lifecycle: pre-pickup
        # chatter, then a pickup posted by the OLD orchestrator (id 900,
        # NOT recorded in orchestrator_comment_ids), then a human "do not
        # merge yet" posted while the dev was implementing, then a
        # PR-opened comment posted by the NEW orchestrator (id 960,
        # recorded). The human comment between the two bot posts is the
        # signal that must NOT be lost.
        issue = make_issue(500, label="validating", comments=[
            FakeComment(
                id=800, body="original issue clarification",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=950, body="please do not merge yet",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=960, body=":sparkles: PR opened: #1000",
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
        # Legacy state: PR-opened (960) is the FIRST recorded bot id;
        # pickup_comment_id is missing because pickup happened under the
        # old code. Validating handoff will then see only {960} as
        # orchestrator content; the seed-watermark function must NOT
        # falsely treat ids 800/900/950 as pre-pickup chatter.
        gh.seed_state(
            500, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[960],
        )

        # Step 1: validating approves. Handoff must NOT advance the
        # watermark past 950.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        wm = gh.pinned_data(500).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 950,
            f"watermark must not consume legacy human feedback at id 950 "
            f"(got {wm})",
        )

        # Step 2: in_review tick. Every gate passes -- the only thing
        # standing between the PR and a ready-ping is the human's "do
        # not merge yet" comment. The handler must surface it as fresh
        # feedback and route to `fixing`.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No merge call fires.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((500, "done"), gh.label_history)
        # The "do not merge yet" comment surfaces as fresh PR feedback
        # and routes the issue to `fixing` (alongside other legacy
        # comments the migration cannot reliably classify).
        mocks["run_agent"].assert_not_called()
        self.assertIn((500, "fixing"), gh.label_history)
        # The legacy default falls through to scan from the beginning,
        # so the route bookmarks the latest visible human/issue-side id.
        self.assertGreaterEqual(
            gh.pinned_data(500).get("pending_fix_issue_max_id"), 950,
        )


class HandoffSkipsConsumedRepliesTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human reply consumed by `_resume_developer_on_human_reply` during
    implementing or validating must not re-surface as fresh PR feedback in
    in_review. The validating handoff watermark seed has to walk past such
    already-consumed comments; otherwise the next in_review tick re-routes
    the issue to `fixing` on the same human input the dev has already
    addressed.
    """

    PR_NUMBER = 1500
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-900"

    def test_consumed_reply_does_not_replay_after_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Lifecycle: pickup (900) -> implementing dev asks question, parks
        # at 910 -> human replies "use sqlite" at 920 -> next tick resumes
        # the dev with that comment -> dev commits, _on_commits posts
        # PR-opened at 930 -> validating reviewer approves and posts
        # approval comment at 940. The reply at 920 was already fed to
        # the dev; in_review must NOT replay it.
        issue = make_issue(900, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=910, body="@hitl agent needs your input to proceed",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=920, body="use sqlite please",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=930, body=":sparkles: PR opened: #1500",
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
        # `last_action_comment_id=920` reflects the post-resume bump --
        # the resume ate comments after the park (910) up through 920.
        gh.seed_state(
            900, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 910, 930],
            pickup_comment_id=900,
            last_action_comment_id=920,
        )

        # Step 1: validating approves. The handoff seed must walk PAST
        # comment 920 (already consumed) instead of stopping at it.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("cafe1234",),
        )
        wm = gh.pinned_data(900).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertGreaterEqual(
            wm, 930,
            f"watermark must advance past consumed reply (id 920); got {wm}",
        )

        # Step 2: in_review tick. Comment 920 must NOT surface and the
        # handler reaches the manual-merge HITL ping path.
        pr.approved = True
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # Manual-merge-only: no merge call. The HITL ping fires because
        # the seed kept the consumed reply out of `new_comments`.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((900, "done"), gh.label_history)
        self.assertNotIn((900, "fixing"), gh.label_history)
        ping_comments = [
            body for _, body in gh.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)

    def test_resume_bumps_last_action_comment_id_to_consumed_max(self) -> None:
        # Direct unit-level check on `_resume_developer_on_human_reply`:
        # after the resume runs, `last_action_comment_id` must reflect
        # the highest consumed id, not the prior park id.

        gh = FakeGitHubClient()
        issue = make_issue(901, label="implementing", comments=[
            FakeComment(id=910, body="park", user=FakeUser("orchestrator")),
            FakeComment(id=920, body="use sqlite", user=FakeUser("alice")),
            FakeComment(id=921, body="and add a test", user=FakeUser("alice")),
        ])
        gh.add_issue(issue)
        gh.seed_state(
            901, dev_agent="claude", dev_session_id="dev-sess",
            last_action_comment_id=910,
        )
        state = gh.read_pinned_state(issue)

        with patch.object(workflow, "_ensure_worktree", lambda spec, n, **_: _FAKE_WT), \
             patch.object(workflow, "run_agent", lambda *a, **kw: _agent()):
            result = workflow._resume_developer_on_human_reply(
                gh, _TEST_SPEC, issue, state
            )

        self.assertIsNotNone(result)
        self.assertEqual(
            state.get("last_action_comment_id"), 921,
            "resume must bump last_action_comment_id to max(consumed)",
        )


class HandoffConsumedThroughIssueThreadOnlyTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """`last_action_comment_id` only records issue-thread comments fed via
    `_resume_developer_on_human_reply`; PR-conversation comments are never
    consumed via that path. The validating handoff seed must NOT apply
    `consumed_through` to the PR-conversation surface, or a human PR comment
    whose id sits below a later-consumed issue-thread reply gets silently
    advanced past and the HITL ping fires over unread feedback.
    """

    PR_NUMBER = 1600
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-800"

    def test_pr_conv_comment_below_consumed_through_is_preserved(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Lifecycle: pickup (900) -> park asking question (910) -> human
        # leaves a PR-conv comment at 915 (the one that MUST surface) ->
        # human also replies on the issue thread at 920 -> resume consumes
        # the issue reply and bumps `last_action_comment_id` to 920 ->
        # PR-opened comment at 930 -> validating reviewer approves and
        # posts approval at 940. The PR-conv comment at 915 was never fed
        # to the dev (validating only watches the issue thread); without
        # the fix the seed walks past it because 915 <= consumed_through
        # (920) and the next tick pings HITL over it.
        issue = make_issue(800, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=910, body="@hitl agent needs your input to proceed",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=920, body="use sqlite please",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=930, body=":sparkles: PR opened: #1600",
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
                    id=915, body="please add a docstring to the public class",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            800, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 910, 930],
            pickup_comment_id=900,
            last_action_comment_id=920,
        )

        # Step 1: validating approves and seeds in_review watermarks. The
        # seed must stop before 915 so the next in_review tick scans the
        # PR-conv surface and finds the human comment. Approval routes
        # through `documenting` first (the final-docs hop).
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("cafe1234",),
        )
        self.assertIn((800, "documenting"), gh.label_history)
        wm = gh.pinned_data(800).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 915,
            "watermark must stop before unread PR-conv comment id=915 "
            f"(consumed_through=920 must NOT apply across surfaces); got {wm}",
        )

        # Step 2: simulate the documenting no-change exit (final docs
        # pass found nothing to commit) and run the in_review tick.
        # The PR-conv comment surfaces and the handler routes the issue
        # to `fixing` (the fixing handler owns the dev resume on the
        # next tick) instead of pinging HITL.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Routed to fixing -- the unread PR-conv text is bookmarked for
        # the fixing handler. No HITL ping fires over unread feedback.
        # `pending_fix_issue_max_id` covers BOTH the issue-thread and
        # PR-conversation surfaces (they share the IssueComment id space);
        # 915 was the unread PR-conv comment, 920 was the issue-thread
        # human reply that consumed_through skipped at handoff but
        # in_review re-scans regardless, so the max across the bucket is
        # 920. The point of the test is that 915 has to be visible to
        # the fixing handler -- it must sit at or below the bookmark and
        # past the watermark.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((800, "fixing"), gh.label_history)
        data = gh.pinned_data(800)
        self.assertGreaterEqual(data.get("pending_fix_issue_max_id"), 915)
        # The watermark stays put so the fixing handler can re-scan and
        # see id 915.
        self.assertLess(data.get("pr_last_comment_id"), 915)
