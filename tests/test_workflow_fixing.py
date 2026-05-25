# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for `_handle_fixing` (PR-feedback quiet-window + dev-resume loop).

`fixing` is entered by `_handle_in_review` when fresh PR feedback lands on
any of the four comment surfaces. The fixing handler rescans the
existing in_review watermarks each tick, debounces the quiet window
against the newest comment timestamp, resumes the locked dev session via
`_resume_dev_with_text` once the window expires, advances watermarks
past the consumed feedback, and on a pushed fix flips the label to
`documenting` (NOT directly to `validating`) with `review_round=0` and
a cleared `agent_approved_sha`. Routing through `documenting` mirrors
the validating-side pushed-fix exits and gives the docs pass a chance
to refresh any README / docs / plans touched by the fix before the
reviewer agent sees the new diff. The no-new-feedback bounce is the
one fixing exit that still flips directly to `validating` -- with no
fix work landed, the docs hop has nothing to do.

The PR-terminal arcs (merged / closed / open-PR-with-closed-issue),
dispatcher routing, label-bookkeeping, and missing-`pr_number` park
are covered in `tests/test_workflow.py`'s `FixingLabelRoutingTest`.
"""
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


class HandleFixingTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Cover the fixing handler against debounce expiry, dev resume/push,
    watermark advancement, and comments arriving while already labeled
    `fixing`.
    """

    PR_NUMBER = 880
    BRANCH = "orchestrator/issue-880"

    def _seed(
        self,
        *,
        issue_number: int = 880,
        pr=None,
        issue_comments=(),
        with_pr_number: bool = True,
        extra_state=None,
    ):
        gh = FakeGitHubClient()
        issue = make_issue(issue_number, label="fixing")
        for c in issue_comments:
            issue.comments.append(c)
        gh.add_issue(issue)
        if pr is not None:
            gh.add_pr(pr)
        state: dict = {
            "branch": self.BRANCH,
            "dev_agent": "claude",
            "dev_session_id": "dev-sess",
            "review_round": 1,
            "agent_approved_sha": "cafe1234",
            "pr_last_comment_id": 1999,
            "pr_last_review_comment_id": 0,
            "pr_last_review_summary_id": 0,
            "pending_fix_at": "2026-05-24T00:00:00+00:00",
            "pending_fix_issue_max_id": 2000,
        }
        if with_pr_number and pr is not None:
            state["pr_number"] = pr.number
        if extra_state:
            state.update(extra_state)
        gh.seed_state(issue_number, **state)
        return gh, issue

    def _open_pr(self, **kwargs):
        defaults = dict(
            number=self.PR_NUMBER,
            head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True,
            check_state="success",
        )
        defaults.update(kwargs)
        return FakePR(**defaults)

    # --- debounce expiry --------------------------------------------------

    def test_fixing_within_debounce_window_does_not_resume(self) -> None:
        # Triggering comment is fresh (created `now`); IN_REVIEW_DEBOUNCE_SECONDS
        # has not elapsed, so the handler must NOT resume the dev. No agent
        # spawn, no label change, watermarks untouched.
        now = datetime.now(timezone.utc)
        comment = FakeComment(
            id=2000, body="please tighten the docstring",
            user=FakeUser("alice"), created_at=now,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[comment])

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        # Watermark not advanced past the triggering comment yet.
        self.assertEqual(gh.pinned_data(880).get("pr_last_comment_id"), 1999)
        self.assertFalse(gh.pinned_data(880).get("awaiting_human"))

    def test_fixing_past_debounce_resumes_dev(self) -> None:
        # Triggering comment is older than the debounce window; the handler
        # builds a `_build_pr_comment_followup` prompt and resumes the dev
        # via `_resume_dev_with_text`.
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="rename foo to bar",
            user=FakeUser("alice"), created_at=old,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[comment])

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="pushed fix",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        mocks["run_agent"].assert_called_once()
        call_args = mocks["run_agent"].call_args
        # `run_agent(backend, prompt, cwd, **kwargs)`.
        backend = call_args.args[0]
        prompt = call_args.args[1]
        # Followup prompt quotes the human's comment so the dev sees what
        # to fix.
        self.assertIn("rename foo to bar", prompt)
        self.assertIn("PR comments", prompt)
        # Dev session resumed (not a fresh spawn) on the locked backend.
        self.assertEqual(
            call_args.kwargs.get("resume_session_id"), "dev-sess",
        )
        self.assertEqual(backend, "claude")

    # --- newer comments extend the debounce window ------------------------

    def test_newer_comment_extends_debounce_window(self) -> None:
        # First tick: an older triggering comment (id=2000) is past the
        # window but a newer comment (id=2001) just landed -- the freshest
        # timestamp resets the gate. Handler must NOT resume; no agent
        # call, no label change.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        just_now = datetime.now(timezone.utc)
        triggering = FakeComment(
            id=2000, body="please fix the bug",
            user=FakeUser("alice"), created_at=long_ago,
        )
        followup = FakeComment(
            id=2001, body="actually rename it too",
            user=FakeUser("alice"), created_at=just_now,
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr, issue_comments=[triggering, followup],
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.label_history, [])

    # --- comments arriving while already labeled fixing -------------------

    def test_fresh_comment_during_fixing_is_picked_up_on_next_tick(
        self,
    ) -> None:
        # Tick 1 (in_review handoff already done; we simulate that state):
        # the triggering comment id=2000 sits past the watermark with the
        # bookmark recorded. Before tick 2 fires, a SECOND human comment
        # id=2001 lands. The rescan picks BOTH up and the followup quotes
        # both surfaces. Both comments are past the debounce window.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        also_old = datetime.now(timezone.utc) - timedelta(minutes=30)
        triggering = FakeComment(
            id=2000, body="please fix the docstring",
            user=FakeUser("alice"), created_at=long_ago,
        )
        late_arrival = FakeComment(
            id=2001, body="and rename helper to util",
            user=FakeUser("bob"), created_at=also_old,
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr, issue_comments=[triggering, late_arrival],
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="pushed",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args.args[1]
        # Both comments are quoted in the followup so the dev sees the
        # full conversation that landed while the label was `fixing`.
        self.assertIn("please fix the docstring", prompt)
        self.assertIn("and rename helper to util", prompt)
        # Watermark advanced past BOTH consumed comments.
        self.assertGreaterEqual(
            gh.pinned_data(880).get("pr_last_comment_id"), 2001,
        )

    # --- dev resume + push --> flip to documenting -----------------------

    def test_pushed_fix_flips_to_documenting_with_reset_state(self) -> None:
        # A pushed fix routes through `documenting` (NOT directly to
        # `validating`) so the docs pass runs against the new head before
        # the reviewer agent re-evaluates. The dev fix may have touched
        # code that needs a README / docs update, and skipping
        # documenting here would let the reviewer approve a commit whose
        # docs the documenting handler has not had a chance to refresh.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please address the typo",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[comment])

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="fixed",
                ),
                head_shas=("sha-before", "sha-after"),
                push_branch=True,
            )

        # Dev pushed; label flipped to documenting.
        mocks["_push_branch"].assert_called_once()
        self.assertIn((880, "documenting"), gh.label_history)
        # And NOT directly to validating -- documenting must run first.
        self.assertNotIn((880, "validating"), gh.label_history)
        data = gh.pinned_data(880)
        # Review round reset so validating starts fresh on the new diff
        # after documenting hands off.
        self.assertEqual(data.get("review_round"), 0)
        # Stale agent approval dropped (the head just moved).
        self.assertIsNone(data.get("agent_approved_sha"))
        # Bookmarks cleared after consumption.
        self.assertIsNone(data.get("pending_fix_at"))
        self.assertIsNone(data.get("pending_fix_issue_max_id"))
        # Watermark advanced past the consumed comment.
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 2000)

    def test_dev_timeout_parks_and_advances_watermarks(self) -> None:
        # On dev timeout `_handle_dev_fix_result` parks awaiting human.
        # The fixing handler still advances the in_review watermarks past
        # the consumed feedback so the next tick does not replay it and
        # busy-loop the dev on the same comment.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please fix",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[comment])

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(timed_out=True),
                head_shas=("sha-before",),
            )

        data = gh.pinned_data(880)
        self.assertTrue(data.get("awaiting_human"))
        # Watermark advanced even though no fix landed -- the dev saw
        # the feedback via the resume prompt.
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 2000)
        # Did NOT advance into either downstream stage; stays in fixing
        # for the operator.
        self.assertNotIn((880, "documenting"), gh.label_history)
        self.assertNotIn((880, "validating"), gh.label_history)

    # --- watermark advancement across all three surfaces ----------------

    def test_pushed_fix_advances_all_three_watermarks(self) -> None:
        # Feedback lands on three surfaces simultaneously: an issue
        # comment, an inline review comment, and a review summary.
        # After a pushed fix every watermark must move past the max id
        # consumed on that surface.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue_comment = FakeComment(
            id=2000, body="rename foo",
            user=FakeUser("alice"), created_at=long_ago,
        )
        inline_comment = FakeComment(
            id=3000, body="add a test for this branch",
            user=FakeUser("bob"), created_at=long_ago,
        )
        summary_review = FakePRReview(
            id=4000, body="please update the doc string",
            state="CHANGES_REQUESTED",
            user=FakeUser("carol"), submitted_at=long_ago,
        )
        pr = self._open_pr(
            review_comments=[inline_comment],
            reviews=[summary_review],
        )
        gh, issue = self._seed(
            pr=pr, issue_comments=[issue_comment],
            extra_state={
                "pr_last_review_comment_id": 2999,
                "pr_last_review_summary_id": 3999,
                "pending_fix_review_max_id": 3000,
                "pending_fix_review_summary_max_id": 4000,
            },
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="pushed",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        mocks["_push_branch"].assert_called_once()
        self.assertIn((880, "documenting"), gh.label_history)
        data = gh.pinned_data(880)
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 2000)
        self.assertEqual(data.get("pr_last_review_comment_id"), 3000)
        self.assertEqual(data.get("pr_last_review_summary_id"), 4000)
        # Prompt also quoted every surface.
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("rename foo", prompt)
        self.assertIn("add a test for this branch", prompt)
        self.assertIn("please update the doc string", prompt)

    def test_consumed_issue_comment_refreshes_user_content_hash(
        self,
    ) -> None:
        # When fixing feeds a fresh issue-thread comment to the dev,
        # the next tick's `_handle_documenting` (and the eventual
        # `_handle_validating` once docs hands off) would otherwise
        # see the same comment as user-content drift (the hash covers
        # title + body + human issue-thread comments) and resume the
        # dev a second time on input it already handled. The hash
        # must advance with the consumption so the documenting /
        # validating drift checks are no-ops on the next tick.
        from orchestrator.workflow_drift import _compute_user_content_hash
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please fix the docstring",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr, issue_comments=[comment],
            extra_state={
                # Stale hash from before the human comment landed.
                "user_content_hash": "stale-hash-pre-comment",
            },
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="pushed",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        data = gh.pinned_data(880)
        # Pushed successfully, flipped to documenting (the docs pass
        # runs before the reviewer agent re-evaluates).
        self.assertIn((880, "documenting"), gh.label_history)
        # The stored hash matches the current computed hash, i.e.
        # the documenting / validating tick's
        # `_detect_user_content_change` will be a no-op rather than
        # re-resuming the dev on the already-consumed comment.
        from orchestrator.workflow_messages import _orchestrator_ids
        expected = _compute_user_content_hash(
            issue,
            _orchestrator_ids(
                workflow.PinnedState(data=dict(data)),
            ),
        )
        self.assertEqual(data.get("user_content_hash"), expected)
        self.assertNotEqual(
            data.get("user_content_hash"), "stale-hash-pre-comment",
        )

    def test_failed_fix_also_refreshes_user_content_hash(self) -> None:
        # Symmetric guard for the failure path: the dev saw the
        # comment via the resume prompt even when the push failed,
        # so the hash baseline must move with the consumption.
        # Otherwise a later relabel out of `fixing` into a stage
        # that consults `_detect_user_content_change` would re-fire
        # on the same comment.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please address the typo",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr, issue_comments=[comment],
            extra_state={"user_content_hash": "stale-hash-pre-comment"},
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(timed_out=True),
                head_shas=("sha-before",),
            )

        data = gh.pinned_data(880)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotEqual(
            data.get("user_content_hash"), "stale-hash-pre-comment",
        )

    def test_pushed_fix_bump_does_not_swallow_concurrent_human_comment(
        self,
    ) -> None:
        # Race window: a human posts an issue-thread comment AFTER the
        # handler's rescan but BEFORE the post-push watermark advance.
        # The pushed-fix bump MUST NOT leap past the unseen comment;
        # otherwise the next in_review tick (after validating completes)
        # would skip the feedback and AUTO_MERGE could land the PR over
        # it. The legacy in_review pushed-fix path had the same
        # constraint and advanced only to comments actually fed to the
        # dev.
        from unittest.mock import patch as _patch_mock
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        triggering = FakeComment(
            id=2000, body="please fix the bug",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[triggering])

        # Splice in a concurrent human comment with id higher than the
        # triggering one mid-handler so the bump's `latest_comment_id`
        # candidate would otherwise leap past it.
        concurrent = FakeComment(
            id=2500, body="actually also rename helper",
            user=FakeUser("bob"), created_at=long_ago,
        )
        original_handle_fix_result = workflow._handle_dev_fix_result

        def push_then_inject(*args, **kwargs):
            result = original_handle_fix_result(*args, **kwargs)
            issue.comments.append(concurrent)
            return result

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600), \
             _patch_mock.object(
                 workflow, "_handle_dev_fix_result", push_then_inject,
             ):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="pushed",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        data = gh.pinned_data(880)
        # Label flipped to documenting (push succeeded; docs pass runs
        # before the reviewer agent re-evaluates).
        self.assertIn((880, "documenting"), gh.label_history)
        # Watermark advanced past the consumed triggering comment but
        # NOT past the concurrent one -- the next in_review tick must
        # still see id=2500 as fresh feedback.
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 2000)
        self.assertLess(data.get("pr_last_comment_id"), 2500)

    def test_failed_fix_bump_does_not_swallow_concurrent_human_comment(
        self,
    ) -> None:
        # Symmetric guard for the failure path: a human posts an
        # issue-thread comment AFTER the rescan but BEFORE the
        # post-park watermark advance. The bump MUST NOT leap past it;
        # otherwise the next fixing tick sees `awaiting_human` with no
        # new feedback, the gate fires, and the human's comment is
        # silently dropped. Verifies the "comments arriving while
        # already labeled `fixing`" contract on the timeout/dirty/push-
        # fail paths, mirroring the success-path guard above.
        from unittest.mock import patch as _patch_mock
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        triggering = FakeComment(
            id=2000, body="please fix the bug",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[triggering])

        concurrent = FakeComment(
            id=2500, body="actually also rename helper",
            user=FakeUser("bob"), created_at=long_ago,
        )
        original_handle_fix_result = workflow._handle_dev_fix_result

        def timeout_then_inject(*args, **kwargs):
            # `_handle_dev_fix_result` on a timed-out agent posts the
            # park comment and returns False. Splice the concurrent
            # human comment in AFTER that post but BEFORE the handler
            # advances the watermark.
            result = original_handle_fix_result(*args, **kwargs)
            issue.comments.append(concurrent)
            return result

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600), \
             _patch_mock.object(
                 workflow, "_handle_dev_fix_result", timeout_then_inject,
             ):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(timed_out=True),
                head_shas=("sha-before",),
            )

        data = gh.pinned_data(880)
        # Parked awaiting human (timeout failure).
        self.assertTrue(data.get("awaiting_human"))
        # Watermark advanced past the consumed triggering comment but
        # NOT past the concurrent one -- the next fixing tick must
        # still see id=2500 as fresh feedback.
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 2000)
        self.assertLess(data.get("pr_last_comment_id"), 2500)

        # Second tick: rescan picks up the concurrent comment so
        # `awaiting_human and not new_feedback` is False; park flags
        # clear and the dev resumes with the human's text. Use a
        # successful agent result this time so the second tick
        # produces a push and we can assert the flow recovered.
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="pushed",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        mocks["run_agent"].assert_called_once()
        # The concurrent comment IS quoted in the next dev resume.
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("actually also rename helper", prompt)

    # --- awaiting-human gate (parked from prior failed resume) ----------

    def test_awaiting_human_with_no_new_feedback_is_no_op(self) -> None:
        # After a prior failed tick parked the issue and bumped the
        # watermark past the original triggering comment, a poll with no
        # fresh human reply must be a no-op -- no agent spawn, no comment
        # post, no label change.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                "awaiting_human": True,
                "park_reason": "agent_timeout",
                "pr_last_comment_id": 2500,  # already past any old feedback
            },
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.label_history, [])

    def test_awaiting_human_with_fresh_reply_resumes_dev(self) -> None:
        # The human typed a reply after the park. The fresh comment is
        # past the bumped watermark and past the debounce window, so the
        # handler clears the park flags and resumes the dev with the
        # new context.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        reply = FakeComment(
            id=2600, body="actually try X instead",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr, issue_comments=[reply],
            extra_state={
                "awaiting_human": True,
                "park_reason": "agent_timeout",
                "pr_last_comment_id": 2500,
            },
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="pushed",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        mocks["run_agent"].assert_called_once()
        data = gh.pinned_data(880)
        # Park flags cleared (either by _resume_dev_with_text or after
        # the successful push). After a successful push we end up in
        # documenting so the docs pass runs against the new head before
        # the reviewer re-evaluates.
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertIn((880, "documenting"), gh.label_history)
        self.assertNotIn((880, "validating"), gh.label_history)

    # --- no unread feedback at all --------------------------------------

    def test_no_unread_feedback_bounces_back_to_validating(self) -> None:
        # Defensive recovery: if the rescan finds nothing (watermarks
        # already cover the bookmarks), there is no fix work to do.
        # Bounce the label DIRECTLY back to `validating` (NOT through
        # `documenting`) so the reviewer re-evaluates and the issue is
        # not stranded in `fixing`. Skipping the documenting hop here is
        # deliberate: there is no fix work and therefore no new commit
        # that would need a docs pass before re-validation.
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr,
            extra_state={
                # Watermark already past the recorded bookmark.
                "pr_last_comment_id": 5000,
                "pending_fix_issue_max_id": 4900,
            },
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertIn((880, "validating"), gh.label_history)
        # And NOT through documenting -- the no-feedback bounce is the
        # one fixing exit that skips the docs hop.
        self.assertNotIn((880, "documenting"), gh.label_history)
        data = gh.pinned_data(880)
        self.assertIsNone(data.get("pending_fix_at"))
        self.assertIsNone(data.get("pending_fix_issue_max_id"))

    # --- PR fetch failure bails this tick instead of crashing -----------

    def test_get_pr_failure_for_open_issue_bails_without_crash(self) -> None:
        # If `gh.get_pr` raises for an open `fixing` issue, the handler
        # used to fall through into the rescan with `pr=None` and crash
        # at `gh.pr_conversation_comments_after(pr, ...)`. The guard
        # should bail the tick gracefully so the next poll re-fetches.
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr)
        # Replace `get_pr` so the call raises. PyGithub-side failures
        # (rate limit, 5xx, network blip) are typically transient.
        original_get_pr = gh.get_pr

        def boom(_pr_number):
            raise RuntimeError("github api down")
        gh.get_pr = boom  # type: ignore[assignment]
        try:
            with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
                mocks = self._run(
                    lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                    run_agent=_agent(),
                )
        finally:
            gh.get_pr = original_get_pr  # type: ignore[assignment]

        # No agent spawn, no label change, no park comment -- just a
        # quiet bail so the next tick retries.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertFalse(gh.pinned_data(880).get("awaiting_human"))

    def test_missing_pr_last_comment_id_falls_back_to_last_action(
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
            id=500, body="some old discussion from implementing",
            user=FakeUser("alice"), created_at=long_ago,
        )
        triggering = FakeComment(
            id=2000, body="please rename foo",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr, issue_comments=[historical, triggering],
            extra_state={
                # No `pr_last_comment_id` at all -- the in_review
                # legacy migration did not run on this issue.
                "pr_last_comment_id": None,
                # But `last_action_comment_id` is set (a park comment
                # id from validating, say) and sits above the
                # historical comment.
                "last_action_comment_id": 1000,
            },
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="pushed",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        mocks["run_agent"].assert_called_once()
        prompt = mocks["run_agent"].call_args.args[1]
        # The triggering comment (id=2000) IS quoted -- it's past
        # the last_action_comment_id fallback floor.
        self.assertIn("please rename foo", prompt)
        # The historical comment (id=500) is NOT quoted -- it sits
        # below the fallback floor (1000) and must not be re-fed.
        self.assertNotIn("some old discussion from implementing", prompt)

    # --- orchestrator comments are filtered from the rescan -------------

    def test_orchestrator_park_comment_is_filtered_from_rescan(self) -> None:
        # A prior tick may have posted an orchestrator comment with id
        # past the watermark. The rescan filters orchestrator-authored
        # comments (by recorded id AND by hidden body marker) so a HITL
        # ping does not re-trigger a dev resume.
        from orchestrator.workflow_messages import _ORCH_COMMENT_MARKER
        orch_comment = FakeComment(
            id=2050,
            body=f":bell: ready for review/merge\n\n{_ORCH_COMMENT_MARKER}",
            user=FakeUser("orchestrator"),
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr, issue_comments=[orch_comment],
            extra_state={
                # Watermark already past the bookmark so the rescan
                # only sees the orchestrator-authored comment.
                "pr_last_comment_id": 2010,
                "pending_fix_issue_max_id": 2000,
            },
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # No new feedback -> bounce back to validating (rather than
        # treating the orchestrator's own comment as fresh feedback).
        self.assertIn((880, "validating"), gh.label_history)

    # --- crash/restart and failure-path coverage ------------------------

    def test_missing_dev_session_resumes_via_fresh_spawn(self) -> None:
        # `dev_session_id` may be absent on a `fixing` issue whose prior
        # dev session was dropped by the silent-park fallback, or on
        # legacy state that pre-dates session tracking. The fixing
        # handler MUST NOT park on missing-session: `_resume_dev_with_text`
        # treats `dev_sid=None` as the fresh-spawn case, so the dev
        # resumes correctly with the locked backend. Asserting fresh
        # spawn here pins the "resume correctly" half of the
        # crash/restart contract (the other half -- park on missing
        # `pr_number` -- is in `FixingLabelRoutingTest`).
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please tighten the test",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr, issue_comments=[comment],
            extra_state={"dev_session_id": None},
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="fresh-sess",
                    last_message="pushed fix",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        # The handler resumed with `resume_session_id=None` -- the locked
        # backend (`dev_agent=claude`) drives a fresh spawn rather than
        # parking on the missing session.
        mocks["run_agent"].assert_called_once()
        call_args = mocks["run_agent"].call_args
        self.assertIsNone(call_args.kwargs.get("resume_session_id"))
        # Did NOT park -- the issue made progress instead (advancing to
        # documenting; the docs pass runs before validating).
        data = gh.pinned_data(880)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIn((880, "documenting"), gh.label_history)
        self.assertNotIn((880, "validating"), gh.label_history)

    def test_push_failure_parks_in_fixing_with_transient_reason(self) -> None:
        # Push failure on the dev fix -> park awaiting_human in `fixing`
        # with the transient `push_failed` reason. The workflow label
        # MUST stay at `fixing` so the operator can see where the issue
        # is in the lifecycle; flipping to `documenting` (or directly to
        # `validating`) would imply the fix landed when it did not.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please address the typo",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[comment])

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="fixed",
                ),
                head_shas=("sha-before", "sha-after"),
                push_branch=False,
            )

        data = gh.pinned_data(880)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")
        # Label stayed at `fixing` -- no relabel to `documenting` or
        # `validating`.
        self.assertNotIn((880, "documenting"), gh.label_history)
        self.assertNotIn((880, "validating"), gh.label_history)
        # Watermark advanced past the consumed feedback so the next
        # fixing tick does not replay it on top of the park.
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 2000)

    def test_dirty_tree_parks_in_fixing(self) -> None:
        # Dev committed but left the tree dirty -> park (refuses to
        # push an incomplete branch). Label stays at `fixing`.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please rename helper",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[comment])

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="WIP",
                ),
                head_shas=("sha-before", "sha-after"),
                dirty_files=["orchestrator/foo.py"],
            )

        data = gh.pinned_data(880)
        self.assertTrue(data.get("awaiting_human"))
        # `_on_dirty_worktree` clears `park_reason` (terminal, needs
        # human reply); the audit event still records the reason.
        self.assertIsNone(data.get("park_reason"))
        self.assertNotIn((880, "documenting"), gh.label_history)
        self.assertNotIn((880, "validating"), gh.label_history)
        # Watermark advanced past the consumed feedback.
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 2000)

    def test_no_commit_question_parks_in_fixing(self) -> None:
        # Dev returned a clarifying question with no new commit. The
        # handler routes through `_on_question`, which parks
        # awaiting_human and posts the agent's text on the issue
        # thread. Label MUST stay at `fixing`.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please address the lint",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[comment])

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="Should I prefer ruff or black for this?",
                ),
                # No new commit: head_sha unchanged between before/after.
                head_shas=("sha-before", "sha-before"),
            )

        data = gh.pinned_data(880)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((880, "documenting"), gh.label_history)
        self.assertNotIn((880, "validating"), gh.label_history)
        # Agent's question was surfaced to the human.
        joined = "\n".join(b for _, b in gh.posted_comments)
        self.assertIn("Should I prefer ruff or black for this?", joined)

    def test_agent_silent_failure_parks_in_fixing(self) -> None:
        # Dev returned empty `last_message` and no commit. The handler
        # routes through `_on_question`'s silent-failure branch, parks
        # with `park_reason="agent_silent"`, and the silent-park
        # counter ticks so a future resume can drop a poisoned session.
        # Label MUST stay at `fixing`.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please fix the import order",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(pr=pr, issue_comments=[comment])

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="",
                    exit_code=1,
                ),
                head_shas=("sha-before", "sha-before"),
            )

        data = gh.pinned_data(880)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "agent_silent")
        self.assertNotIn((880, "documenting"), gh.label_history)
        self.assertNotIn((880, "validating"), gh.label_history)
        # Silent-park streak counter ticked so the next resume can
        # drop the poisoned session after the configured threshold.
        self.assertGreaterEqual(
            int(data.get("silent_park_count") or 0), 1,
        )

    def test_restart_with_pending_feedback_resumes_from_watermarks(
        self,
    ) -> None:
        # Crash/restart contract: the orchestrator has no in-memory
        # state across ticks, so a `fixing` issue with pending feedback
        # in pinned state must drive the rescan entirely off the
        # persisted watermarks + bookmarks. Simulate it by leaving the
        # `pending_fix_*` bookmarks recorded by a prior in_review tick
        # but starting with no transient state (no `awaiting_human`,
        # no in-flight session); the rescan finds the triggering
        # comment past `pr_last_comment_id`, debounce expires, and the
        # dev resumes -- exactly as if the handler had never run before.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        comment = FakeComment(
            id=2000, body="please fix the off-by-one",
            user=FakeUser("alice"), created_at=long_ago,
        )
        pr = self._open_pr()
        gh, issue = self._seed(
            pr=pr, issue_comments=[comment],
            # Bookmarks left by in_review when it routed; transient
            # state cleared as if the process just started up.
            extra_state={
                "awaiting_human": False,
                "pending_fix_at": "2026-05-23T00:00:00+00:00",
                "pending_fix_issue_max_id": 2000,
            },
        )

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="pushed",
                ),
                head_shas=("sha-before", "sha-after"),
            )

        mocks["run_agent"].assert_called_once()
        # The followup quotes the triggering comment, proving the
        # rescan re-derived the unread feedback from the persisted
        # watermarks rather than relying on in-memory state.
        prompt = mocks["run_agent"].call_args.args[1]
        self.assertIn("please fix the off-by-one", prompt)
        # Push succeeded -> documenting (the docs pass runs before the
        # reviewer agent re-evaluates); bookmarks cleared.
        self.assertIn((880, "documenting"), gh.label_history)
        self.assertNotIn((880, "validating"), gh.label_history)
        data = gh.pinned_data(880)
        self.assertIsNone(data.get("pending_fix_at"))
        self.assertIsNone(data.get("pending_fix_issue_max_id"))
