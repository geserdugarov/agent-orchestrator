# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""One guarded split, driven straight into the transaction that runs it.

The transaction is entered from exactly one shape -- a `split` a fresh owner
read cleared -- so these tests build that shape and call the owner, rather than
paying for an agent run and an owner read to reach the same place. What the
coordinator does to GET here has its own modules; what happens once it has is
this one's subject.

The remote is the seam every case pins. Creating and proving a snapshot is a
push and a fetch against a real host, and the whole transaction turns on what
those two answer, so a case says which answers it is about with a seed and the
`late_seam_support` owner holds the rest.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace

from orchestrator.git.snapshots.refs import SnapshotOutcome
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.stages.decomposition import late_hold as _late_hold
from orchestrator.workflow.stages.decomposition import (
    late_transaction as _late_transaction,
)
from orchestrator.workflow.stages.decomposition.late_models import (
    _GuardedSplit,
    _LateAdjudicationRun,
    _LateContext,
    _LateDisposition,
    _LateRun,
)

from tests.workflow.fixtures import _TEST_SPEC
from tests.workflow.stages.decomposition.late_seam_support import (
    SnapshotSeed,
    snapshot_seams,
)
from tests.workflow.stages.decomposition.late_test_support import (
    CYCLE_ID,
    GENERATION_NUMBER,
    LATE_ISSUE_NUMBER,
    PLAN_PR_BODY,
    PLAN_PR_NUMBER,
    late_generation,
    seed_late_issue,
    seed_plan_pr,
)

from tests.support.fakes import FakeGitHubClient

# The manifest every case splits into, and the dependency between its two
# children -- so activation has one child to release and one to hold.
CHILDREN = (
    {"title": "A", "body": "the first slice", "depends_on": []},
    {"title": "B", "body": "the second slice", "depends_on": [0]},
)

SNAPSHOT_REF = (
    f"refs/orchestrator/late-split/issue-{LATE_ISSUE_NUMBER}"
    f"/cycle-{CYCLE_ID}/gen-{GENERATION_NUMBER}"
)

KEY_CHILDREN = "children"
KEY_CONSUMERS = "late_consumers"
KEY_DEP_GRAPH = "dep_graph"
KEY_DECOMPOSED_AT = "decomposed_at"
KEY_EXPECTED_CHILDREN = "expected_children_count"
KEY_PARENT_NUMBER = "parent_number"
KEY_PR_NUMBER = "pr_number"
KEY_UMBRELLA = "umbrella"
KEY_RESOURCES = "late_resources"

KEY_ANCESTRY_REF = "late_ancestry_snapshot_ref"
KEY_ANCESTRY_SHA = "late_ancestry_snapshot_sha"
KEY_ANCESTRY_DEPTH = "late_ancestry_depth"
KEY_ANCESTRY_ROOT = "late_ancestry_root_issue"
KEY_ANCESTRY_PARENT = "late_ancestry_parent"
KEY_ANCESTRY_CYCLE = "late_ancestry_cycle_id"
KEY_ANCESTRY_GENERATION = "late_ancestry_generation"
KEY_ANCESTRY_BASE = "late_ancestry_base_branch"
KEY_DECLARED_SCOPE = "late_declared_scope"

EVENT_LATE_SNAPSHOT = "late_snapshot"
EVENT_LATE_CLEANUP = "late_cleanup"
EVENT_LATE_FAILURE = "late_failure"

PARK_SNAPSHOT_FAILED = "late_snapshot_failed"
PARK_CHILDREN_FAILED = "late_children_failed"
PARK_SUPERSESSION_FAILED = "late_supersession_failed"

WORKFLOW_LOG = "orchestrator.workflow"

ERROR = "ERROR"


class LateSplitCase:
    """One oversized issue whose adjudication decided to split it."""

    def setUp(self) -> None:
        self.github = FakeGitHubClient()
        self.generation = late_generation()
        self.issue = seed_late_issue(self.github, self.generation)

    def _transact(
        self,
        *,
        children=CHILDREN,
        snapshot=None,
        generation=None,
        killed=None,
        **state,
    ) -> _LateAdjudicationRun:
        """Run the transaction once over a guarded split, and report it."""
        if state:
            self.github.seed_state(
                self.issue.number, **{**self._pinned(), **state},
            )
        recorded = generation or self.generation
        context = _LateContext(
            gh=self.github,
            spec=_TEST_SPEC,
            issue=self.issue,
            state=self.github.read_pinned_state(self.issue),
            generation=recorded,
        )
        finished = _LateAdjudicationRun(
            disposition=_LateDisposition.DECIDED,
            generation=recorded,
            run=_LateRun(),
            guarded_split=_GuardedSplit(
                generation=recorded, children=tuple(children),
            ),
        )
        with snapshot_seams(snapshot or SnapshotSeed()):
            with killed or nullcontext():
                return _late_transaction._run_late_split(context, finished)

    def _resume(self, **run_fields) -> _LateAdjudicationRun:
        """Run the transaction again from the record it left behind.

        What the next eligible tick does: the recorded verdict is reused, the
        owner is read again, and the same guarded split reaches this owner
        with whatever the parked attempt made durable.
        """
        return self._transact(
            generation=_late_state.read_late_generation(
                self.github.read_pinned_state(self.issue),
            ),
            **run_fields,
        )

    def _pinned(self) -> dict:
        return self.github.pinned_data(self.issue.number)

    def _child_state(self, child_number: int) -> dict:
        return self.github.pinned_data(child_number)

    def _resources(self) -> dict:
        """The obligation ledger as {(kind, target): state}."""
        return {
            (entry["kind"], entry["target"]): entry["state"]
            for entry in self._pinned().get(KEY_RESOURCES) or []
        }

    def _events_named(self, family: str) -> list[dict]:
        return [
            record for record in self.github.recorded_events
            if record.get("event") == family
        ]


class HeldPlanPrSplitCase(LateSplitCase):
    """A split whose plan PR already wears this generation's hold.

    Seeded held rather than held by a first tick, because the subject is what
    the supersession does to it: reaching that state through an adjudication
    would cost a recorded verdict, and a recorded verdict is what stops the
    next tick spawning the run under test.
    """

    def setUp(self) -> None:
        super().setUp()
        self.generation = replace(
            self.generation,
            plan_pr_number=PLAN_PR_NUMBER,
            plan_pr_body=PLAN_PR_BODY,
        )
        self.github.seed_state(
            self.issue.number, **self._pinned(), pr_number=PLAN_PR_NUMBER,
        )
        self.plan_pr = seed_plan_pr(
            self.github, body=_late_hold._hold_body(self.generation),
        )


def label_of(github: FakeGitHubClient, issue_number: int) -> str:
    """The one workflow label an issue currently carries."""
    return next(
        label.name for label in github.get_issue(issue_number).labels
    )


def first_child(github: FakeGitHubClient):
    """The child a split created for the first slice of its manifest."""
    return github.created_child_issues[0]


def refused_snapshot(outcome: SnapshotOutcome) -> SnapshotSeed:
    """A remote that would not let the snapshot be created."""
    return SnapshotSeed(create=outcome)


def unproven_snapshot(outcome: SnapshotOutcome) -> SnapshotSeed:
    """A snapshot the remote reported and would not serve back."""
    return SnapshotSeed(prove=outcome)
