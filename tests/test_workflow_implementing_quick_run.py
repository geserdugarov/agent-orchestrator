# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""`quick_run` fast path: a clean developer result on a `quick_run`-labeled
issue publishes its PR and routes straight from `implementing` to `in_review`,
bypassing the reviewer (`validating`) and docs (`documenting`) passes an
ordinary issue takes. Covers the fresh-completion and recovered-commit
publication paths, the counter reset / PR-state persistence, and the reused
handoff-watermark seed that keeps concurrent human feedback visible."""
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
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


def _quick_run_issue(number: int, **kwargs) -> object:
    issue = make_issue(number, label="implementing", **kwargs)
    issue.labels.append(FakeLabel("quick_run"))
    return issue


class QuickRunRoutesToInReviewTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_fresh_clean_commits_route_to_in_review(self) -> None:
        gh = FakeGitHubClient()
        issue = _quick_run_issue(1)
        gh.add_issue(issue)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            # First probe: not a recovered worktree -> the dev runs. Second
            # probe: the dev produced commits -> the push/publish path.
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        self.assertEqual(len(gh.opened_prs), 1)
        opened = gh.opened_prs[0]
        # Straight to in_review; the reviewer + docs passes are skipped.
        self.assertIn((1, "in_review"), gh.label_history)
        self.assertNotIn((1, "validating"), gh.label_history)
        self.assertNotIn((1, "documenting"), gh.label_history)
        state = gh.pinned_data(1)
        self.assertEqual(state["pr_number"], opened.number)
        self.assertEqual(
            state["branch"],
            "orchestrator/geserdugarov__agent-orchestrator/issue-1",
        )
        # All three in_review comment-surface watermarks are seeded at the
        # handoff so the first in_review tick does not replay the
        # orchestrator's own automated comments as fresh PR feedback.
        self.assertIn("pr_last_comment_id", state)
        self.assertIn("pr_last_review_comment_id", state)
        self.assertIn("pr_last_review_summary_id", state)
        self.assertEqual(state["review_round"], 0)

    def test_ordinary_issue_still_routes_to_validating(self) -> None:
        # The contrast case: without the `quick_run` label a clean commit
        # takes the ordinary reviewer path.
        gh = FakeGitHubClient()
        issue = make_issue(2, label="implementing")
        gh.add_issue(issue)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        self.assertIn((2, "validating"), gh.label_history)
        self.assertNotIn((2, "in_review"), gh.label_history)

    def test_recovered_worktree_routes_to_in_review(self) -> None:
        # A worktree carrying commits from a prior tick (crash before the
        # push/relabel) skips the agent, publishes, and still routes the
        # `quick_run` issue to in_review -- and resets the per-PR budgets.
        gh = FakeGitHubClient()
        issue = _quick_run_issue(3)
        gh.add_issue(issue)
        gh.seed_state(
            3,
            dev_agent="claude",
            dev_session_id="sess-prev",
            review_round=4,
            retry_count=2,
            retry_window_start="2026-07-14T00:00:00+00:00",
            silent_park_count=1,
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(len(gh.opened_prs), 1)
        self.assertIn((3, "in_review"), gh.label_history)
        self.assertNotIn((3, "validating"), gh.label_history)
        state = gh.pinned_data(3)
        self.assertEqual(state["pr_number"], gh.opened_prs[0].number)
        # Prior dev session retained; counters reset for any later bounce
        # back into implementing.
        self.assertEqual(state.get("dev_session_id"), "sess-prev")
        self.assertEqual(state["review_round"], 0)
        self.assertEqual(state["retry_count"], 0)
        self.assertIsNone(state["retry_window_start"])
        self.assertEqual(state["silent_park_count"], 0)


class QuickRunHandoffPreservesHumanFeedbackTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human PR comment posted while the dev was implementing must not be
    swallowed by the quick_run handoff watermark seed -- the reused
    `_seed_in_review_pr_watermarks` stops the walk at the first unread
    non-orchestrator comment, so the next in_review tick still surfaces it and
    routes to `fixing` rather than pinging HITL for a merge over unread
    feedback."""

    PR_NUMBER = 42
    BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-30"

    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = _quick_run_issue(30, comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            # Human left a PR-conversation comment while the dev worked; the
            # handoff must not advance the watermark past it.
            issue_comments=[
                FakeComment(
                    id=950, body="please add a docstring",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        # Reuse path: a prior tick opened the PR but crashed before relabel.
        gh.existing_open_pr[self.BRANCH] = pr
        gh.seed_state(
            30,
            dev_agent="claude", dev_session_id="dev-sess",
            branch=self.BRANCH,
            orchestrator_comment_ids=[900],
            pickup_comment_id=900,
        )
        return gh, issue, pr

    def test_human_pr_comment_survives_quick_run_handoff(self) -> None:
        gh, issue, pr = self._setup()

        # Step 1: recovered worktree publishes and routes to in_review. The
        # watermark stops before the human PR comment at id 950.
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
        )
        self.assertIn((30, "in_review"), gh.label_history)
        watermark = gh.pinned_data(30).get("pr_last_comment_id")
        self.assertIsNotNone(watermark)
        self.assertLess(
            watermark, 950,
            f"watermark must stop before human comment id=950 (got {watermark})",
        )

        # Step 2: in_review tick surfaces the human comment and routes to
        # `fixing` (the fixing handler owns the dev resume) instead of
        # merging or pinging HITL.
        with patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((30, "fixing"), gh.label_history)
        self.assertEqual(
            gh.pinned_data(30).get("pending_fix_issue_max_id"), 950,
        )


if __name__ == "__main__":
    unittest.main()
