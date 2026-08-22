# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What the world answers a late run's probes with, and how it is held.

The seams a late adjudication would otherwise really reach are a checkout on
disk, a size measurement that shells out to git, and -- on a cleared `split`
alone -- a push and a fetch against a real remote. Every one of them is held,
and a case says which answers it is about by handing one of the two seeds
here; what is not asked about is held at the answer that lets the run proceed,
except the measurement, which is held at a failure because a test that reaches
it without saying what it expects has not decided anything.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator.git.snapshots.refs import SnapshotOutcome
from orchestrator.git.verification.probes import _WorktreeStatus

from tests.workflow.git_owners import seam_patch
from tests.workflow.stages.decomposition.late_test_support import (
    CANDIDATE_SHA,
    UNASKED_MEASUREMENT,
)


@dataclass(frozen=True)
class WorktreeSeed:
    """What the candidate's worktree answers a late run's probes with.

    The defaults are the only shape a verdict may be read on: the checkout is
    there, HEAD is still the frozen candidate, and the tree is provably clean.
    A test about a read-only agent that wrote says otherwise.
    """

    exists: bool = True
    head: str = CANDIDATE_SHA
    readable: bool = True
    dirty: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotSeed:
    """What the remote does with the snapshot a split is cut from.

    The defaults are the only shape a split may run under: the ref was written
    and then fetched back and resolved to the frozen candidate. A test about a
    namespace the token cannot write, a ref another commit already occupies,
    or a remote that would not serve it back says otherwise.
    """

    create: SnapshotOutcome = SnapshotOutcome.CREATED
    prove: SnapshotOutcome = SnapshotOutcome.PROVEN


def hold_late_seams(
    stack,
    seed: WorktreeSeed,
    checkout: Path,
    measurement,
    snapshot: SnapshotSeed,
) -> None:
    """Hold every git seam one late run would otherwise really reach.

    The local teardown is held whatever a case is about, because a real `git
    worktree remove` in a unit test is a command against whatever directory
    the configured root happens to name.
    """
    held = {
        "_measure_candidate": measurement or UNASKED_MEASUREMENT,
        "_worktree_path": checkout,
        "_head_sha": seed.head,
        "_worktree_status": _WorktreeStatus(
            readable=seed.readable, paths=tuple(seed.dirty),
        ),
        "create_snapshot_ref": (snapshot or SnapshotSeed()).create,
        "prove_snapshot_ref": (snapshot or SnapshotSeed()).prove,
    }
    for name, answer in held.items():
        stack.enter_context(seam_patch(name, MagicMock(return_value=answer)))
    stack.enter_context(seam_patch("_remove_issue_worktree"))
    stack.enter_context(seam_patch("_delete_local_issue_branch"))


@contextlib.contextmanager
def snapshot_seams(snapshot: SnapshotSeed):
    """Hold the remote and the local teardown for one split transaction.

    The subset a transaction driven on its own needs: it never spawns an agent
    and never reads the worktree, so what has to be held is the push, the
    fetch, and the teardown that follows them.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(seam_patch(
            "create_snapshot_ref", MagicMock(return_value=snapshot.create),
        ))
        stack.enter_context(seam_patch(
            "prove_snapshot_ref", MagicMock(return_value=snapshot.prove),
        ))
        stack.enter_context(seam_patch("_remove_issue_worktree"))
        stack.enter_context(seam_patch("_delete_local_issue_branch"))
        yield
