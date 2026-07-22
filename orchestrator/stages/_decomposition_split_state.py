# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition split state."""
from __future__ import annotations

from orchestrator.stages import _decomposition_state as _state
from orchestrator.stages import decomposition as _owner

_SplitPlan = _owner._SplitPlan
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
_CHILDREN = _state._CHILDREN
_CREATED_AT = _state._CREATED_AT
_PARENT_NUMBER = _state._PARENT_NUMBER
_UMBRELLA = _state._UMBRELLA


def _prepare_split_plan(
    gh: GitHubClient, issue: Issue, state: PinnedState, plan: _SplitPlan,
) -> None:
    state.set("expected_children_count", len(plan.children_manifest))
    state.set(_UMBRELLA, plan.is_umbrella)
    gh.write_pinned_state(issue, state)


def _park_child_create_failure(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    idx: int,
    child: dict,
) -> None:
    from orchestrator import workflow as _wf

    _wf.log.exception(
        "issue=#%s could not create child %d (%r)",
        issue.number, idx, child.get("title"),
    )
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} could not create child issue index={idx} "
        f"({child.get('title')!r}); manual intervention needed (check "
        "orchestrator logs).",
        reason="child_create_failed",
    )
    gh.write_pinned_state(issue, state)


def _persist_created_child(
    gh: GitHubClient, issue: Issue, state: PinnedState, plan: _SplitPlan,
) -> None:
    from orchestrator import workflow as _wf

    state.set(_CHILDREN, [number for number, _ in plan.created])
    if plan.dep_graph:
        state.set("dep_graph", plan.dep_graph)
    state.set("decomposed_at", _wf._now_iso())
    gh.write_pinned_state(issue, state)


def _write_child_pinned_state(
    gh: GitHubClient, new_issue: Issue, parent_number: int,
) -> None:
    """Write a freshly-created child's initial pinned state (parent link and
    creation stamp)."""
    from orchestrator import workflow as _wf

    child_state = PinnedState()
    child_state.set(_PARENT_NUMBER, parent_number)
    child_state.set(_CREATED_AT, _wf._now_iso())
    gh.write_pinned_state(new_issue, child_state)


def _seed_created_child(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    new_issue: Issue,
    child: dict,
) -> bool:
    from orchestrator import workflow as _wf

    try:
        _owner._write_child_pinned_state(gh, new_issue, issue.number)
    except Exception:
        _wf.log.exception(
            "issue=#%s could not seed pinned state on child #%d",
            issue.number, new_issue.number,
        )
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} created child #{new_issue.number} "
            f"({child.get('title')!r}) but could not seed its pinned state "
            "with `parent_number`; manual intervention needed (seed "
            "parent_number on the child or close it).",
            reason="child_seed_failed",
        )
        gh.write_pinned_state(issue, state)
        return False
    return True


def _child_initial_labels() -> list[str]:
    """Labels every split child is born with: only the initial `blocked`
    workflow label. Activation later flips no-dep children to `ready`.
    """
    return [WorkflowLabel.BLOCKED]


def _create_planned_child(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    plan: _SplitPlan,
    idx: int,
) -> bool:
    child = plan.children_manifest[idx]
    try:
        new_issue = gh.create_child_issue(
            title=child["title"],
            body=child["body"],
            parent_number=issue.number,
            labels=_owner._child_initial_labels(),
        )
    except Exception:
        _owner._park_child_create_failure(gh, issue, state, idx, child)
        return False
    plan.record(idx, new_issue.number, child)
    _owner._persist_created_child(gh, issue, state, plan)
    return _owner._seed_created_child(gh, issue, state, new_issue, child)
