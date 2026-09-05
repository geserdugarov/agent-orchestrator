# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pinned-state writes, notices, and audit events on the `persistence` owner."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator.git import commands
from orchestrator.git.base_sync import persistence
from orchestrator.workflow.engine import comments
from tests.git.base_sync import base_sync_helpers as fixtures
from tests.git.base_sync.base_sync_helpers import _OrderedCall, _recorded_calls

PARK_MESSAGE = "@human the rebase could not finalize"

RESET_ARGS = ("reset", "--hard", fixtures.PRE_REBASE_SHA)

CLEAN_ARGS = ("clean", "-fd")

RECOVERY_METHOD = "crash_recovery_pushed"

REBASED_EVENT = "base_rebased"

NOTICE = ":mag: Recovered an interrupted auto-rebase"

POST_PR_COMMENT = "_post_pr_comment"

RETRY_COMMENT_ID = 200

TRANSIENT_PARK_REASON = "unmergeable"

EVENT_FIELD = "event"

ISSUE_FIELD = "issue"

REASON_FIELD = "reason"

PARK_EVENT = "park_awaiting_human"

STAGE_ENTER_EVENT = "stage_enter"

# The client calls whose order is the contract these owners publish through.
ISSUE_COMMENT = "comment"

PR_COMMENT = "pr_comment"

EMIT_EVENT = "emit_event"

SET_LABEL = "set_workflow_label"

WRITE_STATE = "write_pinned_state"


class ParkAutoRebaseFailureTest(unittest.TestCase):
    """`_park_auto_rebase_failure` parks with a durable, recognizable reason."""

    def test_park_lands_on_the_issue_thread(self) -> None:
        context = fixtures._recovery_context()
        ordered: list[str] = []

        with _recorded_calls(
            ordered, context.gh, ISSUE_COMMENT, EMIT_EVENT, WRITE_STATE,
        ):
            persistence._park_auto_rebase_failure(
                context.gh,
                context.issue,
                context.state,
                message=PARK_MESSAGE,
                reason=fixtures.PARK_PUSH_FAILED,
            )

        # The pinned-state write commits the park: the HITL comment and the
        # audit event both land while the durable state still says unparked.
        self.assertEqual(
            ordered,
            [ISSUE_COMMENT, f"{EMIT_EVENT}:{PARK_EVENT}", WRITE_STATE],
        )
        published = context.gh.pinned_data(fixtures.ISSUE)
        self.assertTrue(published.get(fixtures.KEY_AWAITING_HUMAN))
        # `_park_awaiting_human` clears `park_reason` by contract; the reason
        # has to survive that, because the refresh-time retry scan keys off it
        # to tell an auto-rebase park from every other awaiting-human park.
        self.assertEqual(
            published.get(fixtures.KEY_PARK_REASON), fixtures.PARK_PUSH_FAILED,
        )
        self.assertEqual(
            context.gh.recorded_events[-1].get(REASON_FIELD),
            fixtures.PARK_PUSH_FAILED,
        )
        # The message goes to the issue thread, not the PR -- the
        # resume-on-human-reply scan only reads the issue.
        issue_number, body = context.gh.posted_comments[-1]
        self.assertEqual(issue_number, fixtures.ISSUE)
        self.assertIn(PARK_MESSAGE, body)
        self.assertEqual(context.gh.posted_pr_comments, [])

    def test_reason_outside_the_park_set_is_refused(self) -> None:
        context = fixtures._recovery_context()

        with self.assertRaises(AssertionError):
            persistence._park_auto_rebase_failure(
                context.gh,
                context.issue,
                context.state,
                message=PARK_MESSAGE,
                reason="reviewer_timeout",
            )


class ResetClearAndParkTest(unittest.TestCase):
    """`_reset_clear_and_park` restores HEAD, drops the anchor, then parks."""

    def test_reset_targets_the_anchor_and_clears_it(self) -> None:
        context, hardened, ordered = self._reset_and_park()

        self.assertEqual(hardened.call_args.args, RESET_ARGS)
        self.assertEqual(
            hardened.call_args.kwargs.get("cwd"), fixtures.WORKTREE,
        )
        # HEAD is restored before anything is published, and the pinned-state
        # write is last: the HITL comment and the audit event both describe a
        # worktree already back on the anchor.
        self.assertEqual(
            ordered,
            [
                f"{fixtures.GIT_HARDENED}:reset",
                ISSUE_COMMENT,
                f"{EMIT_EVENT}:{PARK_EVENT}",
                WRITE_STATE,
            ],
        )
        published = context.gh.pinned_data(fixtures.ISSUE)
        # The reset put HEAD back on the anchor, so leaving it pinned would
        # only make a later tick re-enter the "HEAD == anchor" no-op case.
        self.assertIsNone(published.get(fixtures.KEY_PENDING_PUSH_SHA))
        self.assertTrue(published.get(fixtures.KEY_AWAITING_HUMAN))
        self.assertEqual(
            published.get(fixtures.KEY_PARK_REASON), fixtures.PARK_PUSH_FAILED,
        )

    def test_clean_discards_leftovers_after_the_reset(self) -> None:
        context, hardened, _ = self._reset_and_park(
            clean=True, reason=fixtures.PARK_DIRTY,
        )

        self.assertEqual(
            [recorded.args for recorded in hardened.call_args_list],
            [RESET_ARGS, CLEAN_ARGS],
        )
        # Each park path carries its own reason all the way into the audit
        # event, so an operator can tell a dirty park from a push failure.
        self.assertEqual(
            context.gh.recorded_events[-1].get(REASON_FIELD),
            fixtures.PARK_DIRTY,
        )

    def test_failed_reset_still_parks(self) -> None:
        # The `awaiting_human` flag is what short-circuits the same-tick
        # handlers, so it has to land even when the worktree is left on an
        # unexpected SHA for the operator to inspect.
        failed = MagicMock(
            return_value=fixtures._git_result(
                returncode=fixtures.GIT_FAILURE_EXIT_CODE,
                stderr="fatal: bad object\n",
            ),
        )

        context, _, _ = self._reset_and_park(hardened=failed)

        published = context.gh.pinned_data(fixtures.ISSUE)
        self.assertTrue(published.get(fixtures.KEY_AWAITING_HUMAN))
        self.assertIsNone(published.get(fixtures.KEY_PENDING_PUSH_SHA))

    def _reset_and_park(
        self,
        *,
        hardened: MagicMock | None = None,
        clean: bool = False,
        reason: str = fixtures.PARK_PUSH_FAILED,
    ):
        context = fixtures._recovery_context(
            pending_auto_base_rebase_push_sha=fixtures.PRE_REBASE_SHA,
        )
        hardened = hardened or MagicMock(return_value=fixtures._git_result())
        ordered: list[str] = []
        recorder = _OrderedCall(ordered, fixtures.GIT_HARDENED, hardened)
        with _recorded_calls(
            ordered, context.gh, ISSUE_COMMENT, EMIT_EVENT, WRITE_STATE,
        ), patch.object(commands, fixtures.GIT_HARDENED, recorder):
            persistence._reset_clear_and_park(
                context,
                fixtures.PRE_REBASE_SHA,
                message=PARK_MESSAGE,
                reason=reason,
                clean=clean,
            )
        return context, hardened, ordered


class PrepareRecoveredRebaseStateTest(unittest.TestCase):
    """`_prepare_recovered_rebase_state` stages what a resumed rebase needs."""

    def test_consumed_retry_unparks_the_issue(self) -> None:
        context = fixtures._recovery_context(
            unparking_consumed_max=RETRY_COMMENT_ID,
            awaiting_human=True,
            park_reason=fixtures.PARK_PUSH_FAILED,
            review_round=3,
            pending_auto_base_rebase_push_sha=fixtures.PRE_REBASE_SHA,
        )

        persistence._prepare_recovered_rebase_state(context)

        self.assertEqual(
            context.state.get(fixtures.KEY_LAST_ACTION_COMMENT_ID),
            RETRY_COMMENT_ID,
        )
        self.assertFalse(context.state.get(fixtures.KEY_AWAITING_HUMAN))
        self.assertIsNone(context.state.get(fixtures.KEY_PARK_REASON))
        self._assert_anchor_and_round_reset(context)

    def test_recovery_without_a_retry_keeps_the_park(self) -> None:
        # A crash recovery nobody asked for must not silently unpark an issue
        # someone else parked; only the anchor and the review round reset.
        context = fixtures._recovery_context(
            awaiting_human=True,
            park_reason=TRANSIENT_PARK_REASON,
            pending_auto_base_rebase_push_sha=fixtures.PRE_REBASE_SHA,
        )

        persistence._prepare_recovered_rebase_state(context)

        self.assertTrue(context.state.get(fixtures.KEY_AWAITING_HUMAN))
        self.assertEqual(
            context.state.get(fixtures.KEY_PARK_REASON), TRANSIENT_PARK_REASON,
        )
        self._assert_anchor_and_round_reset(context)

    def _assert_anchor_and_round_reset(self, context) -> None:
        self.assertIsNone(context.state.get(fixtures.KEY_PENDING_PUSH_SHA))
        self.assertEqual(context.state.get(fixtures.KEY_REVIEW_ROUND), 0)


class PostRecoveredRebaseNoticeTest(unittest.TestCase):
    """`_post_recovered_rebase_notice` never blocks the state it precedes."""

    def test_notice_lands_on_the_pr(self) -> None:
        context = fixtures._recovery_context()

        persistence._post_recovered_rebase_notice(context, NOTICE)

        pr_number, body = context.gh.posted_pr_comments[-1]
        self.assertEqual(pr_number, fixtures.PR_NUMBER)
        self.assertIn(NOTICE, body)

    def test_failed_notice_is_swallowed(self) -> None:
        context = fixtures._recovery_context()
        raising = MagicMock(side_effect=RuntimeError("GitHub is down"))

        with patch.object(comments, POST_PR_COMMENT, raising):
            persistence._post_recovered_rebase_notice(context, NOTICE)

        raising.assert_called_once()


class EmitRecoveredRebaseEventTest(unittest.TestCase):
    """`_emit_recovered_rebase_event` keeps the audit shape stable."""

    def test_event_carries_the_head_and_method(self) -> None:
        context = fixtures._recovery_context(retry_count=2)

        persistence._emit_recovered_rebase_event(
            context, fixtures.RECOVERED_SHA, RECOVERY_METHOD,
        )

        emitted = context.gh.recorded_events[-1]
        self.assertEqual(
            {
                field: emitted.get(field)
                for field in (
                    EVENT_FIELD, ISSUE_FIELD, "stage", "pr_number",
                    "sha", "method", "review_round", "retry_count",
                )
            },
            {
                EVENT_FIELD: REBASED_EVENT,
                ISSUE_FIELD: fixtures.ISSUE,
                "stage": fixtures.LABEL,
                "pr_number": fixtures.PR_NUMBER,
                "sha": fixtures.RECOVERED_SHA,
                "method": RECOVERY_METHOD,
                "review_round": 0,
                "retry_count": 2,
            },
        )


class RouteRecoveredRebaseTest(unittest.TestCase):
    """`_route_recovered_rebase` relabels only a head that is current."""

    def test_current_head_routes_to_validating(self) -> None:
        context = fixtures._recovery_context(behind=0)
        ordered: list[str] = []

        with _recorded_calls(ordered, context.gh, SET_LABEL, WRITE_STATE):
            routed = persistence._route_recovered_rebase(
                context, fixtures.RECOVERED_SHA, RECOVERY_METHOD,
            )

        self.assertTrue(routed)
        self.assertIn(
            (fixtures.ISSUE, "workflow:validating"), context.gh.label_history,
        )
        # The relabel precedes the write, so a tick that dies between them
        # leaves the anchor pinned and the next tick redoes this recovery.
        self.assertEqual(ordered, [SET_LABEL, WRITE_STATE])
        self.assertEqual(context.gh.write_state_calls, 1)

    def test_lagging_head_persists_without_relabeling(self) -> None:
        # Base advanced again while the rebase was interrupted, so the caller
        # falls back through to the normal rebase + push flow this same tick.
        context = fixtures._recovery_context(behind=2)

        routed = persistence._route_recovered_rebase(
            context, fixtures.RECOVERED_SHA, RECOVERY_METHOD,
        )

        self.assertFalse(routed)
        self.assertEqual(context.gh.label_history, [])
        self.assertEqual(context.gh.write_state_calls, 1)


class FinalizeRecoveredRebaseTest(unittest.TestCase):
    """`_finalize_recovered_rebase` publishes every surface, then routes."""

    def test_finalize_writes_every_surface(self) -> None:
        context = fixtures._recovery_context(
            behind=0,
            review_round=3,
            pending_auto_base_rebase_push_sha=fixtures.PRE_REBASE_SHA,
        )
        ordered: list[str] = []

        with _recorded_calls(
            ordered, context.gh, PR_COMMENT, EMIT_EVENT, SET_LABEL, WRITE_STATE,
        ):
            routed = persistence._finalize_recovered_rebase(
                context,
                local_head=fixtures.RECOVERED_SHA,
                method=RECOVERY_METHOD,
                notice=NOTICE,
            )

        self.assertTrue(routed)
        # Notice and audit event first, then the write that records the
        # announcement while the anchor still stands, then the relabel and the
        # single write that commits the anchor clear and the review-round
        # reset. A tick lost between the two writes comes back to a finish
        # that says it announced itself, and makes only the write it never
        # made rather than saying all of it again.
        self.assertEqual(
            ordered,
            [
                PR_COMMENT,
                f"{EMIT_EVENT}:{REBASED_EVENT}",
                WRITE_STATE,
                SET_LABEL,
                f"{EMIT_EVENT}:{STAGE_ENTER_EVENT}",
                WRITE_STATE,
            ],
        )
        self.assertIn(NOTICE, context.gh.posted_pr_comments[-1][1])
        published = context.gh.pinned_data(fixtures.ISSUE)
        self.assertIsNone(published.get(fixtures.KEY_PENDING_PUSH_SHA))
        self.assertEqual(published.get(fixtures.KEY_REVIEW_ROUND), 0)


if __name__ == "__main__":
    unittest.main()
