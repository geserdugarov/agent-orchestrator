# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The one door the decomposition switch closes, and who may not be shut out.

`DECOMPOSE=off` is a whole-install answer: new work goes straight past the size
gate, unread and unmeasured, which is what the switch is for. What these pin
down is the one caller that answer is wrong for -- a publication a rewrite
PERMIT alone may license, which is not new work and is not asking for a count.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import config
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.late_split.models import LateGeneration
from orchestrator.workflow.stages.implementing import (
    late_freeze as _freeze,
    late_records as _records,
)
from tests.support.fakes import FakeGitHubClient, make_issue
from tests.workflow.fixtures import _TEST_SPEC

_DECOMPOSE = "DECOMPOSE"

# One issue, one checkout the gate never reads here: what the switch answers
# is decided off the record and the call's own terms.
_ISSUE_NUMBER = 300
_WORKTREE = Path("/tmp")


class PermitOnlyEntryTest(unittest.TestCase):
    """What the decomposition switch may and may not keep out of the gate.

    Off, the switch takes new work straight past the gate unread, which is the
    whole of what it is for. A crash recovery holding a transfer is not new
    work: what it needs from the gate is not a count but the PERMIT, the one
    thing that can say a replay carries a verdict a human already gave. Kept
    out, it publishes with nothing vouching for the move, finishes its route
    with the exemption still on the commit a human ruled on, and leaves the
    permission standing outstanding for ever.
    """

    def test_a_permit_only_call_stays_inside(self) -> None:
        with patch.object(config, _DECOMPOSE, False):
            outside = _freeze._outside_the_gate(
                self._entered(permit_only=True), LateGeneration(),
            )

        self.assertFalse(outside)

    def test_new_work_is_still_kept_out(self) -> None:
        # The other half of the same claim: the switch keeps doing what it is
        # there for, so this is an exception for one caller rather than a way
        # around it.
        with patch.object(config, _DECOMPOSE, False):
            outside = _freeze._outside_the_gate(
                self._entered(), LateGeneration(),
            )

        self.assertTrue(outside)

    def _entered(self, *, permit_only: bool = False) -> _records._Gate:
        """One gate call over an issue with nothing recorded on it yet."""
        github = FakeGitHubClient()
        issue = make_issue(_ISSUE_NUMBER)
        github.add_issue(issue)
        return _records._Gate(
            gh=github,
            spec=_TEST_SPEC,
            issue=issue,
            state=PinnedState(data={}),
            worktree=_WORKTREE,
            permit_only=permit_only,
        )
