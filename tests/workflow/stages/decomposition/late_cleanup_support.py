# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""One umbrella made by a split, and what it still owes a remote.

Shared by the two modules that ask about it, because they ask about the same
issue from two ends: what a branch obligation costs the terminal, and when the
snapshot it is holding may finally go. The umbrella tick is driven for real in
both, since what is under test is exactly the routing -- an issue that has
become an umbrella never reaches the transaction again.
"""
from __future__ import annotations

from typing import Optional

from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.late_split.models import (
    LateResource,
    LateResourceKind,
    LateResourceState,
)
from orchestrator.workflow.stages.decomposition import umbrella as _umbrella
from orchestrator.workflow.stages.decomposition.models import _ChildScan

from tests.support.fakes import FakeGitHubClient, make_issue
from tests.workflow.fixtures import _TEST_SPEC, _agent
from tests.workflow.stages.decomposition.late_test_support import (
    late_generation,
)

UMBRELLA = "workflow:umbrella"

LABEL_DONE = "done"

LABEL_REJECTED = "rejected"

LABEL_IN_REVIEW = "workflow:in_review"

PARENT_NUMBER = 41

CHILD_NUMBER = 411

SUPERSEDED_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-41"

SNAPSHOT_REF = "refs/orchestrator/late-split/issue-41/cycle-3/gen-1"

STATE_RECONCILED = "reconciled"

STATE_FAILED = "failed"

STATE_RETAINED = "retained"

EVENT_LATE_CLEANUP = "late_cleanup"

RESOLVED_STAMP = "umbrella_resolved_at"

WORKFLOW_LOG = "orchestrator.workflow"


def split_umbrella(
    owed: LateResourceState,
    *,
    snapshot: Optional[LateResourceState] = None,
    child_label: str = LABEL_DONE,
) -> tuple:
    """An umbrella whose children are done and whose remote is still owed."""
    github = FakeGitHubClient()
    parent = make_issue(PARENT_NUMBER, label=UMBRELLA)
    github.add_issue(parent)
    github.add_issue(make_issue(CHILD_NUMBER, label=child_label))
    settled = late_generation(
        threshold=None, additions=None, resources=(),
    ).with_consumers((CHILD_NUMBER,)).with_resource(LateResource(
        kind=LateResourceKind.BRANCH,
        target=SUPERSEDED_BRANCH,
        resource_state=owed,
    ))
    if snapshot is not None:
        settled = settled.with_resource(LateResource(
            kind=LateResourceKind.SNAPSHOT_REF,
            target=SNAPSHOT_REF,
            resource_state=snapshot,
        ))
    recorded = github.read_pinned_state(parent)
    _late_state.write_late_generation(recorded, settled)
    github.seed_state(
        PARENT_NUMBER,
        children=[CHILD_NUMBER],
        umbrella=True,
        **recorded.data,
    )
    return github, parent


def walk_umbrella(case, github, parent) -> None:
    """Run one umbrella tick through the real stage handler."""
    case._run(
        lambda: _umbrella._handle_umbrella(github, _TEST_SPEC, parent),
        run_agent=_agent(),
    )


def resource_states(github: FakeGitHubClient) -> dict:
    """The obligations the parent records, by target."""
    return {
        entry["target"]: entry["state"]
        for entry in github.pinned_data(PARENT_NUMBER).get("late_resources")
        or []
    }


def scan_of(label, *, closed: bool = False) -> _ChildScan:
    """The umbrella's own child scan, reporting one child this way."""
    child = make_issue(CHILD_NUMBER, label=label)
    child.closed = closed
    return _ChildScan(
        children=[CHILD_NUMBER],
        issues={CHILD_NUMBER: child},
        labels={CHILD_NUMBER: label},
    )


class RecordedDelete:
    """A snapshot delete answering one outcome, recording what it was asked.

    `dies` is the crash between the call landing and the write that would have
    recorded it: the delete has happened as far as the remote is concerned,
    and nothing on the issue says so.
    """

    def __init__(self, outcome, *, dies: bool = False) -> None:
        self.outcome = outcome
        self.dies = dies
        self.refs: list[str] = []
        self.shas: list[str] = []

    def __call__(self, _spec, _cwd, *, ref: str, sha: str):
        self.refs.append(ref)
        self.shas.append(sha)
        if self.dies:
            raise KeyboardInterrupt("delete_snapshot_ref")
        return self.outcome
