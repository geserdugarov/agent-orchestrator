# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""The worktree a resume runs in, restored when a prior tick's is gone.

Every resume path -- the human reply, the drift followup, and the parked
`/orchestrator continue` retry -- needs a checkout to run the agent in, and any
of them can arrive after the directory was reaped (an operator cleaned up, or a
terminal stage removed it). Reusing an existing path is the common case and
must stay cheap; recreating one has to go through the pinned branch name so the
restored worktree lands back on the branch this issue's commits are on rather
than a freshly derived one.

It sits apart from the resume owners because all three of them ask for it
before they have a session, a prompt, or a decision -- and because a caller
that skipped it would silently run the agent in whatever directory the path
happened to point at.
"""
from __future__ import annotations

from pathlib import Path

from github.Issue import Issue

from orchestrator import config
from orchestrator.git.worktrees import (
    creation as _worktree_creation,
    paths as _worktree_paths,
)
from orchestrator.github.pinned_state import PinnedState


def _ensure_resume_worktree(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Path:
    worktree = _worktree_paths._worktree_path(spec, issue.number)
    if worktree.exists():
        return worktree
    return _worktree_creation._ensure_worktree(
        spec,
        issue.number,
        branch=_worktree_paths._resolve_branch_name(state, spec, issue.number),
    )
