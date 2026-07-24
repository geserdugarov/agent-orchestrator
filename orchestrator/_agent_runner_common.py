# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Result construction and spawn logging shared by agent backends."""
from __future__ import annotations

import logging
from pathlib import Path

from orchestrator import _agent_session
from orchestrator.agents import models as _agent_models

log = logging.getLogger("orchestrator.agents")


def build_agent_result(
    options: _agent_models.AgentRunOptions,
    process_result: _agent_models.SubprocessResult,
    last_message: str,
) -> _agent_models.AgentResult:
    """Combine process output with its persisted or parsed session id."""
    return _agent_models.AgentResult(
        session_id=(
            options.resume_session_id
            or _agent_session.parse_session_id(process_result.stdout)
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
