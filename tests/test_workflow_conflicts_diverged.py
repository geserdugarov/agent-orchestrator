# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from pathlib import Path
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
    _ResolvingConflictMixin,
    _TEST_SPEC,
    _agent,
)


class ResolvingConflictStaleDivergedTest(
    unittest.TestCase, _ResolvingConflictMixin
):
    """Drive `_handle_resolving_conflict` through the conservative
    stale / diverged worktree parks: a worktree behind or diverged from
    `origin/<branch>` must refuse to force-push and park awaiting human.
    """

    def test_stale_worktree_parks_awaiting_human(self) -> None:
        # Worktree behind `origin/<branch>` (someone pushed to the PR
        # branch out-of-band). Force-pushing the local state would
        # clobber the real PR head; refuse and park.
        gh, issue, pr = self._seed()

        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(0, 2),
            )
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        mocks["run_agent"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("stale or diverged", last_comment)

    def test_diverged_worktree_parks_awaiting_human(self) -> None:
        # Both ahead and behind: histories diverged. Cannot safely push
        # without rewriting remote history that may have value.
        gh, issue, pr = self._seed()

        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(1, 1),
            )
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)


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


class ResolvingConflictPublishGuardUnitTest(unittest.TestCase):
    """Unit tests for the two safety probes behind the already-rebased
    force-publish decision."""

    def _pr(self, sha):
        return FakePR(number=1, head_branch="b", head=FakePRRef(sha=sha))

    def test_pr_head_orchestrator_produced_recognizes_docs_checked_sha(
        self,
    ) -> None:
        # `docs_checked_sha` is the only key production code persists for
        # an orchestrator-produced PR head (set by `_handle_documenting`'s
        # success exits). PR heads from earlier in the lifecycle (the
        # initial implementing push, an intermediate fixing push) are not
        # currently recorded, so the guard refuses those by design rather
        # than guessing.
        gh = FakeGitHubClient()
        issue = make_issue(1, label="resolving_conflict")
        gh.add_issue(issue)
        gh.seed_state(1, docs_checked_sha="abc")
        st = gh.read_pinned_state(issue)
        self.assertTrue(
            conflicts._pr_head_orchestrator_produced(st, self._pr("abc")),
        )
        self.assertFalse(
            conflicts._pr_head_orchestrator_produced(st, self._pr("xyz")),
        )
        # An empty/missing head never matches.
        self.assertFalse(
            conflicts._pr_head_orchestrator_produced(st, self._pr("")),
        )
        # No `docs_checked_sha` recorded -- e.g. a pre-docs validating
        # PR head -- must NOT match an empty-string lookup either.
        gh2 = FakeGitHubClient()
        issue2 = make_issue(2, label="resolving_conflict")
        gh2.add_issue(issue2)
        gh2.seed_state(2, dev_agent="claude")
        st2 = gh2.read_pinned_state(issue2)
        self.assertFalse(
            conflicts._pr_head_orchestrator_produced(st2, self._pr("abc")),
        )

    def test_already_rebased_onto_base_reads_rev_list_count(self) -> None:
        fetch_ok = MagicMock(return_value=MagicMock(returncode=0))
        with patch.object(workflow, "_authed_fetch", fetch_ok), \
             patch.object(
                 workflow, "_git_hardened",
                 MagicMock(return_value=MagicMock(returncode=0, stdout="0\n")),
             ):
            self.assertTrue(
                conflicts._already_rebased_onto_base(_TEST_SPEC, Path("/tmp/x")),
            )
        with patch.object(workflow, "_authed_fetch", fetch_ok), \
             patch.object(
                 workflow, "_git_hardened",
                 MagicMock(return_value=MagicMock(returncode=0, stdout="3\n")),
             ):
            self.assertFalse(
                conflicts._already_rebased_onto_base(_TEST_SPEC, Path("/tmp/x")),
            )

    def test_already_rebased_onto_base_fails_closed_on_fetch_failure(
        self,
    ) -> None:
        # Without proving HEAD is on the CURRENT base tip, we cannot
        # let the force-publish path enable. A stale
        # `<remote>/<base>` ref would let `rev-list HEAD..<remote>/<base>`
        # report "no missing commits" purely because the local mirror is
        # behind the real base -- mis-classifying a behind-base worktree
        # as already-rebased and force-publishing it.
        fetch_fail = MagicMock(
            return_value=MagicMock(returncode=1, stdout="", stderr="boom"),
        )
        rev_list_zero = MagicMock(
            return_value=MagicMock(returncode=0, stdout="0\n"),
        )
        with patch.object(workflow, "_authed_fetch", fetch_fail), \
             patch.object(workflow, "_git_hardened", rev_list_zero):
            self.assertFalse(
                conflicts._already_rebased_onto_base(_TEST_SPEC, Path("/tmp/x")),
            )
        # And the rev-list probe must be skipped entirely on fetch failure
        # -- there is no value reading a count off a stale ref.
        rev_list_zero.assert_not_called()


if __name__ == "__main__":
    unittest.main()
