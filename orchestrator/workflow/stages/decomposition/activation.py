# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The dep-graph walk that decides which children may start next.

A `blocked` child becomes `ready` the moment every dependency the manifest
recorded for it is `done`. A child with no recorded dependencies satisfies that
vacuously, which is deliberate: it is also the retry for a no-dep child whose
same-tick activation flip failed at split time, so nothing has to remember that
the flip was missed.

A child GitHub reports as closed is passed over, whatever label it wears.
Closing an issue does not change its label, so a child a human ended while it
was still `blocked` sits there looking startable forever -- and a walk that
started it would relabel a closed issue `ready`, overriding the close and
handing the umbrella a child that will never report. It is skipped rather than
held, because nothing is going to release it.

Held children are logged rather than parked, because the tree is still making
progress: their siblings run concurrently and are what will eventually release
them. The line names the exact unfinished dependencies so an operator reading a
tick log can tell a waiting parent from a stuck one without opening GitHub, and
it is emitted only when something is actually held so a healthy parent stays
quiet.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from github.Issue import Issue

from orchestrator.github.client import GitHubClient
from orchestrator.github.issues import issue_is_closed
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.stages.decomposition import state as _state
from orchestrator.workflow.stages.decomposition.models import _ChildScan
from orchestrator.workflow.state import WorkflowLabel

log = logging.getLogger("orchestrator.workflow")


@dataclass
class _ChildActivation:
    gh: GitHubClient
    state: PinnedState
    scan: _ChildScan
    held: list[_state._HeldChild]
    relabeled: bool = False

    @classmethod
    def start(
        cls, gh: GitHubClient, state: PinnedState, scan: _ChildScan,
    ) -> _ChildActivation:
        return cls(gh, state, scan, [])

    def consider(self, idx: int, child_number) -> None:
        number = int(child_number)
        child = self.scan.issues.get(number)
        if self.scan.labels.get(number) != WorkflowLabel.BLOCKED:
            return
        if child is None or issue_is_closed(child):
            return
        pending = self._pending_dependencies(idx)
        if pending:
            self.held.append((number, pending))
        else:
            self.gh.set_workflow_label(child, WorkflowLabel.READY)
            self.relabeled = True

    def _pending_dependencies(self, idx: int) -> list[int]:
        dep_graph = self.state.get("dep_graph") or {}
        dependencies = dep_graph.get(str(idx), [])
        dep_numbers = [
            int(self.scan.children[int(dep_idx)])
            for dep_idx in dependencies
            if int(dep_idx) < len(self.scan.children)
        ]
        return [
            number for number in dep_numbers
            if self.scan.labels.get(number) != _state._DONE
        ]


def _activate_ready_children(
    gh: GitHubClient, issue: Issue, state: PinnedState, scan: _ChildScan,
) -> list:
    """Dep-graph activation walk shared by `_handle_blocked` / `_handle_umbrella`.

    Any `blocked` child whose recorded dependencies are all `done` gets
    relabeled `ready`. A child with no recorded deps also flips (vacuous
    all-done over an empty list) -- this recovers any no-dep child that the
    decomposer's same-tick activation step left as `blocked` (network blip,
    label-flip failure, etc.). A child that is closed, or that this scan holds
    no issue for, is passed over: the first has ended and the second cannot be
    written to. Writes pinned state when at least one child was relabeled. Returns the still-held children as
    `[(child_number, pending_dep_numbers)]` for visibility logging.
    """
    activation = _ChildActivation.start(gh, state, scan)
    for idx, child_number in enumerate(scan.children):
        activation.consider(idx, child_number)
    if activation.relabeled:
        gh.write_pinned_state(issue, state)
    return activation.held


def _held_dependency_line(child_number: object, pending: list) -> str:
    """Format one held child and the unfinished dependencies gating it."""
    return f"#{child_number} waits on {_state._issue_ref_list(pending)}"


def _log_held_children(
    issue: Issue, parent_kind: str, children: list, child_labels: dict,
    held: list,
) -> None:
    """Surface which children are still held under a parent and the exact
    unfinished dependencies gating each, so an operator can see at a glance
    why a decomposed parent is not advancing.

    Children whose deps are satisfied are intentionally NOT held -- they run
    concurrently while the parent waits, which is what drives the tree to
    completion. Logged only when something is held to keep a healthy parent
    from spamming the tick log. `parent_kind` is `"blocked"` or `_UMBRELLA`.
    """
    if not held:
        return
    done_count = sum(
        1 for lbl in child_labels.values() if lbl == _state._DONE
    )
    summary = "; ".join(
        _held_dependency_line(cn, pending) for cn, pending in held
    )
    log.info(
        "issue=#%s %s parent: %d/%d children done, %d held: %s",
        issue.number, parent_kind, done_count, len(children), len(held),
        summary,
    )
