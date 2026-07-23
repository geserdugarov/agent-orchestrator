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
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

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
    _agent,
)

DRIFT_ISSUE = 80
DRIFT_PR = 800
HUMAN_RESUME_ISSUE = 81
HUMAN_RESUME_PR = 810
HUMAN_REPLY_ID = 5000
ACTION_WATERMARK = 4000
CHANGES_REQUESTED_ISSUE = 82
CHANGES_REQUESTED_PR = 820
LABEL_VALIDATING = "validating"
DEV_SESSION = "dev-sess"
REVIEW_ROUND = "review_round"


def _branch(number: int) -> str:
    return f"orchestrator/geserdugarov__agent-orchestrator/issue-{number}"


def _paused_view(number: int) -> object:
    """A `validating` issue that also carries `paused` -- the state a fresh
    `gh.get_issue` returns after an operator pauses mid-run. The handler's own
    `issue` snapshot deliberately does NOT carry it, so a guard that consulted
    the stale snapshot would publish."""
    view = make_issue(number, label=LABEL_VALIDATING)
    view.labels.append(FakeLabel(PAUSED_LABEL))
    return view


@dataclass(frozen=True)
class _PauseCase:
    issue_number: int
    pr_number: int
    body: str = "body"
    comments: tuple[FakeComment, ...] = ()
    state: dict[str, object] | None = None


class _ValidatingPauseFixtureMixin(_PatchedWorkflowMixin):
    def _pause_fixture(self, case: _PauseCase):
        github = FakeGitHubClient()
        issue = make_issue(
            case.issue_number,
            label=LABEL_VALIDATING,
            body=case.body,
            comments=list(case.comments),
        )
        github.add_issue(issue)
        github.add_pr(
            FakePR(
                number=case.pr_number,
                head_branch=_branch(case.issue_number),
            ),
        )
        state = {
            "user_content_hash": workflow._compute_user_content_hash(
                issue,
                set(),
            ),
            "dev_agent": "claude",
            "dev_session_id": DEV_SESSION,
            "pr_number": case.pr_number,
            REVIEW_ROUND: 1,
            "branch": _branch(case.issue_number),
        }
        state.update(case.state or {})
        github.seed_state(case.issue_number, **state)
        return github, issue, github.write_state_calls

    def _run_paused(
        self,
        github,
        issue,
        issue_number: int,
        run_agent,
    ):
        with patch.object(
            github,
            "get_issue",
            return_value=_paused_view(issue_number),
        ):
            return self._run_validating(
                github,
                issue,
                run_agent=run_agent,
                head_shas=["before-sha", "after-sha"],
            )

    def _run_paused_fix(self, github, issue):
        issue_fetch = MagicMock(
            side_effect=[
                make_issue(
                    CHANGES_REQUESTED_ISSUE,
                    label=LABEL_VALIDATING,
                ),
                _paused_view(CHANGES_REQUESTED_ISSUE),
            ],
        )
        with patch.object(github, "get_issue", issue_fetch):
            return self._run_validating(
                github,
                issue,
                run_agent=[
                    _agent(
                        session_id="rev-sess",
                        last_message=(
                            "Please tighten the guard.\n\n"
                            "VERDICT: CHANGES_REQUESTED"
                        ),
                    ),
                    _agent(session_id=DEV_SESSION, last_message="fixed"),
                ],
                head_shas=["before-sha", "after-sha"],
            )

    def _assert_drift_paused(self, github, mocks, before_writes: int) -> None:
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(github.label_history, [])
        self.assertEqual(github.posted_pr_comments, [])
        self.assertFalse(
            any(
                ":speech_balloon:" in body
                for _, body in github.posted_comments
            ),
        )
        self.assertEqual(github.write_state_calls, before_writes)
        state = github.pinned_data(DRIFT_ISSUE)
        self.assertEqual(state.get(REVIEW_ROUND), 1)
        self.assertEqual(state.get("user_content_hash"), "stale-hash")

    def _assert_resume_paused(self, github, mocks, before_writes: int) -> None:
        mocks["run_agent"].assert_called_once()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(github.label_history, [])
        self.assertEqual(github.posted_comments, [])
        self.assertEqual(github.posted_pr_comments, [])
        self.assertEqual(github.write_state_calls, before_writes)
        state = github.pinned_data(HUMAN_RESUME_ISSUE)
        self.assertTrue(state.get("awaiting_human"))
        self.assertEqual(
            state.get("last_action_comment_id"),
            ACTION_WATERMARK,
        )
        self.assertEqual(state.get(REVIEW_ROUND), 2)

    def _assert_fix_paused(self, github, mocks, before_writes: int) -> None:
        self.assertEqual(mocks["run_agent"].call_count, 2)
        self.assertIn(
            (CHANGES_REQUESTED_ISSUE, "fixing"),
            github.label_history,
        )
        self.assertNotIn(
            (CHANGES_REQUESTED_ISSUE, LABEL_VALIDATING),
            github.label_history,
        )
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(github.write_state_calls, before_writes + 1)
        state = github.pinned_data(CHANGES_REQUESTED_ISSUE)
        self.assertEqual(state.get(REVIEW_ROUND), 0)


class ValidatingLivePauseDriftResumeTest(
    unittest.TestCase,
    _ValidatingPauseFixtureMixin,
):
    def test_drift_pause_skips_result_handler(self) -> None:
        # A human edited the issue body (stale seeded hash -> drift fires), so
        # the handler resumes the dev on the new body. `paused` is applied
        # during that resume: the guard must stop BEFORE
        # `_post_user_content_change_result` posts an ack / pushes / bumps the
        # round, and before pinned state is written.
        gh, issue, before_writes = self._pause_fixture(
            _PauseCase(
                issue_number=DRIFT_ISSUE,
                pr_number=DRIFT_PR,
                body="updated criteria",
                state={"user_content_hash": "stale-hash"},
            ),
        )

        mocks = self._run_paused(
            gh,
            issue,
            DRIFT_ISSUE,
            _agent(session_id=DEV_SESSION, last_message="fixed"),
        )

        # Result handler never ran: no push, no relabel, no ack comment.
        self._assert_drift_paused(gh, mocks, before_writes)


class ValidatingLivePauseAwaitingHumanTest(
    unittest.TestCase,
    _ValidatingPauseFixtureMixin,
):
    def test_resume_skips_fix_disposition(
        self,
    ) -> None:
        # Parked on a question (dev-side, non-transient), a human replied and
        # the dev is resumed. `paused` applied mid-resume must stop BEFORE
        # `_handle_dev_fix_result` parks / pushes / bumps the round -- and
        # before any pinned write -- leaving the park intact.
        gh, issue, before_writes = self._pause_fixture(
            _PauseCase(
                issue_number=HUMAN_RESUME_ISSUE,
                pr_number=HUMAN_RESUME_PR,
                comments=(
                    FakeComment(
                        id=HUMAN_REPLY_ID,
                        body="here is the answer",
                        user=FakeUser("alice"),
                    ),
                ),
                state={
                    REVIEW_ROUND: 2,
                    "awaiting_human": True,
                    "park_reason": None,
                    "last_action_comment_id": ACTION_WATERMARK,
                },
            ),
        )

        mocks = self._run_paused(
            gh,
            issue,
            HUMAN_RESUME_ISSUE,
            _agent(session_id=DEV_SESSION, last_message="done"),
        )

        # Exactly the dev resume ran; no push / relabel / comment followed.
        self._assert_resume_paused(gh, mocks, before_writes)


class ValidatingLivePauseChangesRequestedTest(
    unittest.TestCase,
    _ValidatingPauseFixtureMixin,
):
    def test_fix_pause_keeps_fixing_label(self) -> None:
        # The reviewer returns CHANGES_REQUESTED, so the handler posts the
        # feedback, flips to `fixing` (durable, pre-spawn), and resumes the dev
        # with the fix prompt. `paused` applied during that resume must stop
        # BEFORE `_handle_dev_fix_result` pushes / bumps the round / relabels
        # back to `validating`. The pre-spawn `fixing` flip stands so
        # `_handle_fixing` owns the resume once the label is removed.
        gh, issue, before_writes = self._pause_fixture(
            _PauseCase(
                issue_number=CHANGES_REQUESTED_ISSUE,
                pr_number=CHANGES_REQUESTED_PR,
                state={REVIEW_ROUND: 0},
            ),
        )

        # The reviewer runs and returns CHANGES_REQUESTED while the issue is
        # still clean; the operator applies `paused` only during the ensuing
        # dev resume. So the reviewer-run guard must see a non-paused issue on
        # its fetch and only the post-dev-resume guard sees `paused` -- a
        # blanket paused view would trip the reviewer-run guard first and the
        # dev would never resume.
        mocks = self._run_paused_fix(gh, issue)

        # Reviewer + dev both ran; the pre-spawn flip to `fixing` is durable.
        self._assert_fix_paused(gh, mocks, before_writes)


if __name__ == "__main__":
    unittest.main()
