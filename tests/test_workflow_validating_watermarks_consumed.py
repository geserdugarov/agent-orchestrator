# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from orchestrator import config, workflow

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    REVIEW_APPROVED_MESSAGE,
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)

CONSUMED_REPLY_ISSUE = 900
CONSUMED_REPLY_PR = 1500
CONSUMED_REPLY_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-900"
RESUME_WATERMARK_ISSUE = 901
ISSUE_THREAD_ISSUE = 800
ISSUE_THREAD_PR = 1600
ISSUE_THREAD_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-800"
PICKUP_COMMENT_ID = 900
PARK_COMMENT_ID = 910
CONSUMED_REPLY_ID = 920
PR_OPEN_AFTER_RESUME_ID = 930
LATEST_REPLY_ID = 921
UNREAD_PR_COMMENT_ID = 915
REVIEW_DEBOUNCE_SECONDS = 600
LABEL_VALIDATING = "validating"
LABEL_IN_REVIEW = "in_review"
LABEL_FIXING = "fixing"
PICKUP_MESSAGE = ":robot: orchestrator picking this up."
BOT_LOGIN = "orchestrator"
HUMAN_LOGIN = "alice"
BACKEND_CLAUDE = "claude"
DEV_SESSION = "dev-sess"
REVIEWED_SHA = "cafe1234"
CHECKS_SUCCESS = "success"
PR_LAST_COMMENT_ID = "pr_last_comment_id"
DEBOUNCE_SETTING = "IN_REVIEW_DEBOUNCE_SECONDS"
RUN_AGENT = "run_agent"
LONG_AGO = datetime.now(timezone.utc) - timedelta(hours=1)


class HandoffSkipsConsumedRepliesTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human reply consumed by `_resume_developer_on_human_reply` during
    implementing or validating must not re-surface as fresh PR feedback in
    in_review. The validating handoff watermark seed has to walk past such
    already-consumed comments; otherwise the next in_review tick re-routes
    the issue to `fixing` on the same human input the dev has already
    addressed.
    """

    def test_consumed_reply_not_replayed(self) -> None:
        gh = FakeGitHubClient()
        # Lifecycle: pickup (900) -> implementing dev asks question, parks
        # at 910 -> human replies "use sqlite" at 920 -> next tick resumes
        # the dev with that comment -> dev commits, _on_commits posts
        # PR-opened at 930 -> validating reviewer approves and posts
        # approval comment at 940. The reply at 920 was already fed to
        # the dev; in_review must NOT replay it.
        issue = make_issue(
            CONSUMED_REPLY_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=PARK_COMMENT_ID,
                    body="@hitl agent needs your input to proceed",
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=CONSUMED_REPLY_ID,
                    body="use sqlite please",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=PR_OPEN_AFTER_RESUME_ID,
                    body=":sparkles: PR opened: #1500",
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=CONSUMED_REPLY_PR,
            head_branch=CONSUMED_REPLY_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
        )
        gh.add_pr(pr)
        # `last_action_comment_id=920` reflects the post-resume bump --
        # the resume ate comments after the park (910) up through 920.
        gh.seed_state(
            CONSUMED_REPLY_ISSUE,
            pr_number=CONSUMED_REPLY_PR,
            branch=CONSUMED_REPLY_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[
                PICKUP_COMMENT_ID,
                PARK_COMMENT_ID,
                PR_OPEN_AFTER_RESUME_ID,
            ],
            pickup_comment_id=PICKUP_COMMENT_ID,
            last_action_comment_id=CONSUMED_REPLY_ID,
        )

        # Step 1: validating approves. The handoff seed must walk PAST
        # comment 920 (already consumed) instead of stopping at it.
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=(REVIEWED_SHA,),
        )
        watermark = gh.pinned_data(CONSUMED_REPLY_ISSUE).get(PR_LAST_COMMENT_ID)
        self.assertIsNotNone(watermark)
        self.assertGreaterEqual(
            watermark,
            PR_OPEN_AFTER_RESUME_ID,
            f"watermark must advance past consumed reply (id 920); got {watermark}",
        )

        # Step 2: in_review tick. Comment 920 must NOT surface and the
        # handler reaches the manual-merge HITL ping path.
        pr.approved = True
        if not any(label.name == LABEL_IN_REVIEW for label in issue.labels):
            issue.labels = [FakeLabel(LABEL_IN_REVIEW)]
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

        # Manual-merge-only: no merge call. The HITL ping fires because
        # the seed kept the consumed reply out of `new_comments`.
        self._assert_ready_path(gh, mocks)

    def test_resume_bumps_last_action_to_consumed_max(self) -> None:
        # Direct unit-level check on `_resume_developer_on_human_reply`:
        # after the resume runs, `last_action_comment_id` must reflect
        # the highest consumed id, not the prior park id.

        gh = FakeGitHubClient()
        issue = make_issue(
            RESUME_WATERMARK_ISSUE,
            label="implementing",
            comments=[
                FakeComment(id=PARK_COMMENT_ID, body="park", user=FakeUser(BOT_LOGIN)),
                FakeComment(id=CONSUMED_REPLY_ID, body="use sqlite", user=FakeUser(HUMAN_LOGIN)),
                FakeComment(id=LATEST_REPLY_ID, body="and add a test", user=FakeUser(HUMAN_LOGIN)),
            ],
        )
        gh.add_issue(issue)
        gh.seed_state(
            RESUME_WATERMARK_ISSUE,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            last_action_comment_id=PARK_COMMENT_ID,
        )
        state = gh.read_pinned_state(issue)

        with (
            patch.object(
                workflow,
                "_ensure_worktree",
                lambda spec, issue_number, **_: _FAKE_WT,
            ),
            patch.object(workflow, RUN_AGENT, lambda *args, **kwargs: _agent()),
        ):
            resume_result = workflow._resume_developer_on_human_reply(gh, _TEST_SPEC, issue, state)

        self.assertIsNotNone(resume_result)
        self.assertEqual(
            state.get("last_action_comment_id"),
            LATEST_REPLY_ID,
            "resume must bump last_action_comment_id to max(consumed)",
        )

    def _assert_ready_path(self, github, mocks) -> None:
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(github.merge_calls, [])
        self.assertNotIn(
            (CONSUMED_REPLY_ISSUE, "done"),
            github.label_history,
        )
        self.assertNotIn(
            (CONSUMED_REPLY_ISSUE, LABEL_FIXING),
            github.label_history,
        )
        ping_comments = [
            body
            for _, body in github.posted_comments
            if "ready for review/merge" in body
        ]
        self.assertEqual(len(ping_comments), 1)


class HandoffConsumedThroughIssueThreadOnlyTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`last_action_comment_id` only records issue-thread comments fed via
    `_resume_developer_on_human_reply`; PR-conversation comments are never
    consumed via that path. The validating handoff seed must NOT apply
    `consumed_through` to the PR-conversation surface, or a human PR comment
    whose id sits below a later-consumed issue-thread reply gets silently
    advanced past and the HITL ping fires over unread feedback.
    """

    def test_pr_comment_below_consumed_max_is_kept(self) -> None:
        gh = FakeGitHubClient()
        # Lifecycle: pickup (900) -> park asking question (910) -> human
        # leaves a PR-conv comment at 915 (the one that MUST surface) ->
        # human also replies on the issue thread at 920 -> resume consumes
        # the issue reply and bumps `last_action_comment_id` to 920 ->
        # PR-opened comment at 930 -> validating reviewer approves and
        # posts approval at 940. The PR-conv comment at 915 was never fed
        # to the dev (validating only watches the issue thread); without
        # the fix the seed walks past it because 915 <= consumed_through
        # (920) and the next tick pings HITL over it.
        issue = make_issue(
            ISSUE_THREAD_ISSUE,
            label=LABEL_VALIDATING,
            comments=[
                FakeComment(
                    id=PICKUP_COMMENT_ID,
                    body=PICKUP_MESSAGE,
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=PARK_COMMENT_ID,
                    body="@hitl agent needs your input to proceed",
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=CONSUMED_REPLY_ID,
                    body="use sqlite please",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=LONG_AGO,
                ),
                FakeComment(
                    id=PR_OPEN_AFTER_RESUME_ID,
                    body=":sparkles: PR opened: #1600",
                    user=FakeUser(BOT_LOGIN),
                    created_at=LONG_AGO,
                ),
            ],
        )
        gh.add_issue(issue)
        pr = FakePR(
            number=ISSUE_THREAD_PR,
            head_branch=ISSUE_THREAD_BRANCH,
            head=FakePRRef(sha=REVIEWED_SHA),
            mergeable=True,
            check_state=CHECKS_SUCCESS,
            issue_comments=[
                FakeComment(
                    id=UNREAD_PR_COMMENT_ID,
                    body="please add a docstring to the public class",
                    user=FakeUser(HUMAN_LOGIN),
                    created_at=LONG_AGO,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            ISSUE_THREAD_ISSUE,
            pr_number=ISSUE_THREAD_PR,
            branch=ISSUE_THREAD_BRANCH,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=0,
            orchestrator_comment_ids=[
                PICKUP_COMMENT_ID,
                PARK_COMMENT_ID,
                PR_OPEN_AFTER_RESUME_ID,
            ],
            pickup_comment_id=PICKUP_COMMENT_ID,
            last_action_comment_id=CONSUMED_REPLY_ID,
        )

        # Step 1: validating approves and seeds in_review watermarks. The
        # seed must stop before 915 so the next in_review tick scans the
        # PR-conv surface and finds the human comment. Approval routes
        # through `documenting` first (the final-docs hop).
        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=(REVIEWED_SHA,),
        )
        self.assertIn((ISSUE_THREAD_ISSUE, "documenting"), gh.label_history)
        watermark = gh.pinned_data(ISSUE_THREAD_ISSUE).get(PR_LAST_COMMENT_ID)
        self.assertIsNotNone(watermark)
        self.assertLess(
            watermark,
            UNREAD_PR_COMMENT_ID,
            "watermark must stop before unread PR-conv comment id=915 "
            f"(consumed_through=920 must NOT apply across surfaces); got {watermark}",
        )

        # Step 2: simulate the documenting no-change exit (final docs
        # pass found nothing to commit) and run the in_review tick.
        # The PR-conv comment surfaces and the handler routes the issue
        # to `fixing` (the fixing handler owns the dev resume on the
        # next tick) instead of pinging HITL.
        if not any(label.name == LABEL_IN_REVIEW for label in issue.labels):
            issue.labels = [FakeLabel(LABEL_IN_REVIEW)]
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
        self._assert_unread_route(gh, mocks)

    def _assert_unread_route(self, github, mocks) -> None:
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(github.merge_calls, [])
        self.assertIn(
            (ISSUE_THREAD_ISSUE, LABEL_FIXING),
            github.label_history,
        )
        state = github.pinned_data(ISSUE_THREAD_ISSUE)
        self.assertGreaterEqual(
            state.get("pending_fix_issue_max_id"),
            UNREAD_PR_COMMENT_ID,
        )
        self.assertLess(
            state.get(PR_LAST_COMMENT_ID),
            UNREAD_PR_COMMENT_ID,
        )
