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
import shutil
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

# A checkout path nothing put on disk: what the probe reads once a teardown
# has taken one down.
_ABSENT_CHECKOUT = "/nonexistent/orchestrator-test-checkout"


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
    """What the world does to a split: the remote, and the local teardown.

    The defaults are the only shape a split may run under: the ref was written
    and then fetched back and resolved to the frozen candidate, and the
    checkout the superseded branch was on came down. A test about a namespace
    the token cannot write, a ref another commit already occupies, a remote
    that would not serve it back, or a worktree that would not go says
    otherwise.

    `local_gone` sits here rather than beside it because it answers the same
    question the other two do -- what the world outside this process did with
    what the transaction asked of it -- and because the branch obligation is
    settled by all three together.
    """

    create: SnapshotOutcome = SnapshotOutcome.CREATED
    prove: SnapshotOutcome = SnapshotOutcome.PROVEN
    local_gone: bool = True


class _RemovedCheckout:
    """A worktree teardown that actually takes the directory down.

    Held as a mock everywhere else, but this one has to be real: what decides
    whether a branch obligation is settled is a read of the checkout taken
    AFTER the teardown, so a teardown that changed nothing would leave every
    reclamation reading as refused.
    """

    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout

    def __call__(self, _spec, _issue_number, **_options) -> None:
        shutil.rmtree(self.checkout, ignore_errors=True)


@contextlib.contextmanager
def local_teardown(*, local_gone: bool = True):
    """Hold the local half of a branch reclamation and the read behind it.

    A real `git worktree remove` in a unit test is a command against whatever
    directory the configured root happens to name, and the read that decides
    whether it happened would shell out to a clone that is not there. What a
    case says is only the answer: `local_gone=False` is the checkout that
    would not come down.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(seam_patch(
            "_worktree_path", MagicMock(return_value=Path(_ABSENT_CHECKOUT)),
        ))
        _hold_teardown(stack, Path(_ABSENT_CHECKOUT), local_gone=local_gone)
        yield


def _hold_teardown(stack, checkout: Path, *, local_gone: bool = True) -> None:
    """Hold the local teardown and the read that decides it happened."""
    stack.enter_context(
        seam_patch("_remove_issue_worktree", _RemovedCheckout(checkout)),
    )
    stack.enter_context(seam_patch("_delete_local_issue_branch"))
    stack.enter_context(seam_patch(
        "_local_branch_present", MagicMock(return_value=not local_gone),
    ))


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
    _hold_teardown(
        stack, checkout, local_gone=(snapshot or SnapshotSeed()).local_gone,
    )


@contextlib.contextmanager
def snapshot_seams(snapshot: SnapshotSeed):
    """Hold the remote and the local teardown for one split transaction.

    The subset a transaction driven on its own needs: it never spawns an agent
    and never reads the worktree, so what has to be held is the push, the
    fetch, the teardown that follows them, and the read that decides whether
    that teardown happened. The seed's `local_gone` is the checkout that would
    not come down, which leaves the branch obligation owed.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(seam_patch(
            "create_snapshot_ref", MagicMock(return_value=snapshot.create),
        ))
        stack.enter_context(seam_patch(
            "prove_snapshot_ref", MagicMock(return_value=snapshot.prove),
        ))
        stack.enter_context(seam_patch(
            "_worktree_path", MagicMock(return_value=Path(_ABSENT_CHECKOUT)),
        ))
        _hold_teardown(
            stack, Path(_ABSENT_CHECKOUT), local_gone=snapshot.local_gone,
        )
        yield
