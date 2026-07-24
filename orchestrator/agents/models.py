# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Agent run options and result models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Optional, TypedDict

from orchestrator import config
from orchestrator.usage import UsageMetrics


@dataclass
class AgentResult:
    """Normalized outcome returned by either supported agent backend."""

    session_id: Optional[str]
    last_message: str
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    interrupted: bool = False
    usage: Optional[UsageMetrics] = None


CodexResult = AgentResult


@dataclass(frozen=True)
class AgentRunOptions:
    """Optional controls shared by fresh agent runs and session resumes."""

    resume_session_id: Optional[str] = None
    extra_env: Optional[dict[str, str]] = None
    timeout: Optional[int] = None
    extra_args: tuple[str, ...] = ()

    @property
    def timeout_seconds(self) -> int:
        return self.timeout or config.AGENT_TIMEOUT


class AgentRunOptionFields(TypedDict, total=False):
    """Legacy keyword controls accepted beside ``AgentRunOptions``."""

    resume_session_id: Optional[str]
    extra_env: Optional[dict[str, str]]
    timeout: Optional[int]
    extra_args: tuple[str, ...]


class SubprocessResult(NamedTuple):
    """Captured process streams and termination classification."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    interrupted: bool
