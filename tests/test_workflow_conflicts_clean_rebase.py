# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import config, workflow

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
    _issue_branch,
)

FETCH_ISSUE = 450
FETCH_PR = 850
CONFLICT_ISSUE = 200
MISSING_PR_ISSUE = 201
RUN_AGENT = "run_agent"
CONFLICT_ROUND = "conflict_round"
UNCHANGED_HEAD = "samehead"
MAX_CONFLICT_ROUNDS_SETTING = "MAX_CONFLICT_ROUNDS"


def _seed_fetch_case():
    github = FakeGitHubClient()
    issue = make_issue(FETCH_ISSUE, label="resolving_conflict")
    github.add_issue(issue)
    github.add_pr(
        FakePR(
            number=FETCH_PR,
            head_branch=_issue_branch(FETCH_ISSUE),
            head=FakePRRef(sha="cafe1234"),
            mergeable=False,
            check_state="success",
        ),
    )
    github.seed_state(
        FETCH_ISSUE,
        pr_number=FETCH_PR,
        branch=_issue_branch(FETCH_ISSUE),
        dev_agent="claude",
        dev_session_id="dev-sess",
        conflict_round=0,
    )
    return github, issue


def _assert_fetch_calls(test_case, authed_fetch_mock) -> None:
    test_case.assertEqual(authed_fetch_mock.call_count, 2)
    fetch_calls = authed_fetch_mock.call_args_list
    for fetch_call in fetch_calls:
        test_case.assertEqual(fetch_call.kwargs["cwd"], _FAKE_WT)
        test_case.assertTrue(
            fetch_call.args[1].startswith("+"),
            f"refspec {fetch_call.args[1]!r} should start with '+' for force-update",
        )
    joined_refspecs = " ".join(recorded_call.args[1] for recorded_call in fetch_calls)
    test_case.assertIn(
        f"refs/remotes/origin/{_TEST_SPEC.base_branch}",
        joined_refspecs,
        "expected base-branch fetch refspec",
    )
    test_case.assertIn(
        "refs/remotes/origin/orchestrator/geserdugarov__agent-orchestrator/issue-450",
        joined_refspecs,
        "expected PR-branch fetch refspec",
    )


def _clean_state(github):
    return github.pinned_data(CONFLICT_ISSUE)


def _terminal_state(github):
    return github.pinned_data(CONFLICT_ISSUE)


class AuthedFetchRoutingTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The conflict-resolution fetch must run inside the agent-writable
    worktree under the same security envelope as `_push_branch`: askpass-
    based auth, detached global/system config, blocked hooks/fsmonitor/
    credential helpers. `_handle_resolving_conflict` MUST route the
    fetch through `_authed_fetch` (not plain `_git`) so a planted url
    rewrite / credential helper / hooksPath cannot exfiltrate the token.
    """

    def test_fetch_uses_explicit_refspec(self) -> None:
        gh, issue = _seed_fetch_case()
        merge_mock = MagicMock(return_value=(True, []))

        # The mixin's `_run` itself patches `_authed_fetch` to a default
        # success mock, so we read the call back from the returned
        # mocks dict rather than installing our own outer patch (which
        # `_run`'s inner `with` would override).
        with patch.object(
            workflow,
            "_rebase_base_into_worktree",
            merge_mock,
        ):
            mocks = self._run_resolving_conflict(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
                head_shas=["sha", "sha"],
            )

        authed_fetch_mock = mocks["_authed_fetch"]
        # Two fetches per fresh resolving_conflict round: first for the
        # PR branch (so the SHA-alignment / unpushed-recovery check sees
        # current `origin/<branch>`), then for the base branch (so the
        # upcoming `git rebase` sees current `origin/<base>`).
        _assert_fetch_calls(self, authed_fetch_mock)


class ResolvingConflictCleanRebaseTest(unittest.TestCase, _ResolvingConflictMixin):
    """Drive `_handle_resolving_conflict` through the clean base-rebase
    routing: the no-agent rebase push, the no-op / cap rounds, and the
    PR-state terminal short-circuits.
    """

    def test_pushes_and_routes_to_validating(self) -> None:
        # A clean base rebase that actually moved HEAD pushes the
        # rebased branch and hands straight back to `validating`. Docs
        # do not run here -- the single docs pass runs after reviewer
        # approval before `in_review` via the final-docs handoff.
        gh, issue, _ = self._seed()
        mocks, merge_mock, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
        )
        # Agent must NOT be spawned -- a clean base rebase does not need
        # the dev to do anything.
        mocks[RUN_AGENT].assert_not_called()
        merge_mock.assert_called_once()
        mocks["_push_branch"].assert_called_once_with(
            _TEST_SPEC,
            _FAKE_WT,
            self.issue_branch,
            force_with_lease="beforehead",
        )
        self.assertIn((CONFLICT_ISSUE, "validating"), gh.label_history)
        self.assertNotIn((CONFLICT_ISSUE, "documenting"), gh.label_history)
        state = _clean_state(gh)
        self.assertEqual(state.get("review_round"), 0)
        self.assertEqual(state.get(CONFLICT_ROUND), 1)
        self.assertIn("last_conflict_resolved_at", state)

    def test_up_to_date_skips_push_and_bumps_round(
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
        gh, issue, _ = self._seed()
        mocks, _, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
            head_shas=[UNCHANGED_HEAD, UNCHANGED_HEAD],
            push_branch=True,
        )
        mocks[RUN_AGENT].assert_not_called()
        # Nothing to push when base hasn't moved relative to the branch.
        mocks["_push_branch"].assert_not_called()
        self.assertIn((CONFLICT_ISSUE, "validating"), gh.label_history)
        self.assertNotIn((CONFLICT_ISSUE, "documenting"), gh.label_history)
        state = _clean_state(gh)
        self.assertEqual(state.get("review_round"), 0)
        self.assertEqual(state.get(CONFLICT_ROUND), 1)

    def test_no_op_rebase_loops_until_cap_fires(self) -> None:
        # A PR stuck unmergeable purely due to branch protection would
        # bounce between in_review and resolving_conflict with the rebase
        # always a no-op. The cap must fire after MAX_CONFLICT_ROUNDS
        # such no-op rounds.
        gh, issue, _ = self._seed(extra_state={CONFLICT_ROUND: 2})
        with patch.object(config, MAX_CONFLICT_ROUNDS_SETTING, 3):
            self._run_with_merge(
                gh,
                issue,
                merge_succeeded=True,
                head_shas=[UNCHANGED_HEAD, UNCHANGED_HEAD],
                push_branch=True,
            )
        # One more no-op round consumed: 2 -> 3.
        self.assertEqual(gh.pinned_data(CONFLICT_ISSUE).get(CONFLICT_ROUND), 3)
        # On the next tick we'd be at the cap; simulate by re-running:
        with patch.object(config, MAX_CONFLICT_ROUNDS_SETTING, 3):
            _, merge_mock, _ = self._run_with_merge(
                gh,
                issue,
                merge_succeeded=True,
                head_shas=[UNCHANGED_HEAD, UNCHANGED_HEAD],
                push_branch=True,
            )
        merge_mock.assert_not_called()
        self.assertTrue(_clean_state(gh).get("awaiting_human"))

    def test_cap_exhausted_parks_awaiting_human(self) -> None:
        # `MAX_CONFLICT_ROUNDS` defaults to 3; once the counter reaches it,
        # the handler must park instead of attempting another round.
        gh, issue, _ = self._seed(extra_state={CONFLICT_ROUND: 3})
        with patch.object(config, MAX_CONFLICT_ROUNDS_SETTING, 3):
            mocks, merge_mock, _ = self._run_with_merge(
                gh,
                issue,
                merge_succeeded=True,
            )
        # Neither merge nor agent runs on the cap branch.
        merge_mock.assert_not_called()
        mocks[RUN_AGENT].assert_not_called()
        state = _clean_state(gh)
        self.assertTrue(state.get("awaiting_human"))
        # Label stays on `resolving_conflict` -- no flip.
        self.assertNotIn((CONFLICT_ISSUE, "validating"), gh.label_history)
        self.assertNotIn((CONFLICT_ISSUE, "done"), gh.label_history)
        self.assertIn(MAX_CONFLICT_ROUNDS_SETTING, gh.posted_comments[-1][1])


class ResolvingConflictTerminalRoutingTest(
    unittest.TestCase,
    _ResolvingConflictMixin,
):
    """Finalize terminal pull requests and park missing PR state."""

    def test_pr_merged_externally_finalizes_to_done(self) -> None:
        # Mirror the in_review terminal: a human merged the PR (perhaps
        # after manually resolving conflicts) while we were resolving.
        gh, issue, _ = self._seed(pr_merged=True, pr_state="closed")
        mocks, merge_mock, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
        )
        # No merge / agent / push attempt -- terminal short-circuit.
        merge_mock.assert_not_called()
        mocks[RUN_AGENT].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        self.assertIn((CONFLICT_ISSUE, "done"), gh.label_history)
        self.assertIn("merged_at", _terminal_state(gh))
        self.assertTrue(issue.closed)

    def test_pr_closed_unmerged_finalizes_to_rejected(self) -> None:
        gh, issue, _ = self._seed(pr_state="closed")
        mocks, merge_mock, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
        )
        merge_mock.assert_not_called()
        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((CONFLICT_ISSUE, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", _terminal_state(gh))
        # PR is gone -- the orchestrator-owned branch and worktree must
        # come down on the rejected terminal too, mirroring the merged
        # path. Failure to clean up here is exactly the bug this test
        # guards against.
        mocks["_cleanup_terminal_branch"].assert_called_once_with(
            gh,
            _TEST_SPEC,
            CONFLICT_ISSUE,
            branch=_issue_branch(CONFLICT_ISSUE),
        )

    def test_manual_close_rejects_without_cleanup(
        self,
    ) -> None:
        # Mirror the in_review counterpart: closing the issue while the
        # PR is still open is a human stop signal. The handler flips the
        # label to `rejected` but deliberately leaves the branch /
        # worktree alone (operator may still want to salvage the PR).
        gh, issue, pr = self._seed(pr_state="open")
        issue.closed = True
        mocks, merge_mock, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
        )
        merge_mock.assert_not_called()
        mocks[RUN_AGENT].assert_not_called()
        self.assertIn((CONFLICT_ISSUE, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", _terminal_state(gh))
        mocks["_cleanup_terminal_branch"].assert_not_called()

        # Documented caveat: a subsequent PR close is not observed by
        # the orchestrator -- the closed-issue sweep only covers
        # `in_review` / `resolving_conflict`, and `rejected` is terminal
        # in the dispatcher. Operator must clean up by hand.
        pr.state = "closed"
        self.assertNotIn(
            CONFLICT_ISSUE,
            {pollable_issue.number for pollable_issue in gh.list_pollable_issues()},
            "rejected closed issues are not swept, so the orchestrator "
            "cannot observe the later PR close; cleanup must be manual.",
        )

    def test_no_pr_number_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(MISSING_PR_ISSUE, label="resolving_conflict")
        gh.add_issue(issue)
        gh.seed_state(MISSING_PR_ISSUE)

        merge_mock = MagicMock(return_value=(True, []))
        git_mock = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with (
            patch.object(
                workflow,
                "_rebase_base_into_worktree",
                merge_mock,
            ),
            patch.object(workflow, "_git", git_mock),
            patch.object(
                workflow,
                "_git_hardened",
                git_mock,
            ),
        ):
            mocks = self._run_resolving_conflict(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
            )
        merge_mock.assert_not_called()
        mocks[RUN_AGENT].assert_not_called()
        self.assertTrue(gh.pinned_data(MISSING_PR_ISSUE).get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
