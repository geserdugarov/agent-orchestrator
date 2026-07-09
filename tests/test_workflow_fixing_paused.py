# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Live `paused` guard for the fixing stage: an operator who applies `paused`
(or `backlog`) WHILE the PR-feedback dev resume is in flight freezes the issue
before the run's results are published. `_resume_dev_with_text(pause_guard=True)`
re-fetches the issue after the run returns (`gh.get_issue`) and, on a hit, the
handler returns before the ACK fast path, `_handle_dev_fix_result`, the
watermark advance, or any relabel / pinned-state write -- so the feedback stays
unconsumed and the committed work stays on the branch until the label is
removed, when a later tick re-discovers the feedback and republishes."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from orchestrator import config, workflow
from orchestrator.github import PAUSED_LABEL

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakeUser,
    make_issue,
)
from tests.workflow_helpers import _PatchedWorkflowMixin, _TEST_SPEC, _agent

ISSUE = 880
PR_NUMBER = 880
BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-880"
PR_HEAD_SHA = "cafe1234"
DEV_AGENT = "claude"
DEV_SESSION = "dev-sess"
TRIGGER_ID = 2000
DEBOUNCE_SECONDS = 600


def _paused_view(number: int) -> object:
    """A `fixing` issue that also carries `paused` -- the state a fresh
    `gh.get_issue` returns after an operator pauses mid-run."""
    view = make_issue(number, label="fixing")
    view.labels.append(FakeLabel(PAUSED_LABEL))
    return view


class FixingLivePauseTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seed(self, gh: FakeGitHubClient) -> object:
        # Triggering PR feedback older than the debounce window so the quiet
        # gate passes and the handler reaches the dev resume this tick.
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(ISSUE, label="fixing")
        issue.comments.append(
            FakeComment(
                id=TRIGGER_ID, body="rename foo to bar",
                user=FakeUser("alice"), created_at=old,
            )
        )
        gh.add_issue(issue)
        gh.add_pr(FakePR(
            number=PR_NUMBER, head_branch=BRANCH,
            head=FakePRRef(sha=PR_HEAD_SHA), mergeable=True,
            check_state="success",
        ))
        gh.seed_state(
            ISSUE,
            pr_number=PR_NUMBER,
            branch=BRANCH,
            dev_agent=DEV_AGENT,
            dev_session_id=DEV_SESSION,
            review_round=1,
            pr_last_comment_id=1999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            pending_fix_at="2026-05-24T00:00:00+00:00",
            pending_fix_issue_max_id=TRIGGER_ID,
        )
        return issue

    def test_resume_blocks_publish_relabel_and_watermark(
        self,
    ) -> None:
        # The handler's `issue` snapshot carries no `paused`; the operator
        # applied it only after the resume started, so it appears solely on the
        # freshly fetched view. The dev even reports a pushable fix -- a guard
        # that consulted the stale labels (or skipped the check) would push and
        # flip to `validating`. Asserting no push / no relabel proves the guard
        # reads `gh.get_issue` and honors it.
        gh = FakeGitHubClient()
        issue = self._seed(gh)
        before_writes = gh.write_state_calls

        get_issue_mock = MagicMock(return_value=_paused_view(ISSUE))
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", DEBOUNCE_SECONDS), \
             patch.object(gh, "get_issue", get_issue_mock):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id=DEV_SESSION, last_message="pushed fix"),
                head_shas=("sha-before", "sha-after"),
                push_branch=True,
            )

        mocks["run_agent"].assert_called_once()
        get_issue_mock.assert_called_with(ISSUE)
        # No publish, no relabel, no ACK / park comment.
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        # Durable state untouched: watermark stays below the feedback, the
        # route bookmark and park flag survive, and nothing is written -- so a
        # later tick re-discovers the same feedback once the label is removed.
        self.assertEqual(gh.write_state_calls, before_writes)
        data = gh.pinned_data(ISSUE)
        self.assertEqual(data.get("pr_last_comment_id"), 1999)
        self.assertEqual(data.get("pending_fix_at"), "2026-05-24T00:00:00+00:00")
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("dev_session_id"), DEV_SESSION)

    def test_paused_then_removed_republishes_via_resume(self) -> None:
        # End-to-end: tick 1 is frozen by a live pause and leaves the feedback
        # unconsumed; tick 2, after the operator removes `paused`, re-discovers
        # the same feedback, resumes the dev, pushes the fix, and flips to
        # `validating` -- proving the pause preserved exactly enough state to
        # resume cleanly.
        gh = FakeGitHubClient()
        issue = self._seed(gh)

        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", DEBOUNCE_SECONDS), \
             patch.object(gh, "get_issue", MagicMock(return_value=_paused_view(ISSUE))):
            self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id=DEV_SESSION, last_message="pushed fix"),
                head_shas=("sha-before", "sha-after"),
                push_branch=True,
            )
        self.assertEqual(gh.label_history, [])

        # Tick 2: `paused` removed -> the fresh fetch is clean, so the resume's
        # pushed fix publishes and relabels to `validating`.
        unpaused = make_issue(ISSUE, label="fixing")
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", DEBOUNCE_SECONDS), \
             patch.object(gh, "get_issue", MagicMock(return_value=unpaused)):
            mocks = self._run(
                lambda: workflow._handle_fixing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id=DEV_SESSION, last_message="pushed fix"),
                head_shas=("sha-before", "sha-after"),
                push_branch=True,
            )

        mocks["_push_branch"].assert_called_once()
        self.assertIn((ISSUE, "validating"), gh.label_history)
        data = gh.pinned_data(ISSUE)
        self.assertEqual(data.get("review_round"), 0)
        self.assertIsNone(data.get("pending_fix_at"))
        self.assertGreaterEqual(data.get("pr_last_comment_id"), TRIGGER_ID)


if __name__ == "__main__":
    unittest.main()
