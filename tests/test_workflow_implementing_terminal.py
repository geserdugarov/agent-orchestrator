# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Terminal handling for `_handle_implementing`: external-merge short-circuit
to `done` and the closed-issue sweep that flips to `rejected` (with safe
deferrals for transient PR-fetch failures)."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import (
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class HandleImplementingExternalMergeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human merged the PR before implementing finished (e.g. an
    operator cherry-picked the work elsewhere). The handler must
    short-circuit to `done` instead of resuming the dev agent.
    """

    def test_external_merge_finalizes_to_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(150, label="implementing")
        gh.add_issue(issue)
        pr = FakePR(
            number=15000,
            head_branch="orchestrator/issue-150",
            head=FakePRRef(sha="cafe1234"),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            150, pr_number=15000, branch="orchestrator/issue-150",
            dev_agent="claude", dev_session_id="dev-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((150, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(150))
        self.assertTrue(issue.closed)
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 150,
        )


class HandleImplementingClosedIssueTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """Closed `implementing` issues yielded by the new closed-issue sweep
    must NOT spawn the dev agent. The handler now flips to `rejected`
    after the external-merge finalize returns False.
    """

    def test_closed_implementing_with_no_pr_flips_to_rejected(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(151, label="implementing")
        issue.closed = True
        gh.add_issue(issue)
        gh.seed_state(151, dev_agent="claude", dev_session_id="dev-sess")

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((151, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(151))
        mocks["run_agent"].assert_not_called()
        # No PR → no branch cleanup (no remote ref to delete).
        mocks["_cleanup_terminal_branch"].assert_not_called()

    def test_closed_implementing_with_open_pr_skips_cleanup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(152, label="implementing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=15200,
            head_branch="orchestrator/issue-152",
            head=FakePRRef(sha="cafe1234"),
            merged=False,
            state="open",
        )
        gh.add_pr(pr)
        gh.seed_state(
            152, pr_number=15200, branch="orchestrator/issue-152",
            dev_agent="claude", dev_session_id="dev-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((152, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(152))
        mocks["run_agent"].assert_not_called()
        # Open PR + closed issue: leave the branch alone so the operator
        # can salvage / reopen the PR.
        mocks["_cleanup_terminal_branch"].assert_not_called()

    def test_closed_implementing_with_closed_pr_runs_cleanup(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(153, label="implementing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=15300,
            head_branch="orchestrator/issue-153",
            head=FakePRRef(sha="cafe1234"),
            merged=False,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            153, pr_number=15300, branch="orchestrator/issue-153",
            dev_agent="claude", dev_session_id="dev-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((153, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(153))
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 153,
        )
        # `pr_closed_without_merge` event emitted only when the PR
        # itself is closed (mirrors in_review / fixing semantics).
        kinds = [e["event"] for e in gh.recorded_events]
        self.assertIn("pr_closed_without_merge", kinds)

    def test_closed_implementing_defers_when_pr_fetch_fails(self) -> None:
        # Both `_finalize_if_pr_merged` and `_finalize_if_issue_closed`
        # need a successful `gh.get_pr` call to act safely on a closed
        # issue with a pinned `pr_number`. If the PR fetch raises, the
        # merge helper returns False on "could not fetch" (same return
        # value as "not merged"); flipping the issue to `rejected`
        # from the closed-issue helper anyway would permanently
        # terminal-label a merged-PR issue whose merged-path finalize
        # is just retrying through a transient network blip. The fix:
        # the closed-issue helper must defer when its own fetch
        # raises, leaving the issue alone for the next tick.
        gh = FakeGitHubClient()
        issue = make_issue(154, label="implementing")
        issue.closed = True
        gh.add_issue(issue)
        # Pin a `pr_number` but DON'T add the PR to `gh.pulls`. The
        # fake's `get_pr` raises `KeyError` when the number is missing,
        # which models the real PyGithub failure surface (any exception
        # from `gh.get_pr` -- transient 5xx, rate limit, network blip).
        gh.seed_state(
            154, pr_number=15400, branch="orchestrator/issue-154",
            dev_agent="claude", dev_session_id="dev-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertNotIn((154, "rejected"), gh.label_history)
        self.assertNotIn((154, "done"), gh.label_history)
        self.assertNotIn(
            "closed_without_merge_at", gh.pinned_data(154),
        )
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_terminal_branch"].assert_not_called()

    def test_closed_implementing_defers_when_pr_merged(self) -> None:
        # Models the race where `_finalize_if_pr_merged` had a fetch
        # failure (returned False) but the PR is actually merged. The
        # closed-issue helper then runs its own fetch successfully,
        # sees the PR merged, and must NOT flip to `rejected` -- the
        # next tick will re-enter the merged-PR path. Otherwise a
        # merged PR's issue would be permanently mis-labeled.
        gh = FakeGitHubClient()
        issue = make_issue(155, label="implementing")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=15500,
            head_branch="orchestrator/issue-155",
            head=FakePRRef(sha="cafe1234"),
            merged=True,
            state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(
            155, pr_number=15500, branch="orchestrator/issue-155",
            dev_agent="claude", dev_session_id="dev-sess",
        )
        # Force `_finalize_if_pr_merged` to bail on the merged-path
        # `set_workflow_label("done")` write by intercepting
        # `gh.get_pr`: raise on the FIRST call (the merge helper) so
        # it returns False, succeed on the SECOND call (the closed
        # helper's own fetch).
        real_get_pr = gh.get_pr
        call_count = {"n": 0}

        def flaky_get_pr(pr_number):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated transient GitHub failure")
            return real_get_pr(pr_number)

        gh.get_pr = flaky_get_pr  # type: ignore[assignment]

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # No terminal label flip this tick: both finalize helpers
        # deferred. The next tick's `_finalize_if_pr_merged` will
        # succeed and run the proper merged-path cleanup.
        self.assertNotIn((155, "rejected"), gh.label_history)
        self.assertNotIn((155, "done"), gh.label_history)
        self.assertNotIn(
            "closed_without_merge_at", gh.pinned_data(155),
        )
        self.assertNotIn("merged_at", gh.pinned_data(155))
        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_terminal_branch"].assert_not_called()
