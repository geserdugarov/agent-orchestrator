# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared agent dispatch: backend selection, result assembly, spawn logging."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Unpack

from orchestrator.agents import models as _agent_models
from orchestrator.agents import sessions as _agent_sessions

log = logging.getLogger("orchestrator.agents")


def resolve_agent_run_options(
    options: Optional[_agent_models.AgentRunOptions],
    option_fields: _agent_models.AgentRunOptionFields,
) -> _agent_models.AgentRunOptions:
    """Normalize the object and legacy keyword option forms."""
    if options is not None and option_fields:
        raise TypeError("pass either options or keyword option fields, not both")
    if options is not None:
        return options
    return _agent_models.AgentRunOptions(**option_fields)


def build_agent_result(
    options: _agent_models.AgentRunOptions,
    process_result: _agent_models.SubprocessResult,
    last_message: str,
) -> _agent_models.AgentResult:
    """Combine process output with its persisted or parsed session id."""
    return _agent_models.AgentResult(
        session_id=(
            options.resume_session_id
            or _agent_sessions.parse_session_id(process_result.stdout)
        ),
        last_message=last_message,
        exit_code=process_result.exit_code,
        timed_out=process_result.timed_out,
        stdout=process_result.stdout,
        stderr=process_result.stderr,
        interrupted=process_result.interrupted,
    )


def log_agent_spawn(
    backend: str,
    cwd: Path,
    options: _agent_models.AgentRunOptions,
) -> None:
    """Log one backend spawn without exposing its prompt or environment."""
    log.info(
        "%s spawn: cwd=%s resume=%s timeout=%ss",
        backend,
        cwd,
        bool(options.resume_session_id),
        options.timeout_seconds,
    )


def run_agent(
    backend: str,
    prompt: str,
    cwd: Path,
    *,
    options: Optional[_agent_models.AgentRunOptions] = None,
    **option_fields: Unpack[_agent_models.AgentRunOptionFields],
) -> _agent_models.AgentResult:
    """Dispatch to Codex or Claude with normalized optional controls."""
    run_options = resolve_agent_run_options(options, option_fields)
    # Import the backend owner at call time and read its `run_*` entry then,
    # so a test that patches `codex.run_codex` / `claude.run_claude` on the
    # owner module intercepts dispatch, and reaching a sibling subpackage
    # never re-enters the package mid-initialization.
    from orchestrator.agents.backends import claude, codex
    if backend == "codex":
        backend_runner = codex.run_codex
    elif backend == "claude":
        backend_runner = claude.run_claude
    else:
        raise ValueError(
            f"unknown agent backend {backend!r}; expected 'codex' or 'claude'",
        )
    return backend_runner(prompt, cwd, options=run_options)
