# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Conflict models."""
from __future__ import annotations

from orchestrator.stages import conflicts as _owner

AgentResult = _owner.AgentResult
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass


@dataclass(frozen=True)
class _ConflictContext:
    """The per-tick `resolving_conflict` handles, bundled so the rebase-loop
    helpers thread them as a single value instead of four positional
    arguments (mirrors fixing's `_FixingContext`)."""
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState


@dataclass(frozen=True)
class _WorktreeSync:
    """A PR worktree measured against its remote branch tip: the worktree
    path, the branch name, and how far HEAD is ahead / behind the freshly
    fetched `<remote>/<branch>` head."""
    worktree: Path
    branch: str
    ahead: int
    behind: int


@dataclass(frozen=True)
class _DivergeDecision:
    """Verdict of the diverged-worktree guard: whether the tick parked, plus
    the force-publish lease pinned to a validated orchestrator-produced PR
    head when an already-rebased worktree may be force-published instead."""
    parked: bool
    publish_lease: Optional[str] = None


@dataclass(frozen=True)
class _ConflictResumeRun:
    """The outputs of one locked dev resume in the rebase loop: the worktree
    the agent ran in (`_resume_dev_with_text` may re-create it), the agent
    result, and whether an operator paused mid-run."""
    worktree: Path
    dev_result: AgentResult
    paused: bool
