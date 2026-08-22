# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""A parent whose whole intent is covered by its children.

`umbrella` polls exactly like `blocked` -- same rejected / manually-closed
parks, same dep-graph activation walk -- and differs only in what "every child
resolved" earns. There is no implementation pass to re-enter, so the parent
resolves to `done` and closes instead of flipping to `ready`.

That missing implementation pass is also why the drift check matters more here
than anywhere else: no later stage will ever look at this issue's body again,
so a body edited while children ran would otherwise be closed against the
manifest it no longer describes.

It is also the last boundary at which anything the issue still owes a remote
can be settled, and the first at which the snapshot half CAN be. A parent that
became an umbrella through a late split owes two things -- the branch its
superseded candidate was committed on, and the immutable ref that candidate was
preserved under -- and nothing else ever brings a tick back to either, because
an umbrella polls its children and nothing else. So the all-resolved branch
reconciles what is owed before it closes anything: the branch unconditionally,
and the snapshot under the rule that owns it, since every recorded direct
consumer being terminal is exactly what all-resolved has just made true. The
child scan is handed over rather than re-taken, so proving that costs no
request of its own. A remote that refuses holds the parent open, because an
umbrella closed over an unreclaimed ref is an obligation nobody would ever
settle, while one still open is a retry every tick.
"""
from __future__ import annotations

import logging

from github.Issue import Issue

from orchestrator import config
from orchestrator.github.client import GitHubClient
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import comments as _comments
from orchestrator.workflow.engine import guards as _guards
from orchestrator.workflow.engine import usage as _usage
from orchestrator.workflow.stages.decomposition import activation as _activation
from orchestrator.workflow.stages.decomposition import (
    late_cleanup as _late_cleanup,
)
from orchestrator.workflow.stages.decomposition import parents as _parents
from orchestrator.workflow.stages.decomposition import state as _state
from orchestrator.workflow.state import WorkflowLabel

log = logging.getLogger("orchestrator.workflow")


def _handle_empty_umbrella(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    if state.get(_state._AWAITING_HUMAN):
        return
    _guards._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `{WorkflowLabel.UMBRELLA}` without "
        "recorded children; "
        "manual relabel suspected.",
        reason="umbrella_no_children",
    )
    gh.write_pinned_state(issue, state)


def _complete_umbrella(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    close_body = ":white_check_mark: all children resolved; closing umbrella issue."
    verdict = _usage._format_issue_usage_verdict(state)
    if verdict:
        close_body = f"{close_body}\n\n{verdict}"
    _comments._post_issue_comment(gh, issue, state, close_body)
    state.set(_state._AWAITING_HUMAN, False)
    state.set(_state._PARK_REASON, None)
    state.set("umbrella_resolved_at", _usage._now_iso())
    gh.set_workflow_label(issue, WorkflowLabel.DONE)
    gh.write_pinned_state(issue, state)
    try:
        issue.edit(state="closed")
    except Exception:
        log.exception(
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
    if _parents._route_parent_drift(gh, issue, state):
        return

    children = state.get(_state._CHILDREN) or []
    if not children:
        _handle_empty_umbrella(gh, issue, state)
        return

    scan = _parents._usable_child_scan(gh, spec, issue, state, children)
    if scan is None:
        return
    if all(label == _state._DONE for label in scan.labels.values()):
        # Every child is resolved, so this is the last tick that could settle
        # what the issue still owes a remote -- and the only one that will
        # come back if it cannot. A refusal keeps the label, which is the
        # retry.
        if _late_cleanup._settled_for_terminal(gh, spec, issue, state, scan):
            _complete_umbrella(gh, issue, state)
        return

    held = _activation._activate_ready_children(gh, issue, state, scan)
    _activation._log_held_children(
        issue, _state._UMBRELLA, children, scan.labels, held,
    )
