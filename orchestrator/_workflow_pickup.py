# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow pickup."""
from __future__ import annotations

from orchestrator import _workflow_state as _state
from orchestrator import workflow as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
log = _state.log


def _pickup_author_allowed(spec: config.RepoSpec, issue: Issue) -> bool:
    # Author allowlist: when configured, silently skip unlabeled issues from
    # anyone outside the list so random users can't burn agent budget on a
    # public repo. Maintainers can still drive an outsider's issue manually
    # by adding a workflow label themselves -- the guard only fires here.
    if not config.ALLOWED_ISSUE_AUTHORS:
        return True
    author = getattr(getattr(issue, "user", None), "login", None) or ""
    allowed = {
        github_handle.lower()
        for github_handle in config.ALLOWED_ISSUE_AUTHORS
    }
    if author.lower() in allowed:
        return True
    log.info(
        "repo=%s issue=#%s author=%r not in ALLOWED_ISSUE_AUTHORS; skipping pickup",
        spec.slug, issue.number, author,
    )
    return False


def _record_pickup_comment(state: PinnedState, pickup) -> None:
    pickup_id = getattr(pickup, "id", None)
    if pickup_id is not None:
        state.set("pickup_comment_id", int(pickup_id))


def _start_decomposing(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> None:
    pickup = _owner._post_issue_comment(
        gh, issue, state,
        ":robot: orchestrator picking this up; decomposing.",
    )
    _owner._record_pickup_comment(state, pickup)
    state.set(
        "user_content_hash",
        _owner._compute_user_content_hash(issue, _owner._orchestrator_ids(state)),
    )
    gh.set_workflow_label(issue, WorkflowLabel.DECOMPOSING)
    gh.write_pinned_state(issue, state)
    _owner._handle_decomposing(gh, spec, issue)


def _start_implementing(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> None:
    # Legacy path with DECOMPOSE=off: skip decomposition entirely and route
    # the unlabeled issue straight to implementing, exactly as the
    # bootstrap-milestone code did.
    pickup = _owner._post_issue_comment(
        gh, issue, state,
        ":robot: orchestrator picking this up. Decomposition stage is "
        "disabled; going straight to implementation.",
    )
    # Anchor the validating-handoff seed-watermark on the exact pickup
    # comment id. Without this, an issue that started under an older
    # version of the orchestrator (where bot ids were not tracked) would
    # have its first recorded bot id be a much later comment (PR-opened or
    # approval), causing `_seed_watermark_past_self` to silently advance
    # past every issue/PR comment in between -- including any human
    # "do not merge yet" posted during implementing.
    _owner._record_pickup_comment(state, pickup)
    state.set(
        "user_content_hash",
        _owner._compute_user_content_hash(issue, _owner._orchestrator_ids(state)),
    )
    gh.set_workflow_label(issue, WorkflowLabel.IMPLEMENTING)
    gh.write_pinned_state(issue, state)
    _owner._handle_implementing(gh, spec, issue)


def _handle_pickup(gh: GitHubClient, spec: config.RepoSpec, issue: Issue) -> None:
    if not _owner._pickup_author_allowed(spec, issue):
        return
    state = PinnedState()
    state.set("created_at", _owner._now_iso())
    if config.DECOMPOSE:
        _owner._start_decomposing(gh, spec, issue, state)
    else:
        _owner._start_implementing(gh, spec, issue, state)
