# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing continue behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

ADVANCED_PR_COMMENT_WATERMARK = support.ADVANCED_PR_COMMENT_WATERMARK
ADVANCED_REVIEW_COMMENT_WATERMARK = support.ADVANCED_REVIEW_COMMENT_WATERMARK
ADVANCED_REVIEW_SUMMARY_WATERMARK = support.ADVANCED_REVIEW_SUMMARY_WATERMARK
ALICE = support.ALICE
AWAITING_HUMAN = support.AWAITING_HUMAN
BATCH_INLINE_ID = support.BATCH_INLINE_ID
BATCH_ISSUE_ID = support.BATCH_ISSUE_ID
BATCH_ISSUE_IDS = support.BATCH_ISSUE_IDS
BATCH_PR_CONVERSATION_ID = support.BATCH_PR_CONVERSATION_ID
BATCH_SUMMARY_ID = support.BATCH_SUMMARY_ID
BATCH_SUMMARY_IDS = support.BATCH_SUMMARY_IDS
BOB = support.BOB
BRANCH = support.BRANCH
CAROL = support.CAROL
CHANGES_REQUESTED = support.CHANGES_REQUESTED
CHECK_SUCCESS = support.CHECK_SUCCESS
COMMAND_COMMENT_ID = support.COMMAND_COMMENT_ID
DAVE = support.DAVE
DEV_AGENT = support.DEV_AGENT
DEV_SESSION_ID = support.DEV_SESSION_ID
FIXING = support.FIXING
FRESH_SESSION = support.FRESH_SESSION
FakeComment = support.FakeComment
FakeGitHubClient = support.FakeGitHubClient
FakePR = support.FakePR
FakePRRef = support.FakePRRef
FakePRReview = support.FakePRReview
FakeUser = support.FakeUser
GUIDED_COMMENT_ID = support.GUIDED_COMMENT_ID
ISSUE = support.ISSUE
NO_PRESERVED_MESSAGE = support.NO_PRESERVED_MESSAGE
PARK_AGENT_SILENT = support.PARK_AGENT_SILENT
PARK_AGENT_TIMEOUT = support.PARK_AGENT_TIMEOUT
PARK_REASON = support.PARK_REASON
PENDING_FIX_AT = support.PENDING_FIX_AT
PENDING_FIX_ISSUE_IDS = support.PENDING_FIX_ISSUE_IDS
PENDING_FIX_ISSUE_MAX_ID = support.PENDING_FIX_ISSUE_MAX_ID
PENDING_FIX_REVIEW_IDS = support.PENDING_FIX_REVIEW_IDS
PENDING_FIX_REVIEW_MAX_ID = support.PENDING_FIX_REVIEW_MAX_ID
PENDING_FIX_REVIEW_SUMMARY_IDS = support.PENDING_FIX_REVIEW_SUMMARY_IDS
PENDING_FIX_REVIEW_SUMMARY_MAX_ID = support.PENDING_FIX_REVIEW_SUMMARY_MAX_ID
POISONED_SESSION = support.POISONED_SESSION
PRESERVED_BATCH_BODIES = support.PRESERVED_BATCH_BODIES
PR_HEAD_SHA = support.PR_HEAD_SHA
PR_LAST_COMMENT_ID = support.PR_LAST_COMMENT_ID
PR_LAST_REVIEW_COMMENT_ID = support.PR_LAST_REVIEW_COMMENT_ID
PR_LAST_REVIEW_SUMMARY_ID = support.PR_LAST_REVIEW_SUMMARY_ID
PR_NUMBER = support.PR_NUMBER
PUSHED_FIX_MESSAGE = support.PUSHED_FIX_MESSAGE
RESUME_SESSION_ID = support.RESUME_SESSION_ID
REVIEW_ROUND = support.REVIEW_ROUND
RUN_AGENT = support.RUN_AGENT
SHA_AFTER = support.SHA_AFTER
SHA_BEFORE = support.SHA_BEFORE
VALIDATING = support.VALIDATING
_ContinueSeed = support._ContinueSeed
_PatchedWorkflowMixin = support._PatchedWorkflowMixin
_agent = support._agent
make_issue = support.make_issue
posted_comment_contains = support.posted_comment_contains


class _ContinueCommandFixtureMixin(_PatchedWorkflowMixin):
    """`/orchestrator continue` retries a `fixing` park caused by a
    session-limit / session-failure reason (`agent_silent` / `agent_timeout`).
    On the in_review route it replays the PRESERVED review-feedback batch on a
    FRESH dev session rather than resuming on the command text -- the
    geserdugarov/lance-open-source#23 shape where a generic continue lost the
    latest review feedback. On the validating route (no replayable batch) and
    for parks that still need real human guidance, it is refused rather than
    resumed on the command text. A comment mixing guidance with the command
    line is left as ordinary feedback so its guidance is never dropped.
    """

    def _seed_parked_with_batch(
        self,
        seed: _ContinueSeed,
    ):
        # Batch feedback spans all three surfaces and sits BELOW the advanced
        # watermarks -- the shape after a poisoned/timed-out resume already
        # advanced past it. `_reconstruct_pending_fix_batch` re-fetches it
        # from the preserved `pending_fix_*_ids`. The `/orchestrator continue`
        # comment sits ABOVE the issue watermark so the per-tick rescan
        # surfaces it as fresh feedback. `pending_fix_at=None` +
        # `with_batch_ids=False` models a validating-route park (no batch).
        issue = make_issue(ISSUE, label=FIXING)
        issue.comments.append(
            FakeComment(
                id=BATCH_ISSUE_ID,
                body="fix the null check",
                user=FakeUser(CAROL),
            ),
        )
        command = FakeComment(
            id=seed.command_id,
            body=seed.command_body,
            user=FakeUser(DAVE),
        )
        if not seed.command_on_pr_conversation:
            issue.comments.append(command)
        for comment in seed.extra_issue_comments:
            issue.comments.append(comment)
        pr_conv = [
            FakeComment(
                id=BATCH_PR_CONVERSATION_ID,
                body="handle the edge case",
                user=FakeUser(ALICE),
            ),
        ]
        if seed.command_on_pr_conversation:
            pr_conv.append(command)
        self._pr = FakePR(
            number=PR_NUMBER,
            head_branch=BRANCH,
            head=FakePRRef(sha=PR_HEAD_SHA),
            mergeable=True,
            check_state=CHECK_SUCCESS,
            issue_comments=pr_conv,
            review_comments=[
                FakeComment(
                    id=BATCH_INLINE_ID,
                    body="rename the temp var",
                    user=FakeUser(BOB),
                ),
            ],
            reviews=[
                FakePRReview(
                    id=BATCH_SUMMARY_ID,
                    body="please address the review",
                    state=CHANGES_REQUESTED,
                ),
            ],
        )
        gh = FakeGitHubClient()
        gh.add_issue(issue)
        gh.add_pr(self._pr)
        self._state = {
            "pr_number": PR_NUMBER,
            "branch": BRANCH,
            "dev_agent": DEV_AGENT,
            DEV_SESSION_ID: POISONED_SESSION,
            REVIEW_ROUND: 1,
            AWAITING_HUMAN: True,
            PARK_REASON: seed.park_reason,
            "silent_park_count": seed.silent_park_count,
            # Watermarks advanced PAST the batch.
            PR_LAST_COMMENT_ID: ADVANCED_PR_COMMENT_WATERMARK,
            PR_LAST_REVIEW_COMMENT_ID: ADVANCED_REVIEW_COMMENT_WATERMARK,
            PR_LAST_REVIEW_SUMMARY_ID: ADVANCED_REVIEW_SUMMARY_WATERMARK,
        }
        if seed.pending_fix_at is not None:
            self._state[PENDING_FIX_AT] = seed.pending_fix_at
        if seed.with_batch_ids:
            self._state.update(
                {
                    PENDING_FIX_ISSUE_IDS: list(BATCH_ISSUE_IDS),
                    PENDING_FIX_ISSUE_MAX_ID: BATCH_PR_CONVERSATION_ID,
                    PENDING_FIX_REVIEW_IDS: [BATCH_INLINE_ID],
                    PENDING_FIX_REVIEW_MAX_ID: BATCH_INLINE_ID,
                    PENDING_FIX_REVIEW_SUMMARY_IDS: list(BATCH_SUMMARY_IDS),
                    PENDING_FIX_REVIEW_SUMMARY_MAX_ID: BATCH_SUMMARY_ID,
                }
            )
        gh.seed_state(ISSUE, **self._state)
        return gh, issue, self._pr


def _assert_replayed_prompt(test_case) -> None:
    for body in PRESERVED_BATCH_BODIES:
        test_case.assertIn(body, test_case._prompt)
    test_case.assertIsNone(
        test_case._call.kwargs.get(RESUME_SESSION_ID),
    )


def _assert_replayed_state(test_case, github) -> None:
    test_case._pinned_data = github.pinned_data(ISSUE)
    test_case.assertEqual(
        test_case._pinned_data.get(DEV_SESSION_ID),
        FRESH_SESSION,
    )
    test_case.assertIn((ISSUE, VALIDATING), github.label_history)
    test_case.assertEqual(test_case._pinned_data.get(REVIEW_ROUND), 0)
    test_case.assertIsNone(test_case._pinned_data.get(PENDING_FIX_AT))
    test_case.assertIsNone(
        test_case._pinned_data.get(PENDING_FIX_ISSUE_IDS),
    )
    test_case.assertEqual(
        test_case._pinned_data.get(PR_LAST_COMMENT_ID),
        COMMAND_COMMENT_ID,
    )
    test_case.assertFalse(
        test_case._pinned_data.get(AWAITING_HUMAN),
    )
    test_case.assertIsNone(test_case._pinned_data.get(PARK_REASON))


class OrchestratorContinueCommandTest(
    unittest.TestCase,
    _ContinueCommandFixtureMixin,
):
    def test_session_error_park_replays_saved_batch(self) -> None:
        # Both session-failure reasons: the command drops the poisoned session
        # and replays the FULL preserved batch on a fresh spawn, then the
        # pushed fix routes back to `validating` with the round reset.
        for reason in (PARK_AGENT_SILENT, PARK_AGENT_TIMEOUT):
            with self.subTest(reason=reason):
                gh, issue, pr = self._seed_parked_with_batch(
                    _ContinueSeed(park_reason=reason),
                )

                self._mocks = self._run_fixing(
                    gh,
                    issue,
                    run_agent=_agent(
                        session_id=FRESH_SESSION,
                        last_message=PUSHED_FIX_MESSAGE,
                    ),
                    head_shas=(SHA_BEFORE, SHA_AFTER),
                )

                self._mocks[RUN_AGENT].assert_called_once()
                self._call = self._mocks[RUN_AGENT].call_args
                self._prompt = self._call.args[1]
                _assert_replayed_prompt(self)
                _assert_replayed_state(self, gh)

    def test_refuses_continue_on_question_park(self) -> None:
        # A real agent question / dirty worktree parks with `park_reason=None`.
        # A generic continue carries none of the answer, so refuse: stay
        # parked, consume the command so the refusal does not re-fire, and
        # leave the preserved batch intact for a genuine human reply.
        gh, issue, pr = self._seed_parked_with_batch(
            _ContinueSeed(park_reason=None),
        )

        mocks = self._run_fixing(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self._pinned_data = gh.pinned_data(ISSUE)
        self.assertTrue(self._pinned_data.get(AWAITING_HUMAN))
        self.assertNotIn((ISSUE, VALIDATING), gh.label_history)
        self.assertEqual(self._pinned_data.get(PR_LAST_COMMENT_ID), COMMAND_COMMENT_ID)
        self.assertEqual(
            self._pinned_data.get(PENDING_FIX_ISSUE_IDS),
            list(BATCH_ISSUE_IDS),
        )
        self.assertTrue(
            posted_comment_contains(
                gh,
                "/orchestrator continue",
                "guidance",
            ),
        )

    def test_refuses_continue_when_no_preserved_batch(self) -> None:
        # Eligible reason but nothing on file to replay (bookmarks gone). A
        # bare continue would strand the review feedback, so refuse rather
        # than resume on the command text.
        gh, issue, pr = self._seed_parked_with_batch(
            _ContinueSeed(
                park_reason=PARK_AGENT_SILENT,
                with_batch_ids=False,
            ),
        )

        mocks = self._run_fixing(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        self._pinned_data = gh.pinned_data(ISSUE)
        self.assertTrue(self._pinned_data.get(AWAITING_HUMAN))
        self.assertEqual(self._pinned_data.get(PARK_REASON), PARK_AGENT_SILENT)
        self.assertEqual(self._pinned_data.get(PR_LAST_COMMENT_ID), COMMAND_COMMENT_ID)
        self.assertTrue(
            posted_comment_contains(gh, NO_PRESERVED_MESSAGE),
        )

    def test_continue_with_feedback_resumes_normally(self) -> None:
        # A `/orchestrator continue` posted ALONGSIDE genuine guidance on an
        # unsafe park is NOT intercepted: the other comment is the real answer
        # the park was waiting on, so the normal awaiting-human resume runs on
        # the live session.
        genuine = FakeComment(
            id=GUIDED_COMMENT_ID,
            body="use option B, not A",
            user=FakeUser(DAVE),
        )
        gh, issue, pr = self._seed_parked_with_batch(
            _ContinueSeed(
                park_reason=None,
                extra_issue_comments=(genuine,),
                silent_park_count=0,
            ),
        )

        self._mocks = self._run_fixing(
            gh,
            issue,
            run_agent=_agent(
                session_id=POISONED_SESSION,
                last_message=PUSHED_FIX_MESSAGE,
            ),
            head_shas=(SHA_BEFORE, SHA_AFTER),
        )

        self._mocks[RUN_AGENT].assert_called_once()
        call = self._mocks[RUN_AGENT].call_args
        self.assertIn("use option B", call.args[1])
        # Live session resumed (not dropped) -- this is a real dev question.
        self.assertEqual(call.kwargs.get(RESUME_SESSION_ID), POISONED_SESSION)

    def test_validating_error_refuses_continue(self) -> None:
        # A validating-route park (no `pending_fix_at`, no preserved batch, and
        # no `pending_fix_reviewer_comment_id` anchor) on a session-failure
        # reason must NOT resume the dev on the bare command text. With nothing
        # to replay it is refused: no agent spawn, command consumed, issue stays
        # parked. This is the #742 negative case -- the anchor is absent.
        for reason in (PARK_AGENT_SILENT, PARK_AGENT_TIMEOUT):
            with self.subTest(reason=reason):
                gh, issue, pr = self._seed_parked_with_batch(
                    _ContinueSeed(
                        park_reason=reason,
                        pending_fix_at=None,
                        with_batch_ids=False,
                    ),
                )

                self._mocks = self._run_fixing(
                    gh,
                    issue,
                    run_agent=_agent(),
                )

                self._mocks[RUN_AGENT].assert_not_called()
                self._pinned_data = gh.pinned_data(ISSUE)
                self.assertTrue(self._pinned_data.get(AWAITING_HUMAN))
                self.assertEqual(self._pinned_data.get(PARK_REASON), reason)
                self.assertNotIn((ISSUE, VALIDATING), gh.label_history)
                self.assertEqual(self._pinned_data.get(PR_LAST_COMMENT_ID), COMMAND_COMMENT_ID)
                self.assertTrue(
                    posted_comment_contains(gh, NO_PRESERVED_MESSAGE),
                )
