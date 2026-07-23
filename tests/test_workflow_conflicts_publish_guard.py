# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import workflow
from orchestrator.stages import conflicts

from tests.fakes import (
    FakeGitHubClient,
    FakePR,
    FakePRRef,
    make_issue,
)
from tests.workflow_helpers import _TEST_SPEC


def _pr(sha):
    return FakePR(number=1, head_branch="b", head=FakePRRef(sha=sha))


def _state_for(issue_number: int, **state_fields):
    github = FakeGitHubClient()
    issue = make_issue(issue_number, label="resolving_conflict")
    github.add_issue(issue)
    github.seed_state(issue_number, **state_fields)
    return github.read_pinned_state(issue)


class ResolvingConflictPublishGuardUnitTest(unittest.TestCase):
    """Unit tests for the two safety probes behind the already-rebased
    force-publish decision."""

    def test_pr_head_recognizes_docs_checked_sha(self) -> None:
        # `docs_checked_sha` is the only key production code persists for
        # an orchestrator-produced PR head (set by `_handle_documenting`'s
        # success exits). PR heads from earlier in the lifecycle (the
        # initial implementing push, an intermediate fixing push) are not
        # currently recorded, so the guard refuses those by design rather
        # than guessing.
        state = _state_for(1, docs_checked_sha="abc")
        self.assertTrue(
            conflicts._pr_head_orchestrator_produced(state, _pr("abc")),
        )
        self.assertFalse(
            conflicts._pr_head_orchestrator_produced(state, _pr("xyz")),
        )
        # An empty/missing head never matches.
        self.assertFalse(
            conflicts._pr_head_orchestrator_produced(state, _pr("")),
        )
        # No `docs_checked_sha` recorded -- e.g. a pre-docs validating
        # PR head -- must NOT match an empty-string lookup either.
        state2 = _state_for(2, dev_agent="claude")
        self.assertFalse(
            conflicts._pr_head_orchestrator_produced(state2, _pr("abc")),
        )

    def test_already_rebased_reads_rev_list_count(self) -> None:
        fetch_ok = MagicMock(return_value=MagicMock(returncode=0))
        with (
            patch.object(workflow, "_authed_fetch", fetch_ok),
            patch.object(
                workflow,
                "_git_hardened",
                MagicMock(return_value=MagicMock(returncode=0, stdout="0\n")),
            ),
        ):
            self.assertTrue(
                conflicts._already_rebased_onto_base(_TEST_SPEC, Path("/tmp/x")),
            )
        with (
            patch.object(workflow, "_authed_fetch", fetch_ok),
            patch.object(
                workflow,
                "_git_hardened",
                MagicMock(return_value=MagicMock(returncode=0, stdout="3\n")),
            ),
        ):
            self.assertFalse(
                conflicts._already_rebased_onto_base(_TEST_SPEC, Path("/tmp/x")),
            )

    def test_rebased_probe_fails_closed_on_fetch(self) -> None:
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
