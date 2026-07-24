# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Codex command construction, scratch output, and execution."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Unpack

from orchestrator import _agent_runner_common, config
from orchestrator.agents import environment as _agent_environment
from orchestrator.agents import models as _agent_models
from orchestrator.agents import processes as _agent_processes


@contextmanager
def codex_last_message_file() -> Iterator[Path]:
    """Yield a per-spawn last-message path outside the worktree."""
    descriptor, path_string = tempfile.mkstemp(
        prefix="codex-last-",
        suffix=".txt",
    )
    os.close(descriptor)
    last_message_path = Path(path_string)
    try:
        yield last_message_path
    finally:
        last_message_path.unlink(missing_ok=True)


def read_last_message(last_message_path: Path) -> str:
    """Read Codex's last-message file, or empty text when unavailable."""
    if not last_message_path.exists():
        return ""
    try:
        return last_message_path.read_text(errors="replace")
    except OSError:
        return ""


def codex_command(
    prompt: str,
    cwd: Path,
    last_message_path: Path,
    options: _agent_models.AgentRunOptions,
) -> list[str]:
    """Build fresh or resumed Codex argv with fixed safety/output flags."""
    absolute_cwd = Path(cwd).resolve()
    common_args = [
        "--dangerously-bypass-approvals-and-sandbox",
        "--json",
        "-o",
        str(last_message_path),
    ]
    if options.resume_session_id:
        return [
            config.CODEX_BIN,
            *options.extra_args,
            "exec",
            "resume",
            *common_args,
            options.resume_session_id,
            prompt,
        ]
    return [
        config.CODEX_BIN,
        *options.extra_args,
        "exec",
        "-C",
        str(absolute_cwd),
        *common_args,
        prompt,
    ]


def run_codex(
    prompt: str,
    cwd: Path,
    *,
    options: Optional[_agent_models.AgentRunOptions] = None,
    **option_fields: Unpack[_agent_models.AgentRunOptionFields],
) -> _agent_models.AgentResult:
    """Run Codex through the shared process owner."""
    run_options = _agent_models.resolve_agent_run_options(options, option_fields)
    with codex_last_message_file() as last_message_path:
        _agent_runner_common.log_agent_spawn("codex", cwd, run_options)
        process_result = _agent_processes.run_subprocess(
            codex_command(prompt, cwd, last_message_path, run_options),
            cwd,
            _agent_environment.agent_env(run_options.extra_env),
            run_options.timeout_seconds,
        )
        return _agent_runner_common.build_agent_result(
            run_options,
            process_result,
            read_last_message(last_message_path),
        )
