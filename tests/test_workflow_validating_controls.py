# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Review-cap, interruption, and trust-filter validating scenarios."""

from __future__ import annotations

import unittest

from tests import validating_review_test_support as review_support

Path = review_support.Path
patch = review_support.patch
config = review_support.config
workflow = review_support.workflow
FakeComment = review_support.FakeComment
FakeGitHubClient = review_support.FakeGitHubClient
FakeUser = review_support.FakeUser
make_issue = review_support.make_issue
EVENT_AGENT_SPAWN = review_support.EVENT_AGENT_SPAWN
LABEL_VALIDATING = review_support.LABEL_VALIDATING
REVIEW_APPROVED_MESSAGE = review_support.REVIEW_APPROVED_MESSAGE
ROLE_REVIEWER = review_support.ROLE_REVIEWER
_PatchedWorkflowMixin = review_support._PatchedWorkflowMixin
_TEST_SPEC = review_support._TEST_SPEC
_agent = review_support._agent
_issue_branch = review_support._issue_branch
_ReviewCapFixtureMixin = review_support.ReviewCapFixtureMixin
_InterruptedFixFixtureMixin = review_support.InterruptedFixFixtureMixin
_ResumeTrustFixtureMixin = review_support.ResumeTrustFixtureMixin
_CapRecoveryAssertionsMixin = review_support.CapRecoveryAssertionsMixin

REVIEW_CAP_ISSUE = 80
CAP_MESSAGE_ISSUE = 81
CAP_MESSAGE_PR = 16
CAP_REASON_ISSUE = 82
CAP_REASON_PR = 17
CAP_RECOVERY_ISSUE = 83
SECONDARY_PR = 18
INTERRUPTED_RESUME_PR = 19
TRUST_CAP_ISSUE = 90
TRUST_RETRY_ISSUE = 91
HUMAN_COMMENT_ID = 1100
LATEST_COMMAND_ID = 1101
FOLLOWUP_COMMENT_ID = 1200
HUMAN_RETRY_COMMENT_ID = 1300
ACTION_COMMENT_ID = 950
PICKUP_COMMENT_ID = 900
CAP_COMMAND_ID = 2000
DEV_SESSION = "dev-sess"
RUN_AGENT = "run_agent"
FIXED_MESSAGE = "fixed"
BEFORE_FIX_SHA = "aaa"
AFTER_FIX_SHA = "bbb"
PRE_FIX_SHA = "sha-before"
PUSH_BRANCH = "_push_branch"
REVIEW_ROUND = "review_round"
AWAITING_HUMAN = "awaiting_human"
PARK_REASON = "park_reason"
AGENT_ROLE = "agent_role"
EVENT_NAME = "event"
BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
HUMAN_LOGIN = "alice"
LAST_ACTION_COMMENT_ID = "last_action_comment_id"
REVIEW_CAP = "review_cap"
ADD_ONE_ROUND_COMMAND = "/orchestrator add-review-rounds 1"
CAP_RESET_MESSAGE = "review-cap reset"
USER_CONTENT_HASH = "user_content_hash"
ALLOWED_AUTHORS_SETTING = "ALLOWED_ISSUE_AUTHORS"
ALLOWED_AUTHORS = ("geserdugarov",)


class HandleValidatingReviewCapAddRoundsCommandTest(
    unittest.TestCase,
    _ReviewCapFixtureMixin,
):
    """Reset a review-cap park with a valid operator command."""

    def test_command_resets_round_park_and_reruns(self) -> None:
        # Granting 1 more round on a 3-cap means review_round becomes 2.
        # The reviewer-spawn block fires on the SAME tick (fall-through
        # parity with the reviewer_timeout / reviewer_failed branches) so
        # the operator does not have to wait an extra poll for the
        # reviewer to actually rerun.
        gh, issue = self._seeded(
            comment_body=ADD_ONE_ROUND_COMMAND,
        )

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(
                last_message=REVIEW_APPROVED_MESSAGE,
            ),
            head_shas=[BEFORE_FIX_SHA],
        )

        state = gh.pinned_data(REVIEW_CAP_ISSUE)
        self.assertFalse(state.get(AWAITING_HUMAN))
        self.assertIsNone(state.get(PARK_REASON))
        self.assertEqual(
            state.get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS - 1,
        )
        # Watermark advanced past the operator's command comment so the
        # next tick doesn't re-fire the same command.
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), HUMAN_COMMENT_ID)
        # Reviewer ran THIS tick (parity with reviewer_timeout fall-through).
        self.assertEqual(mocks[RUN_AGENT].call_count, 1)
        self._assert_reviewer_spawn(gh)
        # Confirmation comment posted on the issue.
        self.assertTrue(
            any(
                CAP_RESET_MESSAGE in body
                and "granting 1 more round" in body
                for _, body in gh.posted_comments
            )
        )

    def test_max_n_grants_full_reset(
        self,
    ) -> None:
        # `N >= MAX_REVIEW_ROUNDS` clamps review_round to 0 -- the full
        # reset. The reviewer-spawn block then runs with a fresh budget.
        requested_rounds = config.MAX_REVIEW_ROUNDS + 5
        gh, issue = self._seeded(
            comment_body=f"/orchestrator add-review-rounds {requested_rounds}",
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=[BEFORE_FIX_SHA],
        )

        self.assertEqual(gh.pinned_data(REVIEW_CAP_ISSUE).get(REVIEW_ROUND), 0)

    def test_command_picks_latest(self) -> None:
        # Two commands in the same batch: the later one wins so a
        # corrected post supersedes a stale typo without needing the
        # operator to delete the first comment.
        gh, issue = self._seeded()
        issue.comments.append(
            FakeComment(
                id=HUMAN_COMMENT_ID,
                body=ADD_ONE_ROUND_COMMAND,
                user=FakeUser(HUMAN_LOGIN),
            )
        )
        issue.comments.append(
            FakeComment(
                id=LATEST_COMMAND_ID,
                body="actually scratch that\n/orchestrator add-review-rounds 2",
                user=FakeUser(HUMAN_LOGIN),
            )
        )

        self._run_validating(
            gh,
            issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=[BEFORE_FIX_SHA],
        )

        self.assertEqual(
            gh.pinned_data(REVIEW_CAP_ISSUE).get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS - 2,
        )
        self.assertEqual(gh.pinned_data(REVIEW_CAP_ISSUE).get(LAST_ACTION_COMMENT_ID), LATEST_COMMAND_ID)

    def test_zero_command_rejected_and_parked(self) -> None:
        gh, issue = self._seeded(
            comment_body="/orchestrator add-review-rounds 0",
        )

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )

        # No agent ran: the error path stays parked, doesn't fall through.
        mocks[RUN_AGENT].assert_not_called()
        state = gh.pinned_data(REVIEW_CAP_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), REVIEW_CAP)
        # Round is unchanged.
        self.assertEqual(
            state.get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS,
        )
        # Watermark advanced so the operator can post a corrected command
        # in a new comment without re-tripping the same rejection.
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), HUMAN_COMMENT_ID)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("ignored", last_comment)
        self.assertIn("positive integer", last_comment)

    def test_plain_reply_stays_parked(self) -> None:
        # The original bug: on a `review_cap` park, a plain human reply
        # used to wake the dev session and the reviewer rebumped past
        # the cap on the next tick. The new behavior is to stay parked
        # silently when no command is present; only the explicit command
        # can restart the loop.
        gh, issue = self._seeded(comment_body="any luck on this?")

        mocks = self._run_validating(
            gh,
            issue,
            run_agent=_agent(),
        )

        mocks[RUN_AGENT].assert_not_called()
        state = gh.pinned_data(REVIEW_CAP_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertEqual(state.get(PARK_REASON), REVIEW_CAP)
        # Watermark NOT advanced -- the operator may still post the
        # command later in a follow-up comment, and we need to see it.
        self.assertEqual(state.get(LAST_ACTION_COMMENT_ID), ACTION_COMMENT_ID)


class HandleValidatingReviewCapCommandGuardTest(
    unittest.TestCase,
    _ReviewCapFixtureMixin,
    _CapRecoveryAssertionsMixin,
):
    """Reject misplaced commands and advertise real cap recovery."""

    def test_command_only_fires_on_review_cap_park(self) -> None:
        # A command posted under a different park reason (here: a
        # standard dev-question park with `park_reason=None`) must NOT
        # take the cap-reset branch. The dev resume runs as usual.
        guard_github, guard_issue = self._seeded(
            comment_body=ADD_ONE_ROUND_COMMAND,
            park_reason=None,
            review_round=1,
        )

        self._run_validating(
            guard_github,
            guard_issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message=FIXED_MESSAGE),
            head_shas=[BEFORE_FIX_SHA, AFTER_FIX_SHA],
            dirty_files=(),
            push_branch=True,
        )

        guard_state = guard_github.pinned_data(REVIEW_CAP_ISSUE)
        # Dev resume bumped the round; no cap-reset semantics applied.
        self.assertEqual(guard_state.get(REVIEW_ROUND), 2)
        # No reset confirmation comment was posted.
        self.assertFalse(
            any(
                CAP_RESET_MESSAGE in body
                for _, body in guard_github.posted_comments
            )
        )

    def test_command_inline_in_prose_does_not_fire(self) -> None:
        # The regex requires the command at the start of a line, so a
        # quote of the syntax in regular prose (e.g. the operator asking
        # someone else how to use it) does not trigger the reset.
        guard_github, guard_issue = self._seeded(
            comment_body=("do we just run `/orchestrator add-review-rounds 1` here?"),
        )

        guard_patches = self._run_validating(
            guard_github,
            guard_issue,
            run_agent=_agent(),
        )

        guard_patches[RUN_AGENT].assert_not_called()
        guard_state = guard_github.pinned_data(REVIEW_CAP_ISSUE)
        self.assertTrue(guard_state.get(AWAITING_HUMAN))
        self.assertEqual(guard_state.get(PARK_REASON), REVIEW_CAP)
        self.assertEqual(
            guard_state.get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS,
        )

    def test_cap_park_advertises_command(self) -> None:
        # When the orchestrator first parks on the cap, the park comment
        # itself surfaces the command so an operator who has never seen
        # the syntax can copy/paste it from the guard_issue thread.
        guard_github = FakeGitHubClient()
        guard_issue = make_issue(CAP_MESSAGE_ISSUE, label=LABEL_VALIDATING)
        guard_github.add_issue(guard_issue)
        guard_github.seed_state(
            CAP_MESSAGE_ISSUE,
            review_round=config.MAX_REVIEW_ROUNDS,
            pr_number=CAP_MESSAGE_PR,
            branch=_issue_branch(CAP_MESSAGE_ISSUE),
        )

        self._run_validating(
            guard_github,
            guard_issue,
            run_agent=_agent(),
        )

        last_comment = guard_github.posted_comments[-1][1]
        self.assertIn("/orchestrator add-review-rounds", last_comment)

    def test_cap_park_persists_reason(self) -> None:
        # `_park_awaiting_human` always clears `park_reason` to None (its
        # `reason=` kwarg only feeds the audit event), so the cap branch
        # must re-set the durable field itself. Without this, the next
        # tick's awaiting-human dispatch sees `park_reason=None` and the
        # `/orchestrator add-review-rounds` parser never runs -- the
        # command would silently fall through to the dev-resume branch.
        guard_github = FakeGitHubClient()
        guard_issue = make_issue(CAP_REASON_ISSUE, label=LABEL_VALIDATING)
        guard_github.add_issue(guard_issue)
        guard_github.seed_state(
            CAP_REASON_ISSUE,
            review_round=config.MAX_REVIEW_ROUNDS,
            pr_number=CAP_REASON_PR,
            branch=_issue_branch(CAP_REASON_ISSUE),
        )

        self._run_validating(
            guard_github,
            guard_issue,
            run_agent=_agent(),
        )

        guard_state = guard_github.pinned_data(CAP_REASON_ISSUE)
        self.assertTrue(guard_state.get(AWAITING_HUMAN))
        self.assertEqual(guard_state.get(PARK_REASON), REVIEW_CAP)

    def test_command_fires_after_real_cap_park(self) -> None:
        # End-to-end regression for the original bug: the FIRST tick must
        # park via the cap branch (not pre-seeded shortcut), persist
        # `park_reason="review_cap"`, and seed a `user_content_hash`. The
        # SECOND tick must then bypass the user-content-drift branch
        # (the operator's command comment changes the hash by definition)
        # and route through the cap-reset path so the round actually
        # resets. Pre-seeded tests above cover the command parser in
        # isolation; this one closes the loop on the production sequence.
        guard_github = FakeGitHubClient()
        guard_issue = make_issue(CAP_RECOVERY_ISSUE, label=LABEL_VALIDATING)
        guard_github.add_issue(guard_issue)
        guard_github.seed_state(
            CAP_RECOVERY_ISSUE,
            review_round=config.MAX_REVIEW_ROUNDS,
            pr_number=SECONDARY_PR,
            branch=_issue_branch(CAP_RECOVERY_ISSUE),
            pickup_comment_id=PICKUP_COMMENT_ID,
            dev_session_id=DEV_SESSION,
            dev_agent=BACKEND_CODEX,
        )

        # Tick 1: cap park.
        self._run_validating(
            guard_github,
            guard_issue,
            run_agent=_agent(),
        )
        baseline_hash = self._cap_park_hash(guard_github)

        # Operator posts the command after the cap park. This is a
        # non-orchestrator comment, so it shifts the content hash --
        # without the drift-block bypass the next tick would resume the
        # dev session on a body-edit prompt and never see the command.
        guard_issue.comments.append(
            FakeComment(
                id=CAP_COMMAND_ID,
                body=ADD_ONE_ROUND_COMMAND,
                user=FakeUser(HUMAN_LOGIN),
            )
        )

        # Tick 2: command processes through the cap-reset path.
        self._run_validating(
            guard_github,
            guard_issue,
            run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
            head_shas=[BEFORE_FIX_SHA],
        )
        self._assert_cap_reset_state(guard_github, baseline_hash)
        self._assert_cap_reset_event(guard_github)


class ValidatingDevFixInterruptedHelperTest(
    unittest.TestCase,
    _InterruptedFixFixtureMixin,
):
    """Ignore shutdown-interrupted results before fix disposition."""

    def test_false_without_side_effects(
        self,
    ) -> None:
        helper_github, helper_state, helper_issue = self._seeded()
        agent_result = _agent(
            session_id=DEV_SESSION,
            interrupted=True,
            last_message="partial output before the shutdown SIGTERM",
        )

        pushed = workflow._handle_dev_fix_result(
            helper_github,
            _TEST_SPEC,
            helper_issue,
            helper_state,
            Path("/tmp/wt"),
            agent_result,
            PRE_FIX_SHA,
        )

        self.assertFalse(pushed)
        # No park: awaiting_human untouched, no transient reason tagged, no
        # timeout watermark persisted.
        self.assertFalse(helper_state.get(AWAITING_HUMAN))
        self.assertIsNone(helper_state.get(PARK_REASON))
        self.assertIsNone(helper_state.get("pre_dev_fix_sha"))
        # No HITL / question comment posted on either surface.
        self.assertEqual(helper_github.posted_comments, [])
        self.assertEqual(helper_github.posted_pr_comments, [])

    def test_content_change_returns_parked(
        self,
    ) -> None:
        helper_github, helper_state, helper_issue = self._seeded()
        agent_result = _agent(
            session_id=DEV_SESSION,
            interrupted=True,
            last_message="ACK: looks fine",  # partial; must NOT be honored
        )

        outcome = workflow._post_user_content_change_result(
            helper_github,
            _TEST_SPEC,
            helper_issue,
            helper_state,
            Path("/tmp/wt"),
            agent_result,
            PRE_FIX_SHA,
        )

        # Reported parked, but WITHOUT swallowing the partial message as an
        # ack or parking awaiting_human.
        self.assertEqual(outcome, "parked")
        self.assertFalse(helper_state.get(AWAITING_HUMAN))
        self.assertIsNone(helper_state.get(PARK_REASON))
        self.assertIsNone(helper_state.get("pre_dev_fix_sha"))
        self.assertEqual(helper_github.posted_comments, [])
        self.assertEqual(helper_github.posted_pr_comments, [])


class ValidatingInterruptedResumeHandlerTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Handler-level guards: an interrupted resume in `_handle_validating`'s
    user-content-change and awaiting-human paths must NOT persist the
    consumption pre-staged before the spawn, so the next tick retries the
    resume rather than treating the input as already handled."""

    def test_content_change_resume_not_persisted(
        self,
    ) -> None:
        interrupted_github = FakeGitHubClient()
        interrupted_issue = make_issue(8, label=LABEL_VALIDATING)
        interrupted_issue.comments.append(
            FakeComment(
                id=FOLLOWUP_COMMENT_ID,
                body="tweak the wording",
                user=FakeUser(HUMAN_LOGIN),
            )
        )
        interrupted_github.add_issue(interrupted_issue)
        # A stale hash forces `_detect_user_content_change` to report drift
        # and route into the user-content-change resume.
        interrupted_github.seed_state(
            8,
            user_content_hash="stale-hash-forces-drift",
            last_action_comment_id=PICKUP_COMMENT_ID,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=1,
            pr_number=SECONDARY_PR,
            branch=_issue_branch(8),
        )

        interrupted_patches = self._run_validating(
            interrupted_github,
            interrupted_issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                interrupted=True,
                last_message="partial drift fix before the shutdown SIGTERM",
            ),
            head_shas=[PRE_FIX_SHA],
        )

        # The dev resume DID run (so this exercises the post-resume guard),
        # but produced no commit and was killed.
        interrupted_patches[RUN_AGENT].assert_called_once()
        interrupted_patches[PUSH_BRANCH].assert_not_called()
        # Nothing persisted this tick: the seeded state stands untouched, so
        # the next tick re-detects the drift and retries the resume.
        self.assertEqual(interrupted_github.write_state_calls, 0)
        self.assertEqual(interrupted_github.label_history, [])
        interrupted_state = interrupted_github.pinned_data(8)
        self.assertEqual(interrupted_state.get(USER_CONTENT_HASH), "stale-hash-forces-drift")
        self.assertEqual(interrupted_state.get(LAST_ACTION_COMMENT_ID), PICKUP_COMMENT_ID)

    def test_human_interrupted_resume_not_persisted(self) -> None:
        interrupted_github = FakeGitHubClient()
        interrupted_issue = make_issue(9, label=LABEL_VALIDATING)
        interrupted_issue.comments.append(
            FakeComment(
                id=HUMAN_RETRY_COMMENT_ID,
                body="please retry",
                user=FakeUser(HUMAN_LOGIN),
            )
        )
        interrupted_github.add_issue(interrupted_issue)
        # Seed a matching content hash so `_detect_user_content_change`
        # returns None (no drift, no first-call persist) and the handler
        # reaches the awaiting-human resume path cleanly.
        prior_hash = workflow._compute_user_content_hash(interrupted_issue, set())
        interrupted_github.seed_state(
            9,
            awaiting_human=True,
            last_action_comment_id=1000,
            user_content_hash=prior_hash,
            dev_agent=BACKEND_CLAUDE,
            dev_session_id=DEV_SESSION,
            review_round=1,
            pr_number=INTERRUPTED_RESUME_PR,
            branch=_issue_branch(9),
        )

        interrupted_patches = self._run_validating(
            interrupted_github,
            interrupted_issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                interrupted=True,
                last_message="partial fix before the shutdown SIGTERM",
            ),
            head_shas=[PRE_FIX_SHA],
        )

        interrupted_patches[RUN_AGENT].assert_called_once()
        interrupted_patches[PUSH_BRANCH].assert_not_called()
        # Nothing persisted: the park stays put and the human reply is
        # re-consumed next tick against a fresh dev session.
        self.assertEqual(interrupted_github.write_state_calls, 0)
        interrupted_state = interrupted_github.pinned_data(9)
        self.assertTrue(interrupted_state.get(AWAITING_HUMAN))
        self.assertEqual(interrupted_state.get(LAST_ACTION_COMMENT_ID), 1000)


class HandleValidatingResumeTrustFilterTest(
    unittest.TestCase,
    _ResumeTrustFixtureMixin,
):
    """Allow only trusted authors to drive parked validating resumes."""

    def test_outsider_add_rounds_ignored(self) -> None:
        trust_github = FakeGitHubClient()
        trust_issue = self._seed_cap_park(
            trust_github,
            author="mallory",
            body="/orchestrator add-review-rounds 5",
        )
        with patch.object(config, ALLOWED_AUTHORS_SETTING, ALLOWED_AUTHORS):
            trust_patches = self._run_validating(
                trust_github,
                trust_issue,
                run_agent=_agent(),
            )
        # The outsider's command never reaches the parser: no reviewer rerun,
        # the cap and park stay put, and the watermark is not advanced past the
        # outsider comment so a later trusted command is still seen.
        trust_patches[RUN_AGENT].assert_not_called()
        trust_state = trust_github.pinned_data(TRUST_CAP_ISSUE)
        self.assertTrue(trust_state.get(AWAITING_HUMAN))
        self.assertEqual(trust_state.get(PARK_REASON), REVIEW_CAP)
        self.assertEqual(trust_state.get(REVIEW_ROUND), config.MAX_REVIEW_ROUNDS)
        self.assertEqual(trust_state.get(LAST_ACTION_COMMENT_ID), ACTION_COMMENT_ID)
        self.assertFalse(
            any(
                CAP_RESET_MESSAGE in body
                for _, body in trust_github.posted_comments
            )
        )

    def test_add_review_rounds_command_honored(
        self,
    ) -> None:
        trust_github = FakeGitHubClient()
        trust_issue = self._seed_cap_park(
            trust_github,
            author="geserdugarov",
            body=ADD_ONE_ROUND_COMMAND,
        )
        with patch.object(config, ALLOWED_AUTHORS_SETTING, ALLOWED_AUTHORS):
            trust_patches = self._run_validating(
                trust_github,
                trust_issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=[BEFORE_FIX_SHA],
            )
        # A trusted operator's command resets the cap and reruns the reviewer
        # exactly as with no allowlist configured.
        trust_state = trust_github.pinned_data(TRUST_CAP_ISSUE)
        self.assertFalse(trust_state.get(AWAITING_HUMAN))
        self.assertIsNone(trust_state.get(PARK_REASON))
        self.assertEqual(
            trust_state.get(REVIEW_ROUND),
            config.MAX_REVIEW_ROUNDS - 1,
        )
        self.assertEqual(trust_state.get(LAST_ACTION_COMMENT_ID), HUMAN_COMMENT_ID)
        self.assertEqual(trust_patches[RUN_AGENT].call_count, 1)
        self.assertTrue(
            any(
                CAP_RESET_MESSAGE in body
                for _, body in trust_github.posted_comments
            )
        )

    def test_outsider_retry_does_not_respawn(self) -> None:
        trust_github = FakeGitHubClient()
        trust_issue = self._seed_reviewer_timeout_park(trust_github, author="mallory")
        with patch.object(config, ALLOWED_AUTHORS_SETTING, ALLOWED_AUTHORS):
            trust_patches = self._run_validating(
                trust_github,
                trust_issue,
                run_agent=_agent(),
            )
        # Filtered to empty, so the reviewer_timeout park self-heals through the
        # transient-recovery branch (as on a no-comment tick) instead of the
        # outsider's nudge waking the reviewer.
        trust_patches[RUN_AGENT].assert_not_called()

    def test_trusted_retry_respawns_reviewer(
        self,
    ) -> None:
        trust_github = FakeGitHubClient()
        trust_issue = self._seed_reviewer_timeout_park(trust_github, author="geserdugarov")
        with patch.object(config, ALLOWED_AUTHORS_SETTING, ALLOWED_AUTHORS):
            trust_patches = self._run_validating(
                trust_github,
                trust_issue,
                run_agent=_agent(last_message=REVIEW_APPROVED_MESSAGE),
                head_shas=[BEFORE_FIX_SHA],
            )
        # The trusted nudge re-spawns the reviewer this tick and advances the
        # watermark past the consumed comment.
        self.assertEqual(trust_patches[RUN_AGENT].call_count, 1)
        reviewer_spawns = [
            event
            for event in trust_github.recorded_events
            if event[EVENT_NAME] == EVENT_AGENT_SPAWN and event.get(AGENT_ROLE) == ROLE_REVIEWER
        ]
        self.assertEqual(len(reviewer_spawns), 1)
        self.assertEqual(trust_github.pinned_data(TRUST_RETRY_ISSUE).get(LAST_ACTION_COMMENT_ID), FOLLOWUP_COMMENT_ID)


if __name__ == "__main__":
    unittest.main()
