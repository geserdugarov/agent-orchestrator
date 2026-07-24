# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Claude command construction and execution."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Unpack

from orchestrator import config
from orchestrator.agents import environment as _agent_environment
from orchestrator.agents import models as _agent_models
from orchestrator.agents import processes as _agent_processes
from orchestrator.agents import runner as _agent_runner
from orchestrator.agents import sessions as _agent_sessions


def claude_command(
    prompt: str,
    options: _agent_models.AgentRunOptions,
) -> list[str]:
    """Build Claude argv with fixed safety and stream-json flags."""
    command = [
        config.CLAUDE_BIN,
        *options.extra_args,
        "-p",
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if options.resume_session_id:
        command += ["--resume", options.resume_session_id]
    command.append(prompt)
    return command


def claude_process_last_message(
    process_result: _agent_models.SubprocessResult,
) -> str:
    """Allow partial-message fallback only for clean completions."""
    succeeded = (
        process_result.exit_code == 0
        and not process_result.timed_out
        and not process_result.interrupted
    )
    return _agent_sessions.claude_last_message(
        process_result.stdout,
        allow_assistant_fallback=succeeded,
    )


def run_claude(
    prompt: str,
    cwd: Path,
    *,
    options: Optional[_agent_models.AgentRunOptions] = None,
    **option_fields: Unpack[_agent_models.AgentRunOptionFields],
) -> _agent_models.AgentResult:
    """Run Claude through the shared process owner."""
    run_options = _agent_runner.resolve_agent_run_options(options, option_fields)
    _agent_runner.log_agent_spawn("claude", cwd, run_options)
    process_result = _agent_processes.run_subprocess(
        claude_command(prompt, run_options),
        cwd,
        _agent_environment.agent_env(run_options.extra_env),
        run_options.timeout_seconds,
    )
    return _agent_runner.build_agent_result(
        run_options,
        process_result,
        claude_process_last_message(process_result),
    )
