# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition recovery state."""
from __future__ import annotations

from orchestrator.stages import _decomposition_state as _state
from orchestrator.stages import decomposition as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_CHILDREN = _state._CHILDREN
_CREATED_AT = _state._CREATED_AT
_PARENT_NUMBER = _state._PARENT_NUMBER
_PARK_REASON = _state._PARK_REASON
_UMBRELLA = _state._UMBRELLA


def _issue_ref_list(numbers: list) -> str:
    """Render issue/child numbers as a `#a, #b` comma-joined reference list."""
    return ", ".join(f"#{number}" for number in numbers)


def _decomposition_drift_notice(orphans: list) -> str:
    notice = (
        ":pencil2: issue content changed; re-running decomposer against "
        "the updated body."
    )
    if not orphans:
        return notice
    orphan_list = _owner._issue_ref_list(orphans)
    return (
        f"{notice} The previously-tracked children ({orphan_list}) will be "
        "ORPHANED -- the orchestrator no longer tracks them; please close "
        "any that no longer apply to the updated requirements."
    )


def _clear_decomposition_manifest(state: PinnedState) -> None:
    state.set("decomposer_session_id", None)
    state.set(_CHILDREN, [])
    state.set("dep_graph", {})
    state.set("expected_children_count", None)
    state.set(_UMBRELLA, None)
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)


def _park_incomplete_decomposition(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    expected,
    children: list,
) -> None:
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
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
    from orchestrator import workflow as _wf

    child_issue = gh.get_issue(int(child_number))
    child_state = gh.read_pinned_state(child_issue)
    if not child_state.get(_PARENT_NUMBER):
        child_state.set(_PARENT_NUMBER, issue.number)
        if not child_state.get(_CREATED_AT):
            child_state.set(_CREATED_AT, _wf._now_iso())
        child_state.set(_AWAITING_HUMAN, False)
        child_state.set(_PARK_REASON, None)
        gh.write_pinned_state(child_issue, child_state)


def _repair_recovered_child(
    gh: GitHubClient, issue: Issue, state: PinnedState, child_number,
) -> bool:
    from orchestrator import workflow as _wf

    try:
        _owner._seed_orphan_child_state(gh, issue, child_number)
    except Exception:
        _wf.log.exception(
            "issue=#%s could not repair orphan child #%s during "
            "decomposition recovery", issue.number, child_number,
        )
        _wf._park_awaiting_human(
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
        _owner._repair_recovered_child(gh, issue, state, child_number)
        for child_number in children
    )
