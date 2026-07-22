# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing models."""
from __future__ import annotations

from orchestrator.stages import implementing as _owner

AgentResult = _owner.AgentResult
Optional = _owner.Optional
Path = _owner.Path
dataclass = _owner.dataclass


@dataclass(frozen=True)
class _PreparedDevRun:
    agent_result: AgentResult
    before_sha: Optional[str]
    paused: bool
    worktree: Path


@dataclass(frozen=True)
class _AgentWork:
    agent_result: AgentResult
    worktree: Path


@dataclass(frozen=True)
class _PRWork(_AgentWork):
    branch: str


@dataclass(frozen=True)
class _DevSession:
    spec: str
    backend: str
    extra_args: tuple[str, ...]
    session_id: Optional[str]


@dataclass(frozen=True)
class _DevResumePlan:
    session: _DevSession
    fresh_spawn: bool
    resume_count: int


@dataclass(frozen=True)
class _DevResumeOptions:
    followup_has_tracked_repos: bool = False
    pause_guard: bool = False

    @classmethod
    def from_fields(cls, fields: dict) -> _DevResumeOptions:
        unknown = set(fields) - {"followup_has_tracked_repos", "pause_guard"}
        if unknown:
            raise TypeError(f"unexpected resume option(s): {sorted(unknown)!r}")
        return cls(**fields)
