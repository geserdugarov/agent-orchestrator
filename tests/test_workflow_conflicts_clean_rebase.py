# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow
from orchestrator.github import BASE_SYNC_HOLD_LABEL

from tests.fakes import (
    FakeGitHubClient,
    FakeLabel,
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


class HandleResolvingConflictUsesAuthedFetchTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The conflict-resolution fetch must run inside the agent-writable
    worktree under the same security envelope as `_push_branch`: askpass-
    based auth, detached global/system config, blocked hooks/fsmonitor/
    credential helpers. `_handle_resolving_conflict` MUST route the
    fetch through `_authed_fetch` (not plain `_git`) so a planted url
    rewrite / credential helper / hooksPath cannot exfiltrate the token.
    """

    def test_fetch_call_targets_authed_fetch_with_explicit_refspec(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(450, label="resolving_conflict")
        gh.add_issue(issue)
        pr = FakePR(
            number=850, head_branch="orchestrator/geserdugarov__agent-orchestrator/issue-450",
            head=FakePRRef(sha="cafe1234"),
            mergeable=False, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            450, pr_number=850, branch="orchestrator/geserdugarov__agent-orchestrator/issue-450",
            dev_agent="claude", dev_session_id="dev-sess",
            conflict_round=0,
        )

        merge_mock = MagicMock(return_value=(True, []))

        # The mixin's `_run` itself patches `_authed_fetch` to a default
        # success mock, so we read the call back from the returned
        # mocks dict rather than installing our own outer patch (which
        # `_run`'s inner `with` would override).
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                head_shas=["sha", "sha"],
            )

        authed_fetch_mock = mocks["_authed_fetch"]
        # Two fetches per fresh resolving_conflict round: first for the
        # PR branch (so the SHA-alignment / unpushed-recovery check sees
        # current `origin/<branch>`), then for the base branch (so the
        # upcoming `git rebase` sees current `origin/<base>`).
        self.assertEqual(authed_fetch_mock.call_count, 2)
        refspecs = [call.args[1] for call in authed_fetch_mock.call_args_list]
        cwds = [call.kwargs["cwd"] for call in authed_fetch_mock.call_args_list]
        # All fetches run inside the WORKTREE (agent-writable), where
        # the hardening actually matters -- not `target_root`.
        for cwd in cwds:
            self.assertEqual(cwd, _FAKE_WT)
        # All refspecs use the explicit `+refs/heads/X:refs/remotes/origin/X`
        # form so single-branch clones still create the remote-tracking ref.
        for refspec in refspecs:
            self.assertTrue(
                refspec.startswith("+"),
                f"refspec {refspec!r} should start with '+' for force-update",
            )
        # Verify both refs are fetched: the PR branch and the base branch.
        joined = " ".join(refspecs)
        self.assertIn(
            f"refs/remotes/origin/{_TEST_SPEC.base_branch}", joined,
            "expected base-branch fetch refspec",
        )
        self.assertIn(
            "refs/remotes/origin/orchestrator/geserdugarov__agent-orchestrator/issue-450", joined,
            "expected PR-branch fetch refspec",
        )


class ResolvingConflictCleanRebaseTest(
    unittest.TestCase, _ResolvingConflictMixin
):
    """Drive `_handle_resolving_conflict` through the clean base-rebase
    routing: the no-agent rebase push, the hold-label pause, the no-op /
    cap rounds, and the PR-state terminal short-circuits.
    """

    def test_clean_rebase_pushes_and_flips_to_validating(self) -> None:
        # A clean base rebase that actually moved HEAD pushes the
        # rebased branch and hands straight back to `validating`. Docs
        # do not run here -- the single docs pass runs after reviewer
        # approval before `in_review` via the final-docs handoff.
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )
        # Agent must NOT be spawned -- a clean base rebase does not need
        # the dev to do anything.
        mocks["run_agent"].assert_not_called()
        merge_mock.assert_called_once()
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.BRANCH,
            force_with_lease="beforehead",
        )
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)
        self.assertIn("last_conflict_resolved_at", data)

    def test_hold_base_sync_label_pauses_resolving_conflict(self) -> None:
        gh, issue, pr = self._seed()
        issue.labels.append(FakeLabel(BASE_SYNC_HOLD_LABEL))
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )

        mocks["run_agent"].assert_not_called()
        merge_mock.assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.label_history, [])
        data = gh.pinned_data(200)
        self.assertEqual(data.get("conflict_round"), 0)
        self.assertFalse(data.get("awaiting_human"))

    def test_clean_rebase_already_up_to_date_skips_push_and_ticks_round(
        self,
    ) -> None:
        # When the base hasn't moved (e.g. unmergeability is purely due to
        # branch protection), the rebase is a no-op and there is nothing to
        # push. The handler must still increment `conflict_round` so the
        # cap eventually fires -- otherwise the in_review <-> resolving
        # cycle would loop forever. The label hands back to `validating`
        # so the next reviewer round / in_review tick can re-evaluate;
        # every other resolving_conflict exit also targets `validating`
        # now, so there's no `documenting` detour to skip relative to
        # the pushed paths.
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["samehead", "samehead"],
            push_branch=True,
        )
        mocks["run_agent"].assert_not_called()
        # Nothing to push when base hasn't moved relative to the branch.
        mocks["_push_branch"].assert_not_called()
        self.assertIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "documenting"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("conflict_round"), 1)

    def test_no_op_rebase_loops_until_cap_fires(self) -> None:
        # A PR stuck unmergeable purely due to branch protection would
        # bounce between in_review and resolving_conflict with the rebase
        # always a no-op. The cap must fire after MAX_CONFLICT_ROUNDS
        # such no-op rounds.
        gh, issue, pr = self._seed(extra_state={"conflict_round": 2})
        with patch.object(config, "MAX_CONFLICT_ROUNDS", 3):
            mocks, merge_mock, git_mock = self._run_with_merge(
                gh, issue,
                merge_succeeded=True,
                head_shas=["samehead", "samehead"],
                push_branch=True,
            )
        # One more no-op round consumed: 2 -> 3.
        self.assertEqual(gh.pinned_data(200).get("conflict_round"), 3)
        # On the next tick we'd be at the cap; simulate by re-running:
        with patch.object(config, "MAX_CONFLICT_ROUNDS", 3):
            mocks2, merge_mock2, _ = self._run_with_merge(
                gh, issue,
                merge_succeeded=True,
                head_shas=["samehead", "samehead"],
                push_branch=True,
            )
        merge_mock2.assert_not_called()
        self.assertTrue(gh.pinned_data(200).get("awaiting_human"))

    def test_cap_exhausted_parks_awaiting_human(self) -> None:
        # `MAX_CONFLICT_ROUNDS` defaults to 3; once the counter reaches it,
        # the handler must park instead of attempting another round.
        gh, issue, pr = self._seed(extra_state={"conflict_round": 3})
        with patch.object(config, "MAX_CONFLICT_ROUNDS", 3):
            mocks, merge_mock, git_mock = self._run_with_merge(
                gh, issue, merge_succeeded=True,
            )
        # Neither merge nor agent runs on the cap branch.
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        # Label stays on `resolving_conflict` -- no flip.
        self.assertNotIn((200, "validating"), gh.label_history)
        self.assertNotIn((200, "done"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("MAX_CONFLICT_ROUNDS", last_comment)

    def test_pr_already_merged_externally_finalizes_to_done(self) -> None:
        # Mirror the in_review terminal: a human merged the PR (perhaps
        # after manually resolving conflicts) while we were resolving.
        gh, issue, pr = self._seed(pr_merged=True, pr_state="closed")
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue, merge_succeeded=True,
        )
        # No merge / agent / push attempt -- terminal short-circuit.
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((200, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(200))
        self.assertTrue(issue.closed)

    def test_pr_closed_unmerged_finalizes_to_rejected(self) -> None:
        gh, issue, pr = self._seed(pr_state="closed")
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue, merge_succeeded=True,
        )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertIn((200, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(200))
        # PR is gone -- the orchestrator-owned branch and worktree must
        # come down on the rejected terminal too, mirroring the merged
        # path. Failure to clean up here is exactly the bug this test
        # guards against.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 200,
            branch="orchestrator/geserdugarov__agent-orchestrator/issue-200",
        )

    def test_manually_closed_with_open_pr_marks_rejected_without_cleanup(
        self,
    ) -> None:
        # Mirror the in_review counterpart: closing the issue while the
        # PR is still open is a human stop signal. The handler flips the
        # label to `rejected` but deliberately leaves the branch /
        # worktree alone (operator may still want to salvage the PR).
        gh, issue, pr = self._seed(pr_state="open")
        issue.closed = True
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue, merge_succeeded=True,
        )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertIn((200, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(200))
        mocks["_cleanup_terminal_branch"].assert_not_called()

        # Documented caveat: a subsequent PR close is not observed by
        # the orchestrator -- the closed-issue sweep only covers
        # `in_review` / `resolving_conflict`, and `rejected` is terminal
        # in the dispatcher. Operator must clean up by hand.
        pr.state = "closed"
        pollable_numbers = {i.number for i in gh.list_pollable_issues()}
        self.assertNotIn(
            200, pollable_numbers,
            "rejected closed issues are not swept, so the orchestrator "
            "cannot observe the later PR close; cleanup must be manual.",
        )

    def test_no_pr_number_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(201, label="resolving_conflict")
        gh.add_issue(issue)
        gh.seed_state(201)

        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock,
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
            )
        merge_mock.assert_not_called()
        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(201).get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
