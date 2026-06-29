# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import workflow

from tests.workflow_helpers import (
    _ResolvingConflictMixin,
    _TEST_SPEC,
    _agent,
)


class ResolvingConflictDirtyParkingTest(
    unittest.TestCase, _ResolvingConflictMixin
):
    """Drive `_handle_resolving_conflict` through the dirty-worktree and
    rebase-in-progress parking branches: any leftover uncommitted edits or
    an unfinished rebase must park awaiting human rather than push an
    incomplete tree.
    """

    def test_agent_left_dirty_worktree_parks_awaiting_human(self) -> None:
        gh, issue, pr = self._seed()

        merge_mock = MagicMock(return_value=(False, ["a.py"]))
        git_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        # Note: the mixin's `_run` patches `_worktree_dirty_files` itself,
        # so wire dirty_files through the kwarg rather than a separate
        # outer patch (which `_run`'s patch would override).
        with patch.object(
            workflow, "_rebase_base_into_worktree", merge_mock
        ), patch.object(workflow, "_git", git_mock), patch.object(
            workflow, "_git_hardened", git_mock,
        ):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(
                    session_id="dev-sess", last_message="halfway there",
                ),
                push_branch=True,
                head_shas=["beforehead", "after"],
                dirty_files=["a.py"],
            )

        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)

    def test_agent_left_rebase_in_progress_parks_without_push(self) -> None:
        gh, issue, pr = self._seed()
        mocks, merge_mock, git_mock = self._run_with_merge(
            gh, issue,
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

        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((200, "validating"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("rebase is still in progress", last_comment)
        self.assertIn("I resolved one stop", last_comment)

    def test_dirty_recovered_commits_parks_without_push(self) -> None:
        # Crash recovery with leftover dirty files: a previous tick
        # committed a resolution but ALSO left uncommitted edits, then
        # crashed before the dirty check ran. Pushing now would publish
        # a SHA that silently omits the leftover edits, and the reviewer
        # at validating would later run on a tree that does not match
        # the PR. Park instead.
        gh, issue, pr = self._seed()

        merge_mock = MagicMock(return_value=(True, []))

        with patch.object(workflow, "_rebase_base_into_worktree", merge_mock):
            mocks = self._run(
                lambda: workflow._handle_resolving_conflict(
                    gh, _TEST_SPEC, issue,
                ),
                run_agent=_agent(),
                push_branch=True,
                branch_ahead_behind=(1, 0),
                dirty_files=["leftover.py"],
            )
        # No push, no merge attempt, no label flip.
        mocks["_push_branch"].assert_not_called()
        merge_mock.assert_not_called()
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted", last_comment)

    def test_dirty_clean_rebase_with_new_commit_parks_without_push(self) -> None:
        # Clean rebase produced a new HEAD but the
        # worktree carries pre-existing dirty files. Pushing the merge
        # rebased branch without those edits would publish an incomplete branch.
        gh, issue, pr = self._seed()
        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["beforehead", "merged"],
            push_branch=True,
            dirty_files=["leftover.py"],
        )
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))

    def test_dirty_clean_rebase_no_op_parks_without_flip(self) -> None:
        # Clean no-op rebase (HEAD didn't change because base hadn't
        # moved) but the worktree carries dirty files. The reviewer
        # at validating reads the worktree directly, so flipping with a
        # dirty tree would let the agent vote on something that does NOT
        # match the PR head. Park instead.
        gh, issue, pr = self._seed()
        mocks, merge_mock, _ = self._run_with_merge(
            gh, issue,
            merge_succeeded=True,
            head_shas=["samehead", "samehead"],
            push_branch=True,
            dirty_files=["leftover.py"],
        )
        mocks["_push_branch"].assert_not_called()
        self.assertNotIn((200, "validating"), gh.label_history)
        data = gh.pinned_data(200)
        self.assertTrue(data.get("awaiting_human"))


if __name__ == "__main__":
    unittest.main()
