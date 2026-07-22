# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition activation."""
from __future__ import annotations

from orchestrator.stages import _decomposition_state as _state
from orchestrator.stages import decomposition as _owner

_ChildScan = _owner._ChildScan
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
dataclass = _owner.dataclass
_DONE = _state._DONE
_HeldChild = _state._HeldChild


@dataclass
class _ChildActivation:
    gh: GitHubClient
    state: PinnedState
    scan: _ChildScan
    held: list[_HeldChild]
    relabeled: bool = False

    @classmethod
    def start(
        cls, gh: GitHubClient, state: PinnedState, scan: _ChildScan,
    ) -> _ChildActivation:
        return cls(gh, state, scan, [])

    def consider(self, idx: int, child_number) -> None:
        number = int(child_number)
        if self.scan.labels.get(number) != "blocked":
            return
        pending = self._pending_dependencies(idx)
        if pending:
            self.held.append((number, pending))
        else:
            self.gh.set_workflow_label(
                self.scan.issues[number], WorkflowLabel.READY,
            )
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
            if self.scan.labels.get(number) != _DONE
        ]


def _activate_ready_children(
    gh: GitHubClient, issue: Issue, state: PinnedState, scan: _ChildScan,
) -> list:
    """Dep-graph activation walk shared by `_handle_blocked` / `_handle_umbrella`.

    Any `blocked` child whose recorded dependencies are all `done` gets
    relabeled `ready`. A child with no recorded deps also flips (vacuous
    all-done over an empty list) -- this recovers any no-dep child that the
    decomposer's same-tick activation step left as `blocked` (network blip,
    label-flip failure, etc.). Writes pinned state when at least one child
    was relabeled. Returns the still-held children as
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
    return f"#{child_number} waits on {_owner._issue_ref_list(pending)}"


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
    from orchestrator import workflow as _wf

    if not held:
        return
    done_count = sum(1 for lbl in child_labels.values() if lbl == _DONE)
    summary = "; ".join(
        _owner._held_dependency_line(cn, pending) for cn, pending in held
    )
    _wf.log.info(
        "issue=#%s %s parent: %d/%d children done, %d held: %s",
        issue.number, parent_kind, done_count, len(children), len(held),
        summary,
    )
