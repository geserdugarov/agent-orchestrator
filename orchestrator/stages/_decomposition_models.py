# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Decomposition models."""
from __future__ import annotations

from orchestrator.stages import decomposition as _owner

AgentResult = _owner.AgentResult
Issue = _owner.Issue
Optional = _owner.Optional
Tuple = _owner.Tuple
config = _owner.config
dataclass = _owner.dataclass


@dataclass
class _DecomposerRunPlan:
    agent_result: Optional[AgentResult]
    keep_worktree: bool = False


@dataclass
class _DecomposerCleanup:
    """Close one decomposer worktree unless its run requests inspection."""

    spec: config.RepoSpec
    issue_number: int
    run_plan: _DecomposerRunPlan

    def close(self) -> None:
        from orchestrator import workflow as _wf

        if not self.run_plan.keep_worktree:
            _wf._cleanup_decompose_worktree(self.spec, self.issue_number)


@dataclass(frozen=True)
class _DecomposerSession:
    spec: str
    backend: str
    extra_args: tuple[str, ...]
    session_id: Optional[str]


@dataclass
class _SplitPlan:
    children_manifest: list
    is_umbrella: bool
    created: list[Tuple[int, dict]]
    dep_graph: dict[str, list[int]]

    @classmethod
    def start(cls, children_manifest: list, is_umbrella: bool) -> _SplitPlan:
        return cls(children_manifest, is_umbrella, [], {})

    def record(self, idx: int, issue_number: int, child: dict) -> None:
        self.created.append((issue_number, child))
        depends_on = list(child.get("depends_on") or [])
        if depends_on:
            self.dep_graph[str(idx)] = depends_on


@dataclass(frozen=True)
class _ChildScan:
    children: list
    issues: dict[int, Issue]
    labels: dict[int, Optional[str]]
