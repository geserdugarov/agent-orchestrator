# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Live `paused` guard for the validating stage: an operator who applies
`paused` (or `backlog`) WHILE a dev resume is in flight freezes the issue
before the resume's result handler runs. The guard re-fetches the issue after
the resume returns (`gh.get_issue`) rather than trusting the handler's label
snapshot, and on a hit the handler returns without posting the ack, pushing,
bumping `review_round`, relabeling, or writing pinned state -- so once the
label is removed a later tick republishes the committed work normally. Covers
the three validating dev resumes: the user-content drift resume, the
awaiting-human resume, and the CHANGES_REQUESTED reviewer-feedback fix."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import workflow
from orchestrator.github import PAUSED_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


def _branch(number: int) -> str:
    return f"orchestrator/geserdugarov__agent-orchestrator/issue-{number}"


def _paused_view(number: int) -> object:
    """A `validating` issue that also carries `paused` -- the state a fresh
    `gh.get_issue` returns after an operator pauses mid-run. The handler's own
    `issue` snapshot deliberately does NOT carry it, so a guard that consulted
    the stale snapshot would publish."""
    view = make_issue(number, label="validating")
    view.labels.append(FakeLabel(PAUSED_LABEL))
    return view


class ValidatingLivePauseDriftResumeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    def test_pause_during_drift_resume_skips_result_handler(self) -> None:
        # A human edited the issue body (stale seeded hash -> drift fires), so
        # the handler resumes the dev on the new body. `paused` is applied
        # during that resume: the guard must stop BEFORE
        # `_post_user_content_change_result` posts an ack / pushes / bumps the
        # round, and before pinned state is written.
        gh = FakeGitHubClient()
        issue = make_issue(80, label="validating", body="updated criteria")
        gh.add_issue(issue)
        pr = FakePR(number=800, head_branch=_branch(80))
        gh.add_pr(pr)
        gh.seed_state(
            80,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=800,
            review_round=1,
            branch=_branch(80),
        )
        before_writes = gh.write_state_calls

        with patch.object(gh, "get_issue", return_value=_paused_view(80)):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="dev-sess", last_message="fixed"),
                head_shas=["before-sha", "after-sha"],
            )

        # Result handler never ran: no push, no relabel, no ack comment.
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_pr_comments, [])
        self.assertFalse(any(
            ":speech_balloon:" in body for _, body in gh.posted_comments
        ))
        # Durable state untouched: no pinned write, round not bumped.
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(80)
        self.assertEqual(state.get("review_round"), 1)
        self.assertEqual(state.get("user_content_hash"), "stale-hash")


class ValidatingLivePauseAwaitingHumanTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    def test_resume_skips_fix_disposition(
        self,
    ) -> None:
        # Parked on a question (dev-side, non-transient), a human replied and
        # the dev is resumed. `paused` applied mid-resume must stop BEFORE
        # `_handle_dev_fix_result` parks / pushes / bumps the round -- and
        # before any pinned write -- leaving the park intact.
        gh = FakeGitHubClient()
        issue = make_issue(81, label="validating", body="body")
        human = FakeComment(id=5000, body="here is the answer", user=FakeUser("alice"))
        issue.comments.append(human)
        gh.add_issue(issue)
        pr = FakePR(number=810, head_branch=_branch(81))
        gh.add_pr(pr)
        # Seed the hash to INCLUDE the new comment so the drift check returns
        # None and the awaiting-human branch (not the drift resume) handles it.
        seed_hash = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(
            81,
            user_content_hash=seed_hash,
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=810,
            review_round=2,
            branch=_branch(81),
            awaiting_human=True,
            park_reason=None,
            last_action_comment_id=4000,
        )
        before_writes = gh.write_state_calls

        with patch.object(gh, "get_issue", return_value=_paused_view(81)):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="dev-sess", last_message="done"),
                head_shas=["before-sha", "after-sha"],
            )

        # Exactly the dev resume ran; no push / relabel / comment followed.
        mocks["run_agent"].assert_called_once()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])
        # Durable state untouched: the park stays put and the consumed-comment
        # watermark is NOT advanced, so the next tick re-consumes the reply.
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(81)
        self.assertTrue(state.get("awaiting_human"))
        self.assertEqual(state.get("last_action_comment_id"), 4000)
        self.assertEqual(state.get("review_round"), 2)


class ValidatingLivePauseChangesRequestedTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    def test_pause_during_reviewer_change_fix_keeps_fixing_label(self) -> None:
        # The reviewer returns CHANGES_REQUESTED, so the handler posts the
        # feedback, flips to `fixing` (durable, pre-spawn), and resumes the dev
        # with the fix prompt. `paused` applied during that resume must stop
        # BEFORE `_handle_dev_fix_result` pushes / bumps the round / relabels
        # back to `validating`. The pre-spawn `fixing` flip stands so
        # `_handle_fixing` owns the resume once the label is removed.
        gh = FakeGitHubClient()
        issue = make_issue(82, label="validating", body="body")
        gh.add_issue(issue)
        pr = FakePR(number=820, head_branch=_branch(82))
        gh.add_pr(pr)
        seed_hash = workflow._compute_user_content_hash(issue, set())
        gh.seed_state(
            82,
            user_content_hash=seed_hash,
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=820,
            review_round=0,
            branch=_branch(82),
        )
        before_writes = gh.write_state_calls

        reviewer = _agent(
            session_id="rev-sess",
            last_message="Please tighten the guard.\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev = _agent(session_id="dev-sess", last_message="fixed")
        # The reviewer runs and returns CHANGES_REQUESTED while the issue is
        # still clean; the operator applies `paused` only during the ensuing
        # dev resume. So the reviewer-run guard must see a non-paused issue on
        # its fetch and only the post-dev-resume guard sees `paused` -- a
        # blanket paused view would trip the reviewer-run guard first and the
        # dev would never resume.
        fetches = {"n": 0}

        def _get_issue(_number):
            fetches["n"] += 1
            return (
                make_issue(82, label="validating")
                if fetches["n"] == 1
                else _paused_view(82)
            )

        with patch.object(gh, "get_issue", side_effect=_get_issue):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=[reviewer, dev],
                head_shas=["before-sha", "after-sha"],
            )

        # Reviewer + dev both ran; the pre-spawn flip to `fixing` is durable.
        self.assertEqual(mocks["run_agent"].call_count, 2)
        self.assertIn((82, "fixing"), gh.label_history)
        # But no relabel back to `validating` and no fix disposition fired.
        self.assertNotIn((82, "validating"), gh.label_history)
        mocks["_push_branch"].assert_not_called()
        # Only the pre-spawn `fixing` write persisted; the post-resume path
        # never wrote again, and `review_round` was not bumped.
        self.assertEqual(gh.write_state_calls, before_writes + 1)
        state = gh.pinned_data(82)
        self.assertEqual(state.get("review_round"), 0)


if __name__ == "__main__":
    unittest.main()
