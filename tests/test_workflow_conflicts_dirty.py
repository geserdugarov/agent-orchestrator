# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator import workflow

from tests.workflow_helpers import (
    _ResolvingConflictMixin,
    _agent,
)

CONFLICT_ISSUE = 200
PUSH_BRANCH = "_push_branch"
AWAITING_HUMAN = "awaiting_human"
LABEL_VALIDATING = "validating"


class ResolvingConflictDirtyParkingTest(unittest.TestCase, _ResolvingConflictMixin):
    """Drive `_handle_resolving_conflict` through the dirty-worktree and
    rebase-in-progress parking branches: any leftover uncommitted edits or
    an unfinished rebase must park awaiting human rather than push an
    incomplete tree.
    """

    def test_dirty_worktree_parks_for_human(self) -> None:
        gh, issue = self._seed()[:2]

        merge_mock = MagicMock(return_value=(False, ["a.py"]))
        git_mock = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        # Note: the mixin's `_run` patches `_worktree_dirty_files` itself,
        # so wire dirty_files through the kwarg rather than a separate
        # outer patch (which `_run`'s patch would override).
        with (
            patch.object(workflow, "_rebase_base_into_worktree", merge_mock),
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
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="halfway there",
                ),
                push_branch=True,
                head_shas=["beforehead", "after"],
                dirty_files=["a.py"],
            )

        mocks[PUSH_BRANCH].assert_not_called()
        self.assertTrue(gh.pinned_data(CONFLICT_ISSUE).get(AWAITING_HUMAN))
        self.assertNotIn((CONFLICT_ISSUE, LABEL_VALIDATING), gh.label_history)

    def test_rebase_in_progress_parks_without_push(self) -> None:
        gh, issue, _ = self._seed()
        mocks, _, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=False,
            conflicted_files=["a.py"],
            head_shas=["beforehead", "after"],
            push_branch=True,
            run_agent_result=_agent(
                session_id="dev-sess",
                last_message="I resolved one stop but another remains",
            ),
            rebase_in_progress=True,
        )

        mocks[PUSH_BRANCH].assert_not_called()
        state = gh.pinned_data(CONFLICT_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))
        self.assertNotIn((CONFLICT_ISSUE, LABEL_VALIDATING), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("rebase is still in progress", last_comment)
        self.assertIn("I resolved one stop", last_comment)

    def test_recovered_commits_park_without_push(self) -> None:
        # Crash recovery with leftover dirty files: a previous tick
        # committed a resolution but ALSO left uncommitted edits, then
        # crashed before the dirty check ran. Pushing now would publish
        # a SHA that silently omits the leftover edits, and the reviewer
        # at validating would later run on a tree that does not match
        # the PR. Park instead.
        gh, issue, _ = self._seed()

        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run_resolving_conflict(
                gh,
                issue,
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(1, 0),
                dirty_files=["leftover.py"],
            )
        # No push, no merge attempt, no label flip.
        mocks[PUSH_BRANCH].assert_not_called()
        merge_mock.assert_not_called()
        self.assertNotIn((CONFLICT_ISSUE, LABEL_VALIDATING), gh.label_history)
        self.assertTrue(gh.pinned_data(CONFLICT_ISSUE).get(AWAITING_HUMAN))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted", last_comment)

    def test_new_commit_rebase_parks_without_push(self) -> None:
        # Clean rebase produced a new HEAD but the
        # worktree carries pre-existing dirty files. Pushing the merge
        # rebased branch without those edits would publish an incomplete branch.
        gh, issue, _ = self._seed()
        mocks, _, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
            dirty_files=["leftover.py"],
        )
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertNotIn((CONFLICT_ISSUE, LABEL_VALIDATING), gh.label_history)
        state = gh.pinned_data(CONFLICT_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))

    def test_noop_rebase_parks_without_label_flip(self) -> None:
        # Clean no-op rebase (HEAD didn't change because base hadn't
        # moved) but the worktree carries dirty files. The reviewer
        # at validating reads the worktree directly, so flipping with a
        # dirty tree would let the agent vote on something that does NOT
        # match the PR head. Park instead.
        gh, issue, _ = self._seed()
        mocks, _, _ = self._run_with_merge(
            gh,
            issue,
            merge_succeeded=True,
            head_shas=["samehead", "samehead"],
            push_branch=True,
            dirty_files=["leftover.py"],
        )
        mocks[PUSH_BRANCH].assert_not_called()
        self.assertNotIn((CONFLICT_ISSUE, LABEL_VALIDATING), gh.label_history)
        state = gh.pinned_data(CONFLICT_ISSUE)
        self.assertTrue(state.get(AWAITING_HUMAN))


if __name__ == "__main__":
    unittest.main()
