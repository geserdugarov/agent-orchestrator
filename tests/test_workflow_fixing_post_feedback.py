# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for fixing post feedback behavior."""

from __future__ import annotations

import unittest

from tests import fixing_test_support as support

IssueScenario = support.IssueScenario

ALICE = support.ALICE
AWAITING_HUMAN = support.AWAITING_HUMAN
DEBOUNCE_CONFIG = support.DEBOUNCE_CONFIG
DEBOUNCE_SECONDS = support.DEBOUNCE_SECONDS
DEV_SESSION = support.DEV_SESSION
DOCUMENTING = support.DOCUMENTING
FIX_FEEDBACK = support.FIX_FEEDBACK
FakeComment = support.FakeComment
FakeUser = support.FakeUser
HISTORICAL_COMMENT_ID = support.HISTORICAL_COMMENT_ID
ISSUE = support.ISSUE
ORCHESTRATOR = support.ORCHESTRATOR
ORCHESTRATOR_PARK_COMMENT_ID = support.ORCHESTRATOR_PARK_COMMENT_ID
PENDING_FIX_AT = support.PENDING_FIX_AT
PENDING_FIX_ISSUE_MAX_ID = support.PENDING_FIX_ISSUE_MAX_ID
PR_LAST_COMMENT_ID = support.PR_LAST_COMMENT_ID
PUSHED_MESSAGE = support.PUSHED_MESSAGE
REVIEW_ROUND = support.REVIEW_ROUND
RUN_AGENT = support.RUN_AGENT
SHA_AFTER = support.SHA_AFTER
SHA_BEFORE = support.SHA_BEFORE
TRANSIENT_PARK_WATERMARK = support.TRANSIENT_PARK_WATERMARK
TRIGGER_ID = support.TRIGGER_ID
VALIDATING = support.VALIDATING
_FixingFixtureMixin = support._FixingFixtureMixin
_agent = support._agent
config = support.config
datetime = support.datetime
patch = support.patch
timedelta = support.timedelta
timezone = support.timezone


class FixingPostFeedbackRoutingTest(unittest.TestCase, _FixingFixtureMixin):
    def test_review_fix_resets_round_to_zero(self) -> None:
        # Companion to the test above: the in_review->fixing route
        # (which sets `pending_fix_at` when fresh PR feedback lands after
        # reviewer approval) MUST reset `review_round` to 0 on a pushed
        # fix. The previous reviewer round was APPROVED so the new fix
        # starts a fresh round-count.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=TRIGGER_ID,
            body=FIX_FEEDBACK,
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=pr,
                issue_comments=[comment],
                extra_state={REVIEW_ROUND: 2},
            )
        )
        # `_seed` already sets `pending_fix_at` (modeling the in_review
        # route); confirm before asserting the reset.
        self.assertIsNotNone(scenario.github.pinned_data(ISSUE).get(PENDING_FIX_AT))

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message="fixed",
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
                push_branch=True,
            )

        pinned_data = scenario.github.pinned_data(ISSUE)
        # Reset to 0 since the previous round was APPROVED.
        self.assertEqual(pinned_data.get(REVIEW_ROUND), 0)
        self.assertIsNone(pinned_data.get(PENDING_FIX_AT))

    # --- no unread feedback at all --------------------------------------

    def test_no_unread_feedback_returns_to_validating(self) -> None:
        # Defensive recovery: if the rescan finds nothing (watermarks
        # already cover the bookmarks), there is no fix work to do.
        # Bounce the label back to `validating` so the reviewer
        # re-evaluates and the issue is not stranded in `fixing`.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                # Watermark already past the recorded bookmark.
                PR_LAST_COMMENT_ID: TRANSIENT_PARK_WATERMARK,
                PENDING_FIX_ISSUE_MAX_ID: 4900,
            },
        )

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((ISSUE, VALIDATING), gh.label_history)
        self.assertNotIn((ISSUE, DOCUMENTING), gh.label_history)
        pinned_data = gh.pinned_data(ISSUE)
        self.assertIsNone(pinned_data.get(PENDING_FIX_AT))
        self.assertIsNone(pinned_data.get(PENDING_FIX_ISSUE_MAX_ID))

    # --- PR fetch failure bails this tick instead of crashing -----------

    def test_open_issue_pr_error_bails_cleanly(self) -> None:
        # If `gh.get_pr` raises for an open `fixing` issue, the handler
        # used to fall through into the rescan with `pr=None` and crash
        # at `gh.pr_conversation_comments_after(pr, ...)`. The guard
        # should bail the tick gracefully so the next poll re-fetches.
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr)
        # Replace `get_pr` so the call raises. PyGithub-side failures
        # (rate limit, 5xx, network blip) are typically transient.
        with patch.object(gh, "get_pr", side_effect=RuntimeError("github api down")):
            with patch.object(
                config,
                DEBOUNCE_CONFIG,
                DEBOUNCE_SECONDS,
            ):
                mocks = self._run_fixing(
                    gh,
                    issue,
                    run_agent=_agent(),
                )

        # No agent spawn, no label change, no park comment -- just a
        # quiet bail so the next tick retries.
        mocks[RUN_AGENT].assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertFalse(gh.pinned_data(ISSUE).get(AWAITING_HUMAN))

    def test_missing_pr_comment_id_uses_last_action(
        self,
    ) -> None:
        # `_handle_in_review` can route to `fixing` with
        # `pr_last_comment_id` still unset (e.g. an issue whose state
        # pre-dates the watermark migration, or a manual relabel
        # path). Without the fallback, fixing would scan from
        # `None` and re-feed every historical issue / PR-conversation
        # comment to the dev as fresh feedback. The fallback mirrors
        # the in_review handler so an existing `last_action_comment_id`
        # (set by prior parks / resumes) acts as the scan floor.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        historical = FakeComment(
            id=HISTORICAL_COMMENT_ID,
            body="some old discussion from implementing",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        triggering = FakeComment(
            id=TRIGGER_ID,
            body="please rename foo",
            user=FakeUser(ALICE),
            created_at=long_ago,
        )
        pr = self._open_pr()
        scenario = IssueScenario(
            *self._seed(
                pr=pr,
                issue_comments=[historical, triggering],
                extra_state={
                    # No `pr_last_comment_id` at all -- the in_review
                    # legacy migration did not run on this issue.
                    PR_LAST_COMMENT_ID: None,
                    # But `last_action_comment_id` is set (a park comment
                    # id from validating, say) and sits above the
                    # historical comment.
                    "last_action_comment_id": 1000,
                },
            )
        )

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            self._mocks = self._run_fixing(
                scenario.github,
                scenario.issue,
                run_agent=_agent(
                    session_id=DEV_SESSION,
                    last_message=PUSHED_MESSAGE,
                ),
                head_shas=(SHA_BEFORE, SHA_AFTER),
            )

        self._mocks[RUN_AGENT].assert_called_once()
        self._agent_call = self._mocks[RUN_AGENT].call_args
        self._prompt = self._agent_call.args[1]
        # The triggering comment (id=TRIGGER_ID) IS quoted -- it's past
        # the last_action_comment_id fallback floor.
        self.assertIn("please rename foo", self._prompt)
        # The historical comment (id=500) is NOT quoted -- it sits
        # below the fallback floor (1000) and must not be re-fed.
        self.assertNotIn("some old discussion from implementing", self._prompt)

    # --- orchestrator comments are filtered from the rescan -------------

    def test_park_comment_filtered_from_rescan(self) -> None:
        # A prior tick may have posted an orchestrator comment with id
        # past the watermark. The rescan filters orchestrator-authored
        # comments (by recorded id AND by hidden body marker) so a HITL
        # ping does not re-trigger a dev resume.
        from orchestrator.workflow_messages import _ORCH_COMMENT_MARKER

        orch_comment = FakeComment(
            id=ORCHESTRATOR_PARK_COMMENT_ID,
            body=f":bell: ready for review/merge\n\n{_ORCH_COMMENT_MARKER}",
            user=FakeUser(ORCHESTRATOR),
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            issue_comments=[orch_comment],
            extra_state={
                # Watermark already past the bookmark so the rescan
                # only sees the orchestrator-authored comment.
                PR_LAST_COMMENT_ID: 2010,
                PENDING_FIX_ISSUE_MAX_ID: TRIGGER_ID,
            },
        )

        with patch.object(config, DEBOUNCE_CONFIG, DEBOUNCE_SECONDS):
            mocks = self._run_fixing(
                gh,
                issue,
                run_agent=_agent(),
            )

        mocks[RUN_AGENT].assert_not_called()
        # No new feedback -> bounce back to validating (rather than
        # treating the orchestrator's own comment as fresh feedback).
        self.assertIn((ISSUE, VALIDATING), gh.label_history)
