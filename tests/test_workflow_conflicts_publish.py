# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow
from orchestrator.stages import conflicts

from tests.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import (
    _FAKE_WT,
    _PatchedWorkflowMixin,
    _TEST_SPEC,
    _agent,
)


class ResolvingConflictPublishesAlreadyRebasedTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A worktree the dev already rebased onto base in an earlier run but
    never pushed reaches `_handle_resolving_conflict` diverged from the
    stale PR head (ahead AND behind it). Instead of the conservative
    `diverged_branch` park, the handler force-publishes -- but ONLY when
    the rebase is genuinely on base AND the stale PR head is one the
    orchestrator produced. Either guard failing keeps the park.
    """

    BRANCH = "orchestrator/issue-310"
    PR_NUMBER = 910
    PR_HEAD = "stalehead00"

    def _seed(self):
        gh = FakeGitHubClient()
        issue = make_issue(310, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha=self.PR_HEAD), state="open",
        )
        gh.add_pr(pr)
        gh.seed_state(
            310, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=2, conflict_round=0,
            # `_handle_documenting`'s success exits are the one place
            # production code records the orchestrator's pushed head, so
            # the force-publish guard recognises this state.
            docs_checked_sha=self.PR_HEAD,
        )
        return gh, issue, pr

    def _run_diverged(self, gh, issue, *, on_base, recognized):
        # The worktree is 4 ahead / 2 behind the remote PR head (a rebase
        # rewrote history). Patch the two safety probes directly so the
        # handler's publish-vs-park branch is exercised in isolation.
        # After a successful force-publish the handler probes
        # `rev-list HEAD..origin/<base>` to decide between the fast
        # path and a follow-up rebase; this scenario is "already on
        # base", so the probe returns 0 and the fast path fires.
        git_on_base = MagicMock(
            return_value=MagicMock(returncode=0, stdout="0\n", stderr=""),
        )
        with patch.object(
            conflicts, "_already_rebased_onto_base",
            MagicMock(return_value=on_base),
        ), patch.object(
            conflicts, "_pr_head_orchestrator_produced",
            MagicMock(return_value=recognized),
        ), patch.object(workflow, "_git", git_on_base):
            return self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(session_id="dev-sess"),
                branch_ahead_behind=(4, 2),
                push_branch=True,
                head_shas=("local", "local"),
            )

    def test_publishes_when_on_base_and_recognized(self) -> None:
        gh, issue, _ = self._seed()
        mocks = self._run_diverged(gh, issue, on_base=True, recognized=True)
        # Force-published over the stale PR head -> validating, no park.
        self.assertIn((310, "validating"), gh.label_history)
        data = gh.pinned_data(310)
        self.assertFalse(data.get("awaiting_human"))
        self.assertNotEqual(data.get("park_reason"), "diverged_branch")
        self.assertEqual(data.get("review_round"), 0)
        rounds = [
            e for e in gh.recorded_events
            if e.get("event") == "conflict_round"
            and e.get("action") == "incremented"
        ]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].get("outcome"), "recovered_push")
        # The push must be leased to the EXACT PR head we validated as
        # orchestrator-produced. A bare `_push_branch(spec, wt, branch)`
        # would do a fresh `ls-remote` and lease against whatever SHA
        # is live at push time, silently clobbering any foreign push
        # that landed between `gh.get_pr()` and this push.
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC, _FAKE_WT, self.BRANCH,
            force_with_lease=self.PR_HEAD,
        )

    def _assert_diverged_park(self, gh) -> None:
        # `_park_awaiting_human` records the reason on the audit event;
        # the durable `park_reason` field stays None by its contract.
        self.assertNotIn((310, "validating"), gh.label_history)
        self.assertTrue(gh.pinned_data(310).get("awaiting_human"))
        parks = [
            e for e in gh.recorded_events
            if e.get("event") == "park_awaiting_human"
            and e.get("reason") == "diverged_branch"
        ]
        self.assertEqual(len(parks), 1)

    def test_parks_when_not_on_base(self) -> None:
        gh, issue, _ = self._seed()
        self._run_diverged(gh, issue, on_base=False, recognized=True)
        self._assert_diverged_park(gh)

    def test_parks_when_pr_head_unrecognized(self) -> None:
        gh, issue, _ = self._seed()
        self._run_diverged(gh, issue, on_base=True, recognized=False)
        self._assert_diverged_park(gh)


if __name__ == "__main__":
    unittest.main()
