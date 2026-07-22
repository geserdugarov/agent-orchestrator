# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base sync models."""
from __future__ import annotations

from orchestrator import base_sync as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
WorkflowLabel = _owner.WorkflowLabel
config = _owner.config
dataclass = _owner.dataclass


@dataclass(frozen=True)
class _AutoRebaseContext:
    """Stable inputs for one refresh-time PR rebase attempt."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    worktree: Path
    pr_number: int
    behind: int
    label: Optional[WorkflowLabel]
    pending_pre_rebase_sha: Optional[str]


@dataclass(frozen=True)
class _AutoRebaseRequest:
    """Legacy refresh arguments before derived synchronization fields."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    worktree: Path
    pr_number: int
    behind: int

    def to_context(self, pending_field: str) -> _AutoRebaseContext:
        """Derive label and recovery state at the compatibility boundary."""
        return _AutoRebaseContext(
            gh=self.gh,
            spec=self.spec,
            issue=self.issue,
            state=self.state,
            worktree=self.worktree,
            pr_number=self.pr_number,
            behind=self.behind,
            label=self.gh.workflow_label(self.issue),
            pending_pre_rebase_sha=self.state.get(pending_field),
        )


@dataclass(frozen=True)
class _AutoRebaseRecoveryContext:
    """Stable inputs for finalizing one interrupted auto-rebase."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    worktree: Path
    pr_number: int
    label: str
    pending_pre_rebase_sha: str
    behind: int = 0
    unparking_consumed_max: Optional[int] = None


@dataclass(frozen=True)
class _AutoRebaseRecoverySnapshot:
    """Local and remote branch state observed during crash recovery."""

    branch: str
    local_head: str
    remote_head: str = ""
    ahead: int = 0
    behind: int = 0


@dataclass(frozen=True)
class _AutoRebaseDecision:
    """Whether the coordinator should continue its normal rebase flow."""

    should_continue: bool
    consumed_comment_id: Optional[int] = None


@dataclass(frozen=True)
class _ConflictRouteContext:
    """Stable inputs for routing an auto-rebase conflict to its handler."""

    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    pr_number: int
    label: str
    behind: int
    conflicted_files: list[str]
    pr_head_sha: Optional[str]
