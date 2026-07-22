# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition parent scan."""
from __future__ import annotations

from orchestrator.stages import _decomposition_state as _state
from orchestrator.stages import decomposition as _owner

_ChildScan = _owner._ChildScan
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
config = _owner.config
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_CHILDREN = _state._CHILDREN
_DONE = _state._DONE


def _route_parent_drift(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> bool:
    """Route a decomposed parent (or blocked child) back to `decomposing`
    on a user-content edit.

    Returns True when drift was detected and the issue was re-routed
    (caller must return); False when the content is unchanged.

    The hash baseline is initialized by `_detect_user_content_change`
    itself on the first encounter, so a legacy issue still missing the
    field is durably seeded (via the helper's own `write_pinned_state`)
    rather than silently absorbing the next edit as the new baseline. Both
    parent and child cases route to decomposing so the manifest is
    re-derived against the updated body: silently persisting the new
    baseline for a child would let `_handle_ready` later see a matching
    hash and skip the re-decomposer even when the edited body now needs
    splitting. Parents with in-flight children list those children as
    orphans in the notice (the new manifest may overlap; the operator
    closes the obsolete ones manually).
    """
    from orchestrator import workflow as _wf

    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is None:
        return False
    orphans = list(state.get(_CHILDREN) or [])
    _wf._route_drift_to_decomposing(gh, issue, state, new_hash, orphans)
    gh.write_pinned_state(issue, state)
    return True


def _read_child_labels(
    gh: GitHubClient, issue: Issue, children: list,
) -> Optional[_ChildScan]:
    """Fetch each recorded child issue and its current workflow label.

    Returns a child scan with issues and labels keyed by child number, or
    None if any child read raised (the caller returns and the tick retries
    on the next poll). Labels are read fresh here: the family-aware bucket
    (see `workflow._FAMILY_AWARE_LABELS`) serializes decomposing / blocked
    / umbrella within a tick, so a child's own label flip cannot race this
    read.
    """
    from orchestrator import workflow as _wf

    child_labels: dict[int, Optional[str]] = {}
    child_issues: dict[int, Issue] = {}
    for child_number in children:
        try:
            child_issue = gh.get_issue(int(child_number))
        except Exception:
            _wf.log.exception(
                "issue=#%s could not read child #%d", issue.number, child_number,
            )
            return None
        child_issues[int(child_number)] = child_issue
        child_labels[int(child_number)] = gh.workflow_label(child_issue)
    return _ChildScan(children, child_issues, child_labels)


def _park_rejected_children(
    gh: GitHubClient, issue: Issue, state: PinnedState, child_labels: dict,
) -> bool:
    """Park the parent when any child carries the `rejected` label.

    Returns True when parked (caller must return); False otherwise.
    Idempotent by `awaiting_human` so a rejected child does not re-park
    every tick.
    """
    from orchestrator import workflow as _wf

    rejected = [
        child_number
        for child_number, child_label in child_labels.items()
        if child_label == "rejected"
    ]
    if not rejected:
        return False
    if state.get(_AWAITING_HUMAN):
        return True
    rejected_refs = _owner._issue_ref_list(rejected)
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} child issue(s) rejected: "
        f"{rejected_refs}; "
        "decide whether to re-decompose or close.",
        reason="child_rejected",
    )
    gh.write_pinned_state(issue, state)
    return True


def _park_manually_closed_children(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
    scan: _ChildScan,
) -> bool:
    """Park the parent when a child was closed without reaching a terminal
    label.

    Returns True when parked (caller must return); False otherwise. On the
    way, each closed candidate is retried against the PR-merge finalize
    helper and its `child_labels` entry is flipped to `done` if the merge
    finalized -- so an externally-merged child whose label was never
    advanced past an in-flight stage no longer strands the aggregation.

    A child closed manually (e.g. via the GitHub UI) before reaching
    `in_review` is invisible to `list_pollable_issues`, which only sweeps
    closed issues for a small label set (the externally-merged path). Its
    workflow label stays frozen at whatever it was at close, so without
    this branch the parent would read the stale label, neither the rejected
    nor the all-done branch would fire, and the parent would wait forever
    for a child that is gone. `in_review` is intentionally allowed: a
    state=closed/label=in_review child is the externally-merged transient
    that the closed-in_review sweep finalizes on the next tick, NOT a manual
    override.
    """
    from orchestrator import workflow as _wf

    manually_closed = _owner._manually_closed_children(scan)
    if manually_closed:
        manually_closed = _owner._remaining_manually_closed(
            gh, spec, scan, manually_closed,
        )
    if not manually_closed:
        return False
    if state.get(_AWAITING_HUMAN):
        return True
    closed_refs = _owner._issue_ref_list(manually_closed)
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} child issue(s) closed without reaching "
        f"`done` or `rejected`: "
        f"{closed_refs}; "
        "decide whether to re-decompose or close.",
        reason="child_manually_closed",
    )
    gh.write_pinned_state(issue, state)
    return True


def _manually_closed_children(scan: _ChildScan) -> list[int]:
    return [
        number for number, child_issue in scan.issues.items()
        if getattr(child_issue, "state", "open") == "closed"
        and scan.labels.get(number) not in (_DONE, "rejected", "in_review")
    ]


def _remaining_manually_closed(
    gh: GitHubClient,
    spec: config.RepoSpec,
    scan: _ChildScan,
    candidates: list[int],
) -> list[int]:
    from orchestrator import workflow as _wf

    remaining: list[int] = []
    for number in candidates:
        child_issue = scan.issues[number]
        child_state = gh.read_pinned_state(child_issue)
        if _wf._finalize_if_pr_merged(gh, spec, child_issue, child_state):
            scan.labels[number] = _DONE
        else:
            remaining.append(number)
    return remaining
