# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition blocked."""
from __future__ import annotations

from orchestrator.stages import _decomposition_state as _state
from orchestrator.stages import decomposition as _owner

_ChildScan = _owner._ChildScan
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_CHILDREN = _state._CHILDREN
_CREATED_AT = _state._CREATED_AT
_DONE = _state._DONE
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID
_PARENT_NUMBER = _state._PARENT_NUMBER
_PARK_REASON = _state._PARK_REASON


def _handle_ready(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    """`ready` is the entry point for an auto-created child or for a parent
    whose decomposer voted `single`. Both cases need the same pickup-state
    seeding the legacy `_handle_pickup` did before flipping to
    `implementing`, so the validating handoff watermark and the in_review
    legacy migration have an anchor comment they can key on.
    """
    from orchestrator import workflow as _wf

    state = gh.read_pinned_state(issue)
    # User-content drift before implementation has started: route back to
    # decomposing so the manifest is re-derived against the new body. A
    # non-umbrella parent can reach `ready` after every child resolves
    # (`_handle_blocked`'s all-done branch flips `blocked` -> `ready`), so
    # the parent may STILL carry `children` / `dep_graph` /
    # `expected_children_count` from the prior manifest. `_route_parent_drift`
    # (via `_route_drift_to_decomposing`) wipes that tracking alongside the
    # locked decomposer session, so the next `_handle_decomposing` tick's
    # half-finished recovery branch does not fire and just flip the issue
    # back to `blocked` without re-running the decomposer.
    if _owner._route_parent_drift(gh, issue, state):
        return
    if state.get("pickup_comment_id") is None:
        if not state.get(_CREATED_AT):
            state.set(_CREATED_AT, _wf._now_iso())
        pickup = _wf._post_issue_comment(
            gh, issue, state,
            ":robot: orchestrator picking this up; starting implementation.",
        )
        pickup_id = getattr(pickup, "id", None)
        if pickup_id is not None:
            state.set("pickup_comment_id", int(pickup_id))
    # Mark every comment visible right now as "already consumed". For a
    # parent that came through `decomposing` / `blocked`, `pickup_comment_id`
    # was anchored on the original "decomposing" comment, so any human
    # feedback posted while children were resolving sits AFTER pickup and
    # would be classified as post-pickup, unconsumed feedback by the
    # in_review watermark seed. The implementer reads the full thread via
    # `_recent_comments_text` at spawn, so by the time the PR reaches
    # `in_review` those comments have been incorporated; replaying them
    # would resume the dev and bounce the PR back to validating instead
    # of allowing merge. Bumping `last_action_comment_id` lets
    # `_seed_watermark_past_self`'s `consumed_through` walk advance past
    # them. The next park (or the validating handoff) will overwrite this
    # value, so it's a transient marker for the in-progress handoff only.
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        prior = state.get(_LAST_ACTION_COMMENT_ID)
        if not isinstance(prior, int) or latest > prior:
            state.set(_LAST_ACTION_COMMENT_ID, latest)
    gh.set_workflow_label(issue, WorkflowLabel.IMPLEMENTING)
    gh.write_pinned_state(issue, state)
    _wf._handle_implementing(gh, spec, issue)


def _usable_child_scan(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue: Issue,
    state: PinnedState,
    children: list,
) -> Optional[_ChildScan]:
    scan = _owner._read_child_labels(gh, issue, children)
    if scan is None:
        return None
    if _owner._park_rejected_children(gh, issue, state, scan.labels):
        return None
    if _owner._park_manually_closed_children(gh, spec, issue, state, scan):
        return None
    return scan


def _handle_empty_blocked_parent(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    if state.get(_PARENT_NUMBER) or state.get(_AWAITING_HUMAN):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `blocked` without recorded children; "
        "manual relabel suspected.",
        reason="blocked_no_children",
    )
    gh.write_pinned_state(issue, state)


def _complete_blocked_parent(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    _wf._post_issue_comment(
        gh, issue, state,
        ":white_check_mark: all children resolved; ready for implementation.",
    )
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
    gh.set_workflow_label(issue, WorkflowLabel.READY)
    gh.write_pinned_state(issue, state)


def _handle_blocked(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    """Poll children to decide whether the parent unblocks (or one of the
    children unblocks).

    The orchestrator's parallel tick path (see
    `workflow._FAMILY_AWARE_LABELS`) submits the whole family-aware
    bucket as a single drain task on one worker thread, so only one of
    `decomposing`, `blocked`, or `umbrella` runs at a time within a
    tick -- even when other issues fan out across worker threads. A
    child's `in_review -> done` label flip and this tick therefore
    still cannot race the parent's child-state writes; we read each
    child's current label fresh here. Issues outside the family-aware
    bucket (`implementing`, `validating`, `in_review`,
    `resolving_conflict`) may run concurrently alongside, but their
    handlers do not write across parent/child boundaries.
    """
    state = gh.read_pinned_state(issue)
    children = state.get(_CHILDREN) or []

    if _owner._route_parent_drift(gh, issue, state):
        return

    if not children:
        _owner._handle_empty_blocked_parent(gh, issue, state)
        return

    scan = _owner._usable_child_scan(gh, spec, issue, state, children)
    if scan is None:
        return
    if all(label == _DONE for label in scan.labels.values()):
        _owner._complete_blocked_parent(gh, issue, state)
        return

    held = _owner._activate_ready_children(gh, issue, state, scan)
    _owner._log_held_children(issue, "blocked", children, scan.labels, held)
