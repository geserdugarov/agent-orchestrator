# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest


from tests.fakes import FakeGitHubClient, FakePR, make_issue
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _agent,
    _issue_branch,
)

DRIFT_ISSUE = 500
DRIFT_PR = 5000
INTERRUPTED_ISSUE = 501
INTERRUPTED_PR = 5001
DEV_SESSION = "dev-sess"


class HandleResolvingConflictHashDriftTest(
    unittest.TestCase,
    _PatchedWorkflowMixin,
):
    """Reviewer point 2: `resolving_conflict` is dispatched per tick too,
    so a body edit while the dev is resolving conflicts must surface to
    the dev. Mirrors the in_review pattern: post a PR notice and resume."""

    def test_drift_posts_pr_notice_and_resumes_dev(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            DRIFT_ISSUE,
            label="resolving_conflict",
            body="updated body",
        )
        gh.add_issue(issue)
        pr = FakePR(number=DRIFT_PR, head_branch=_issue_branch(DRIFT_ISSUE))
        gh.add_pr(pr)
        gh.seed_state(
            DRIFT_ISSUE,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id=DEV_SESSION,
            conflict_round=0,
            branch=_issue_branch(DRIFT_ISSUE),
            user_content_hash="stale-hash",
        )

        self._run_resolving_conflict(
            gh,
            issue,
            run_agent=_agent(session_id=DEV_SESSION, last_message="resolved with edit"),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
            # Three SHAs: drift before/after for the post-resume head
            # delta, plus the third for the `conflict_round` audit emit
            # that records the pushed worktree HEAD.
            head_shas=["before", "after", "after"],
        )

        # Pushed drift fix -> hand straight back to `validating`; the
        # single docs pass is deferred to the post-approval hop.
        self.assertIn((DRIFT_ISSUE, "validating"), gh.label_history)
        self.assertNotIn((DRIFT_ISSUE, "documenting"), gh.label_history)
        # Notice posted on the PR.
        self.assertTrue(
            any(
                "issue body changed" in body
                for _, body in gh.posted_pr_comments
            )
        )

    def test_interrupted_resume_keeps_state(self) -> None:
        # The drift resume routes through the shared
        # `_post_user_content_change_result`, which has no interrupted check
        # of its own. The conflicts caller must short-circuit BEFORE it so a
        # shutdown-sweep-killed run cannot ACK / park off partial output and
        # then persist the consumed-comment / refreshed-hash changes.
        gh = FakeGitHubClient()
        issue = make_issue(
            INTERRUPTED_ISSUE,
            label="resolving_conflict",
            body="updated body",
        )
        gh.add_issue(issue)
        pr = FakePR(number=INTERRUPTED_PR, head_branch=_issue_branch(INTERRUPTED_ISSUE))
        gh.add_pr(pr)
        gh.seed_state(
            INTERRUPTED_ISSUE,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id=DEV_SESSION,
            conflict_round=0,
            branch=_issue_branch(INTERRUPTED_ISSUE),
            user_content_hash="stale-hash",
        )
        before_writes = gh.write_state_calls

        mocks = self._run_resolving_conflict(
            gh,
            issue,
            run_agent=_agent(
                session_id=DEV_SESSION,
                last_message="",
                interrupted=True,
            ),
            has_new_commits=True,
            push_branch=True,
            head_shas=["before-sha", "after-sha"],
        )

        # The drift resume spawned, then was seen interrupted.
        mocks["run_agent"].assert_called_once()
        mocks["_push_branch"].assert_not_called()
        # No durable state churn: the refreshed `user_content_hash`,
        # consumed-comment, and session mutations are all discarded.
        self.assertEqual(gh.write_state_calls, before_writes)
        state = gh.pinned_data(INTERRUPTED_ISSUE)
        self.assertEqual(state.get("user_content_hash"), "stale-hash")
        self.assertFalse(state.get("awaiting_human"))
        self.assertEqual(state.get("conflict_round"), 0)
        # No flip back to validating and no HITL question / ack on the issue.
        self.assertNotIn((INTERRUPTED_ISSUE, "validating"), gh.label_history)
        self.assertFalse(
            any(
                "agent needs your input" in body or "existing work" in body or "timed out" in body
                for _, body in gh.posted_comments
            )
        )


if __name__ == "__main__":
    unittest.main()
