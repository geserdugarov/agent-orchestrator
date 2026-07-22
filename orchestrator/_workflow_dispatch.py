# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow dispatch."""
from __future__ import annotations

from orchestrator import _workflow_state as _state
from orchestrator import workflow as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
analytics = _owner.analytics
config = _owner.config
hard_skip_control_label = _owner.hard_skip_control_label
sys = _owner.sys
time = _owner.time
_ISSUE_HANDLER_NAMES = _state._ISSUE_HANDLER_NAMES
log = _state.log


def _route_issue_to_handler(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, label: Optional[str],
) -> None:
    """Dispatch one issue to its stage handler by workflow label.

    The handlers are looked up as module globals so a test that patches
    ``workflow._handle_<stage>`` intercepts the call even though the dispatch
    lives here. ``done`` / ``rejected`` are terminal no-ops; an unrecognized
    label is logged and left alone for a human. Timing and the
    ``stage_evaluation`` analytics record stay in ``_process_issue``, which
    wraps this call in its try / except / finally.
    """
    handler_name = _ISSUE_HANDLER_NAMES.get(label)
    if handler_name is not None:
        issue_handler = getattr(sys.modules[_owner.__name__], handler_name)
        issue_handler(gh, spec, issue)
    elif label not in ("done", "rejected"):
        log.warning(
            "repo=%s issue=#%s label=%r not implemented yet; leaving alone",
            spec.slug, issue.number, label,
        )


def _process_issue(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    # Postponed-task hold: applying `backlog` (or `paused`) parks the issue
    # outside the state machine entirely until the label is removed. Checked
    # before reading the workflow label so the orchestrator never decomposes,
    # spawns an agent, or otherwise reacts while the operator is using the
    # label as a "not yet" signal. Hard-skips are NOT counted as a stage
    # evaluation: no handler runs and there is nothing to time.
    skip_label = hard_skip_control_label(issue)
    if skip_label is not None:
        log.info(
            "repo=%s issue=#%s has %r; skipping",
            spec.slug, issue.number, skip_label,
        )
        return
    label = gh.workflow_label(issue)
    log.info("repo=%s issue=#%s label=%r", spec.slug, issue.number, label)
    # Time the handler dispatch and append a single `stage_evaluation`
    # analytics record on exit. `evaluation_result` flips to "error" inside the
    # except clause so an unhandled exception still produces a timing
    # record before propagating -- the tick loop's per-issue try/except
    # already logs and isolates the failure, so re-raising here keeps
    # the existing dispatch / exception contract intact. The append
    # itself is internally hardened against OSError; an analytics
    # misconfiguration cannot stop the per-issue tick from advancing.
    start = time.monotonic()
    evaluation_result = "ok"
    try:
        _owner._route_issue_to_handler(gh, spec, issue, label)
    except Exception:
        evaluation_result = "error"
        raise
    finally:
        duration_s = round(time.monotonic() - start, 3)
        analytics.record_stage_evaluation(
            repo=getattr(gh, "_repo_slug", None) or "",
            issue=issue.number,
            stage=label,
            duration_s=duration_s,
            result=evaluation_result,
        )
