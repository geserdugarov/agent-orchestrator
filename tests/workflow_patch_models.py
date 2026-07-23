# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed inputs and basic mock builders for workflow test runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock

from orchestrator.agents import AgentResult


@dataclass(frozen=True)
class _AgentResultSeed:
    session_id: str = "sess-1"
    last_message: str = ""
    timed_out: bool = False
    interrupted: bool = False
    stderr: str = ""
    exit_code: Optional[int] = None


@dataclass(frozen=True)
class _WorkflowRunContext:
    run_agent: Any
    has_new_commits: Any = False
    dirty_files: tuple = ()
    push_branch: bool = True
    head_shas: tuple = ("",)
    first_commit_subject: str = ""
    fallback_prefix: Optional[str] = None
    squash_result: tuple = (True, None, 0, None)
    branch_ahead_behind: tuple = (0, 0)
    rebase_in_progress: bool = False
    verify_result: Any = None
    authed_fetch_result: Any = None
    analytics_log_path: Any = None
    trajectory_log_path: Any = None


def _agent(**agent_fields) -> AgentResult:
    seed = _AgentResultSeed(**agent_fields)
    exit_code = seed.exit_code
    if exit_code is None:
        exit_code = -1 if seed.timed_out else 0
    return AgentResult(
        session_id=seed.session_id,
        last_message=seed.last_message,
        exit_code=exit_code,
        timed_out=seed.timed_out,
        stdout="",
        stderr=seed.stderr,
        interrupted=seed.interrupted,
    )


def _default_infer_subject_prefix(spec, worktree, issue):
    labels = {
        (getattr(label, "name", "") or "").lower()
        for label in (getattr(issue, "labels", None) or [])
    }
    return "fix" if {"bug", "fix"} & labels else "feat"


def _as_mock(value_or_sequence):
    if callable(value_or_sequence):
        return value_or_sequence
    mock = MagicMock()
    if isinstance(value_or_sequence, (list, tuple)):
        mock.side_effect = list(value_or_sequence)
    else:
        mock.return_value = value_or_sequence
    return mock
