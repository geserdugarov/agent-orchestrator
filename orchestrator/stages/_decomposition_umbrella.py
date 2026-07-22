# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition umbrella."""
from __future__ import annotations

from orchestrator.stages import _decomposition_state as _state
from orchestrator.stages import decomposition as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_CHILDREN = _state._CHILDREN
_DONE = _state._DONE
_PARK_REASON = _state._PARK_REASON
_UMBRELLA = _state._UMBRELLA


def _handle_empty_umbrella(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    if state.get(_AWAITING_HUMAN):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `umbrella` without recorded children; "
        "manual relabel suspected.",
        reason="umbrella_no_children",
    )
    gh.write_pinned_state(issue, state)


def _complete_umbrella(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    from orchestrator import workflow as _wf

    close_body = ":white_check_mark: all children resolved; closing umbrella issue."
    verdict = _wf._format_issue_usage_verdict(state)
    if verdict:
        close_body = f"{close_body}\n\n{verdict}"
    _wf._post_issue_comment(gh, issue, state, close_body)
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
    state.set("umbrella_resolved_at", _wf._now_iso())
    gh.set_workflow_label(issue, WorkflowLabel.DONE)
    gh.write_pinned_state(issue, state)
    try:
        issue.edit(state="closed")
    except Exception:
        _wf.log.exception(
            "issue=#%s could not close umbrella after children done",
            issue.number,
        )


def _handle_umbrella(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    """Poll children on an umbrella parent that has no implementation of
    its own.

    Mirrors `_handle_blocked` for the rejected/manually-closed checks and
    the dep-graph activation walk, but the all-done branch resolves the
    umbrella to `done` and closes the issue instead of flipping it to
    `ready` -- there is no implementation pass for an umbrella, so the
    only terminal path is "every child resolved -> close".
    """
    state = gh.read_pinned_state(issue)

    # An umbrella parent NEVER enters implementation -- it just closes when
    # every child resolves -- so a body edit cannot be picked up by any
    # later stage's drift check. Route it back to decomposing here so the
    # new manifest is re-derived against the updated body; without this
    # route-back, an edited umbrella would silently close to `done` against
    # the stale manifest once the old children finished.
    if _owner._route_parent_drift(gh, issue, state):
        return

    children = state.get(_CHILDREN) or []
    if not children:
        _owner._handle_empty_umbrella(gh, issue, state)
        return

    scan = _owner._usable_child_scan(gh, spec, issue, state, children)
    if scan is None:
        return
    if all(label == _DONE for label in scan.labels.values()):
        _owner._complete_umbrella(gh, issue, state)
        return

    held = _owner._activate_ready_children(gh, issue, state, scan)
    _owner._log_held_children(issue, _UMBRELLA, children, scan.labels, held)
