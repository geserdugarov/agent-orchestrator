# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Live `paused` guard for the in_review stage: an operator who applies `paused`
(or `backlog`) WHILE the user-content-drift dev resume is in flight freezes the
issue before the run's results are published. `_resume_dev_with_text(pause_guard
=True)` re-fetches the issue after the run returns (`gh.get_issue`) and, on a
hit, the handler returns before `_post_user_content_change_result`, the in_review
watermark bump, or any relabel / pinned-state write -- so the drift stays
unconsumed (the stale hash stands) and the committed work stays on the branch
until the label is removed."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow
from orchestrator.github import PAUSED_LABEL

from tests.fakes import FakeGitHubClient, FakeLabel, FakePR, make_issue
from tests.workflow_helpers import _PatchedWorkflowMixin, _TEST_SPEC, _agent

BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-85"


def _paused_view(number: int) -> object:
    view = make_issue(number, label="in_review")
    view.labels.append(FakeLabel(PAUSED_LABEL))
    return view


class InReviewLivePauseDriftTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_drift_pause_blocks_relabel_and_bump(self) -> None:
        # A body edit (seeded hash mismatch) drives the drift resume. The
        # operator applies `paused` only after the run starts, so it appears
        # solely on the freshly fetched view -- a guard consulting the stale
        # labels would run `_post_user_content_change_result`, bump the
        # watermarks, and bounce to `validating`. Asserting none of that
        # happens (and the stale hash survives) proves the guard reads
        # `gh.get_issue` and honors it, leaving the drift for a later tick.
        gh = FakeGitHubClient()
        issue = make_issue(85, label="in_review", body="new acceptance")
        gh.add_issue(issue)
        gh.add_pr(FakePR(number=805, head_branch=BRANCH))
        gh.seed_state(
            85,
            user_content_hash="stale-hash",
            dev_agent="claude",
            dev_session_id="dev-sess",
            pr_number=805,
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            branch=BRANCH,
            review_round=2,
        )
        before_writes = gh.write_state_calls

        get_issue_mock = MagicMock(return_value=_paused_view(85))
        with patch.object(gh, "get_issue", get_issue_mock):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="dev-sess", last_message="addressed"),
                has_new_commits=True,
                dirty_files=(),
                push_branch=True,
                head_shas=["before", "after"],
            )

        mocks["run_agent"].assert_called_once()
        get_issue_mock.assert_called_with(85)
        mocks["_push_branch"].assert_not_called()
        # Nothing persisted, no relabel: the drift stays unconsumed.
        self.assertEqual(gh.write_state_calls, before_writes)
        self.assertNotIn((85, "validating"), gh.label_history)
        self.assertNotIn((85, "documenting"), gh.label_history)
        data = gh.pinned_data(85)
        self.assertEqual(data.get("user_content_hash"), "stale-hash")
        self.assertEqual(data.get("review_round"), 2)
        self.assertFalse(data.get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
