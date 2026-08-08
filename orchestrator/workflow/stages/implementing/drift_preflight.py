# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""What the state has to answer before an awaiting-human tick spawns anything.

Two situations reach here, and both are about work that already exists without a
session to explain it. A body edit that lands before any dev session was
recorded normally just falls through to a fresh spawn against the new body --
unless the worktree carries unpushed commits from an earlier tick, in which case
the tick refuses: those commits never saw the edited requirements, and pushing
them would publish work against a spec the human just changed. Whether to
discard them is the operator's call, so it parks as `stale_recovered_work`.

The other is an `agent_timeout` park nobody has replied to. That park is
retryable without a human, so a tick with no new comment tries the quiet
recovery first -- publishing a commit that landed after the timeout -- and only
then falls through. The no-comment condition is the whole gate: once a human HAS
replied, the reply is the signal and the resume path owns the tick instead.
"""
from __future__ import annotations

from typing import Optional

from github.Issue import Issue

from orchestrator import config
from orchestrator.git.verification import probes as _verification_probes
from orchestrator.git.worktrees import (
    creation as _worktree_creation,
    paths as _worktree_paths,
)
from orchestrator.github.client import GitHubClient
from orchestrator.github.comments import filter_trusted
from orchestrator.github.pinned_state import PinnedState
from orchestrator.workflow.engine import (
    comments as _comments,
    drift as _engine_drift,
    guards as _guards,
)
from orchestrator.workflow.stages.implementing import (
    disposition as _disposition,
    models as _models,
    resume as _resume,
    state as _state,
    worktree as _worktree,
)


def _handle_pre_session_drift(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    worktree = _worktree_paths._worktree_path(spec, issue.number)
    if _worktree_creation._has_new_commits(spec, worktree):
        _guards._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} issue body changed but the "
            "worktree carries unpushed commits from a previous tick "
            "and no dev session is recorded. Refusing to push commits "
            "that never saw the edited requirements; decide whether "
            "to discard the recovered work (reset the branch) and "
            "let a fresh agent run, or accept it as-is.",
            reason="stale_recovered_work",
        )
        gh.write_pinned_state(issue, state)
        return True
    if state.get(_state._AWAITING_HUMAN):
        _comments._post_issue_comment(
            gh, issue, state,
            ":pencil2: issue content changed; clearing the park and "
            "spawning a fresh dev run against the updated requirements.",
        )
        _engine_drift._mark_drift_comments_consumed(gh, issue, state)
        state.set(_state._AWAITING_HUMAN, False)
        state.set(_state._PARK_REASON, None)
    return False


def _recover_quiet_implementer_timeout(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> bool:
    if state.get(_state._PARK_REASON) != _state._AGENT_TIMEOUT:
        return False
    comments = filter_trusted(
        gh.comments_after(issue, state.get(_state._LAST_ACTION_COMMENT_ID))
    )
    if comments:
        return False
    recovery = _disposition._try_recover_implementing_timeout_park(
        gh, spec, issue, state,
    )
    if recovery == "pushed":
        gh.write_pinned_state(issue, state)
    return True


def _prepare_awaiting_dev_run(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Optional[_models._PreparedDevRun]:
    if _recover_quiet_implementer_timeout(gh, spec, issue, state):
        return None
    worktree = _worktree._ensure_resume_worktree(spec, issue, state)
    before_sha = _verification_probes._head_sha(worktree)
    resumed = _resume._resume_developer_on_human_reply(
        gh, spec, issue, state, pause_guard=True,
    )
    if resumed is None:
        return None
    worktree, agent_result, paused = resumed
    return _models._PreparedDevRun(agent_result, before_sha, paused, worktree)
