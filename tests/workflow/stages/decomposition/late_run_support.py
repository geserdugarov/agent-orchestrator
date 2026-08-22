# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The harness one late adjudication is driven inside.

Narrower than the stage-handler patch set on purpose: the coordinator is not a
dispatched handler and touches the worktree the frozen candidate lives in, the
tracked spawn, and -- on a cleared `split` alone -- the remote the snapshot is
written to. The worktree it is pointed at is a real directory, because the
coordinator refuses to adjudicate a candidate this host cannot show the agent.

The split transaction is HELD by default, and that is the same discipline the
measurement seam is held under: what a cleared split earns is a subject with
its own modules, and a test about the adjudication that ran the whole
transaction as a side effect would be asserting on state it never asked for.
`transact=True` is how a test says it wants the real one.

Which seams answer what is the `late_seam_support` owner's, and the two
recorders that watch pinned state from inside a side effect are
`late_recorder_support`'s; this module owns the order one run is driven in.
"""
from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.agents import runner as _agent_runner
from orchestrator.workflow.stages.decomposition import (
    late_coordinator as _coordinator,
    late_transaction as _late_transaction,
)

from tests.workflow.fixtures import _TEST_SPEC, _agent
from tests.workflow.stages.decomposition.late_recorder_support import (
    HoldSnapshot as HoldSnapshot,
    SpawnSnapshot as SpawnSnapshot,
)
from tests.workflow.stages.decomposition.late_seam_support import (
    SnapshotSeed as SnapshotSeed,
    WorktreeSeed as WorktreeSeed,
    hold_late_seams,
)
from tests.workflow.stages.decomposition.late_test_support import (
    LATE_ISSUE_NUMBER,
    seeded_late_issue,
)

WORKTREE_NAME = f"issue-{LATE_ISSUE_NUMBER}"


def agent_reply(message: str, **result_fields):
    """One finished agent run carrying a late reply."""
    return _agent(last_message=message, **result_fields)


@contextlib.contextmanager
def late_run_context(
    spawn,
    *,
    worktree: WorktreeSeed = None,
    measurement=None,
    snapshot: SnapshotSeed = None,
    transact: bool = False,
):
    """Point the coordinator at a real worktree and a mocked spawn."""
    seed = worktree or WorktreeSeed()
    with contextlib.ExitStack() as stack:
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        checkout = scratch / WORKTREE_NAME
        if seed.exists:
            checkout.mkdir()
        hold_late_seams(stack, seed, checkout, measurement, snapshot)
        if not transact:
            stack.enter_context(patch.object(
                _late_transaction,
                "_run_late_split",
                MagicMock(side_effect=lambda _context, given: given),
            ))
        stack.enter_context(patch.object(_agent_runner, "run_agent", spawn))
        yield


def adjudicate(github, issue, agent_result=None, **run_fields):
    """Run one late adjudication and report the spawn it went through."""
    if callable(agent_result):
        spawn = MagicMock(side_effect=agent_result)
    else:
        spawn = MagicMock(return_value=agent_result)
    with late_run_context(spawn, **run_fields):
        outcome = _coordinator._adjudicate_late_generation(
            github, _TEST_SPEC, issue, github.read_pinned_state(issue),
        )
    return outcome, spawn


class LateCase:
    """One late issue on a fake client, and the coordinator run over it."""

    def setUp(self) -> None:
        github, issue = seeded_late_issue()
        self.github = github
        self.issue = issue

    def _adjudicate(self, agent_result=None, **run_fields):
        return adjudicate(self.github, self.issue, agent_result, **run_fields)

    def _pinned(self) -> dict:
        return self.github.pinned_data(LATE_ISSUE_NUMBER)

    def _events_named(self, family: str) -> list[dict]:
        return [
            record for record in self.github.recorded_events
            if record.get("event") == family
        ]
