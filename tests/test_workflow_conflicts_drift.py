# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import FakeGitHubClient, FakePR, make_issue
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleResolvingConflictHashDriftTest(
    unittest.TestCase, _PatchedWorkflowMixin,
):
    """Reviewer point 2: `resolving_conflict` is dispatched per tick too,
    so a body edit while the dev is resolving conflicts must surface to
    the dev. Mirrors the in_review pattern: post a PR notice and resume."""

    def test_drift_posts_pr_notice_and_resumes_dev(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(
            500, label="resolving_conflict", body="updated body",
        )
        gh.add_issue(issue)
        pr = FakePR(number=5000, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-500")
        gh.add_pr(pr)
        gh.seed_state(
            500,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            conflict_round=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-500",
            user_content_hash="stale-hash",
        )

        self._run(
            lambda: workflow._handle_resolving_conflict(
                gh, _TEST_SPEC, issue,
            ),
            run_agent=_agent(
                session_id="dev-sess", last_message="resolved with edit"
            ),
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
        self.assertIn((500, "validating"), gh.label_history)
        self.assertNotIn((500, "documenting"), gh.label_history)
        # Notice posted on the PR.
        self.assertTrue(any(
            "issue body changed" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_drift_resume_interrupted_leaves_state_untouched(self) -> None:
        # The drift resume routes through the shared
        # `_post_user_content_change_result`, which has no interrupted check
        # of its own. The conflicts caller must short-circuit BEFORE it so a
        # shutdown-sweep-killed run cannot ACK / park off partial output and
        # then persist the consumed-comment / refreshed-hash changes.
        gh = FakeGitHubClient()
        issue = make_issue(
            501, label="resolving_conflict", body="updated body",
        )
        gh.add_issue(issue)
        pr = FakePR(number=5001, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-501")
        gh.add_pr(pr)
        gh.seed_state(
            501,
            pr_number=pr.number,
            dev_agent="claude",
            dev_session_id="dev-sess",
            conflict_round=0,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-501",
            user_content_hash="stale-hash",
        )
        before_writes = gh.write_state_calls

        mocks = self._run(
            lambda: workflow._handle_resolving_conflict(
                gh, _TEST_SPEC, issue,
            ),
            run_agent=_agent(
                session_id="dev-sess", last_message="", interrupted=True,
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
        data = gh.pinned_data(501)
        self.assertEqual(data.get("user_content_hash"), "stale-hash")
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("conflict_round"), 0)
        # No flip back to validating and no HITL question / ack on the issue.
        self.assertNotIn((501, "validating"), gh.label_history)
        self.assertFalse(any(
            "agent needs your input" in body
            or "existing work" in body
            or "timed out" in body
            for _, body in gh.posted_comments
        ))


if __name__ == "__main__":
    unittest.main()
