# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What a tick that died mid-split left behind, and what the next one may do.

Respawning the decomposer is the one thing recovery must never do. A crashed
split has already opened real GitHub issues, and a second manifest would not
reproduce the first, so the children it creates land beside the orphans rather
than instead of them. Every path here therefore ends in finalize or park, and
the two persistent markers `split` writes are what tells them apart:
`expected_children_count` goes down before the first child is created, and
`children` grows after each one is.

Those same two markers are written by the late split transaction, which owns
them for as long as its generation is live -- so a live generation stops this
recovery outright rather than finalizing a split that has not finished
snapshotting, superseding, or recording what the remote is owed. The tick ends
having changed nothing, which is what leaves the transaction free to resume
from its own durable facts.

Equal counts mean the loop finished and only the label flip was lost, so the
parent finalizes to whatever the manifest asked for. Fewer mean a child exists
that the parent never recorded, which no automatic rule can resolve, so it
parks. Finalizing also repairs each recorded child first: a crash at the last
child satisfies the count but leaves that child without its `parent_number`,
and probably parked by an earlier tick that read it as an unattributed
`blocked` issue -- so the parent's walk would flip it to `ready` and the
implementer would sit waiting on a human reply that is never coming.
"""
from __future__ import annotations

import logging

from github.Issue import Issue

from orchestrator import config
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.late_split import state as _late_state
from orchestrator.workflow.stages.decomposition import (
    late_relabel as _late_relabel,
)
from orchestrator.workflow.stages.decomposition import state as _state
from orchestrator.workflow.state import WorkflowLabel

log = logging.getLogger("orchestrator.workflow")


def _park_incomplete_decomposition(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    expected,
    children: list,
) -> None:
    _guards._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} decomposition crashed mid-way: "
        f"{len(children)} of {expected} children recorded (an orphan child "
        "issue may exist on GitHub if the crash landed between "
        "`create_child_issue` returning and the parent state write); manual "
        "intervention needed (close any partial children and re-decompose, "
        "or finish creating the missing ones).",
        reason="decomposition_crash",
    )
    gh.write_pinned_state(issue, state)


def _seed_orphan_child_state(
    gh: GitHubClient, issue: Issue, child_number,
) -> None:
    """Backfill `parent_number` (and creation stamp / unpark) on an orphan
    child so the parent's dependency walk can find it again."""
    child_issue = gh.get_issue(int(child_number))
    child_state = gh.read_pinned_state(child_issue)
    if not child_state.get(_state._PARENT_NUMBER):
        child_state.set(_state._PARENT_NUMBER, issue.number)
        if not child_state.get(_state._CREATED_AT):
            child_state.set(_state._CREATED_AT, _usage._now_iso())
        child_state.set(_state._AWAITING_HUMAN, False)
        child_state.set(_state._PARK_REASON, None)
        gh.write_pinned_state(child_issue, child_state)


def _repair_recovered_child(
    gh: GitHubClient, issue: Issue, state: PinnedState, child_number,
) -> bool:
    try:
        _seed_orphan_child_state(gh, issue, child_number)
    except Exception:
        log.exception(
            "issue=#%s could not repair orphan child #%s during "
            "decomposition recovery", issue.number, child_number,
        )
        _guards._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} could not repair child #{child_number} "
            "during decomposition recovery (seed `parent_number` on its "
            "pinned state); manual intervention needed (check orchestrator "
            "logs).",
            reason="child_seed_failed",
        )
        gh.write_pinned_state(issue, state)
        return False
    return True


def _repair_recovered_children(
    gh: GitHubClient, issue: Issue, state: PinnedState, children: list,
) -> bool:
    return all(
        _repair_recovered_child(gh, issue, state, child_number)
        for child_number in children
    )


def _markers_not_ours(issue: Issue, state: PinnedState) -> bool:
    """Whether these split markers belong to another owner's decision.

    Two owners can hold them. A human holds them once the issue is parked
    awaiting one: it is stopped either way, and there is nothing for a
    recovery to add. The late split transaction holds them for as long as its
    generation is live, because it writes the same two markers and resumes
    from its own durable facts -- finalizing on its behalf would hand a parent
    on before its snapshot, its supersession, or what the remote is owed had
    been settled.
    """
    if _late_relabel._adjudication_is_live(
        _late_state.read_late_generation(state),
    ):
        log.info(
            "issue=#%s carries a live oversized candidate; leaving its split "
            "markers to the late transaction that wrote them",
            issue.number,
        )
        return True
    return bool(state.get(_state._AWAITING_HUMAN))


def _recover_stale_manifest(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> bool:
    """Half-finished decomposition recovery / stale manifest cleanup.

    Returns True when a recovery path took over and the caller must
    return; False when no manifest markers are present and the caller
    should proceed to spawn the decomposer.

    Two persistent markers signal a prior tick crashed mid-split:
      * `expected_children_count` is written BEFORE any child is created,
        so a SIGKILL after `create_child_issue` returns but before the
        parent records the new child number leaves the parent with this
        marker AND zero recorded children while an orphan child issue
        exists on GitHub. Re-running the decomposer here would emit a
        different manifest and create duplicate children alongside the
        orphan.
      * `children` is written incrementally after each successful create +
        parent-state flush. Its presence covers a crash after at least one
        child was recorded.
    Either marker present without the parent label having flipped to
    `blocked` means we cannot safely respawn the decomposer. Branch by
    whether the recorded count matches expectations: equal -> finalize to
    `blocked`; less -> park awaiting human. Legacy state from a deploy that
    pre-dates `expected_children_count` still routes through the
    `children`-only branch and finalizes.
    """
    expected_raw = state.get("expected_children_count")
    children_recorded = state.get(_state._CHILDREN) or []
    if expected_raw is None and not children_recorded:
        return False
    if _markers_not_ours(issue, state):
        return True
    if expected_raw is not None and len(children_recorded) < int(expected_raw):
        _park_incomplete_decomposition(
            gh, issue, state, expected_raw, children_recorded,
        )
        return True
    # Before finalizing to `blocked`, repair any child whose pinned
    # state was never seeded. A SIGKILL between the parent's
    # incremental `children` write and the child-state write at
    # the LAST child satisfies `len(children) == expected_children_count`
    # but leaves that child orphaned: no `parent_number`, and likely
    # already parked with `awaiting_human=True` by a prior
    # `_handle_blocked` tick that saw it as "unattributed blocked".
    # Without repair, the parent's later walk flips the orphan to
    # `ready`, but `_handle_implementing` reads the stale park and
    # sits waiting for a human reply that never comes.
    if not _repair_recovered_children(gh, issue, state, children_recorded):
        return True
    # `umbrella=True` is persisted alongside `expected_children_count`
    # before any child is created, so the recovery path here picks
    # it up and finalizes to `umbrella` instead of `blocked`. Without
    # this branch, a SIGKILL between the umbrella manifest's child
    # creation loop and the final label flip would resume as a
    # plain blocked parent and re-enter implementation after all
    # children resolved -- the opposite of what the manifest asked.
    finalize_label = (
        WorkflowLabel.UMBRELLA if state.get(_state._UMBRELLA)
        else WorkflowLabel.BLOCKED
    )
    gh.set_workflow_label(issue, finalize_label)
    gh.write_pinned_state(issue, state)
    return True
