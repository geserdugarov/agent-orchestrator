# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The git owners the implementing stage reads, and the boundary each pins.

This stage owns the dev session and nothing under it: the checkout it spawns
in, the branch name it publishes on, the HEAD watermark its disposition turns
on, the push, the commit subject and title behind a fresh PR, the
unpushed-branch probe the question relabel refuses on, and the auto-rebase park
vocabulary the `/orchestrator continue` guard declines all belong to `git/`.
Each is imported from that owner rather than read off the
`orchestrator.workflow` facade, so a patch that has to intercept one lands on
the owner. Every case patches BOTH -- the owner mock has to answer and the
facade guard has to stay untouched -- which is what fails if a call site drifts
back to `_wf`.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.git.base_sync import state as _base_sync_state
from orchestrator.workflow.stages.implementing import (
    continue_command as _continue_command,
    publication as _publication,
    question_relabel as _question_relabel,
    spawn as _spawn,
    state as _state,
)

from tests.fakes import FakeComment, FakeGitHubClient, FakeUser, make_issue
from tests.workflow_helpers import _FAKE_WT, _TEST_SPEC, _agent
from tests.workflow_owner_boundaries import OwnerBoundaryMixin

BOUNDARY_ISSUE = 760
BOUNDARY_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-760"
BEFORE_SHA = "beforeAA"
REPLY_ID = 7600
AUTHOR = "alice"
CUSTOM_REBASE_PARK = "auto_base_rebase_custom"
QUESTION_COMMITS_PARK = "question_commits"
UNSAFE_RELABEL_PARK = "question_unsafe_relabel"
FIRST_SUBJECT = "fix: stop the leak"
INFERRED_PREFIX = "fix"
_MISSING_WT = Path("/tmp/orchestrator-test-boundary-no-such-worktree")

AUTO_REBASE_PARK_REASONS = "_AUTO_REBASE_PARK_REASONS"


def _seeded(*comments, **state_fields):
    """An `implementing` issue and the pinned state its owners read."""
    gh = FakeGitHubClient()
    issue = make_issue(
        BOUNDARY_ISSUE, label="implementing", comments=list(comments),
    )
    gh.add_issue(issue)
    gh.seed_state(BOUNDARY_ISSUE, **state_fields)
    return gh, issue, gh.read_pinned_state(issue)


class FreshSpawnBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The checkout a fresh dev run prepares lands on the git owners."""

    def test_recovered_worktree_shortcut_reads_owners(self) -> None:
        # The shortcut is decided entirely by these reads: a worktree that
        # already carries commits skips the agent, so a seam answered off the
        # facade would let a real checkout decide whether the dev runs.
        gh, issue, state = _seeded()
        with self.git_seams_on_owners(
            _ensure_worktree=MagicMock(return_value=_FAKE_WT),
            _resolve_branch_name=MagicMock(return_value=BOUNDARY_BRANCH),
            _head_sha=MagicMock(return_value=BEFORE_SHA),
            _has_new_commits=MagicMock(return_value=True),
        ):
            prepared = _spawn._prepare_dev_run(gh, _TEST_SPEC, issue, state)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.before_sha, BEFORE_SHA)
        # The branch this run worked on is what the next tick has to resolve.
        self.assertEqual(state.get(_state._BRANCH), BOUNDARY_BRANCH)


class PublicationBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The push and the PR title a fresh commit earns land on their owners."""

    def test_pr_publication_reads_owners(self) -> None:
        gh, issue, state = _seeded()
        with self.git_seams_on_owners(
            _worktree_path=MagicMock(return_value=_FAKE_WT),
            _resolve_branch_name=MagicMock(return_value=BOUNDARY_BRANCH),
            _push_branch=MagicMock(return_value=True),
            _first_commit_subject=MagicMock(return_value=FIRST_SUBJECT),
            _infer_subject_prefix=MagicMock(return_value=INFERRED_PREFIX),
        ):
            _publication._on_commits(gh, _TEST_SPEC, issue, state, _agent())
        self.assertIn((BOUNDARY_ISSUE, "validating"), gh.label_history)
        # The title comes off the branch's own first subject, not the issue.
        self.assertEqual(gh.opened_prs[-1].title, FIRST_SUBJECT)
        self.assertEqual(state.get(_state._BRANCH), BOUNDARY_BRANCH)


class QuestionRelabelBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The read-only hazard check reads the tree and branch off git."""

    def test_unsafe_relabel_reads_owners(self) -> None:
        # A reaped worktree with a surviving branch is the case only the
        # recovery probe can answer, and refusing on it is what keeps a
        # question agent's commits from being pushed as a dev implementation.
        gh, issue, state = _seeded(
            awaiting_human=True, park_reason=QUESTION_COMMITS_PARK,
        )
        with self.git_seams_on_owners(
            _worktree_path=MagicMock(return_value=_MISSING_WT),
            _branch_has_unpushed_commits=MagicMock(
                return_value=BOUNDARY_BRANCH,
            ),
        ):
            handled = _question_relabel._handle_stale_question_park(
                gh, _TEST_SPEC, issue, state,
            )
        self.assertTrue(handled)
        self.assertEqual(state.get(_state._PARK_REASON), UNSAFE_RELABEL_PARK)
        self.assertIn(BOUNDARY_BRANCH, gh.posted_comments[-1][1])


class BaseSyncParkReasonBoundaryTest(unittest.TestCase, OwnerBoundaryMixin):
    """The auto-rebase park reasons are read off the base-sync owner."""

    def test_auto_rebase_reason_lands_on_owner(self) -> None:
        # The rebase loop owns its own retry comment, so a continue command on
        # one of its parks has to fall through untouched.
        gh, issue, state = _seeded(
            FakeComment(id=REPLY_ID, body="/orchestrator continue", user=FakeUser(AUTHOR)),
            awaiting_human=True,
            park_reason=CUSTOM_REBASE_PARK,
        )
        # The facade answer is held empty: reading it there would let the
        # command through and retry a park this stage must not answer.
        with (
            self.facade_park_reasons_empty(),
            patch.object(
                _base_sync_state,
                AUTO_REBASE_PARK_REASONS,
                frozenset((CUSTOM_REBASE_PARK,)),
            ),
        ):
            handled = _continue_command._handle_parked_continue_command(
                gh, _TEST_SPEC, issue, state,
            )
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
