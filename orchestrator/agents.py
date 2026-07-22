# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable agent API and hardened subprocess-group lifecycle.

Backend commands, transcript parsing, environment filtering, and result
models live in focused private leaves. Process creation remains here so the
historical ``orchestrator.agents.subprocess.Popen`` patch point and shared
shutdown registry retain their exact behavior.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional, Unpack

from orchestrator import _agent_api, _agent_process_registry

_running_procs = _agent_process_registry._running_procs
_running_procs_lock = _agent_process_registry._running_procs_lock
_register_proc = _agent_process_registry.register_proc
_unregister_proc = _agent_process_registry.unregister_proc
_registered = _agent_process_registry.registered

AgentResult = _agent_api.AgentResult
CodexResult = _agent_api.CodexResult
AgentRunOptions = _agent_api.AgentRunOptions
_AgentRunOptionFields = _agent_api.AgentRunOptionFields
_SubprocessResult = _agent_api.SubprocessResult
_resolve_agent_run_options = _agent_api.resolve_agent_run_options
parse_session_id = _agent_api.parse_session_id
_first_nested_uuid = _agent_api.first_nested_uuid
_walk_mapping_for_uuid = _agent_api.walk_mapping_for_uuid
_walk_for_uuid = _agent_api.walk_for_uuid
_UUID_RE = _agent_api.uuid_re
_PRIORITY_KEYS = _agent_api.priority_keys

_filter_agent_env = _agent_api.filter_agent_env
_is_secret_shaped = _agent_api.is_secret_shaped
_agent_env_key_allowed = _agent_api.agent_env_key_allowed
_agent_env = _agent_api.agent_env
_FORBIDDEN_AGENT_ENV = _agent_api.forbidden_agent_env
_AGENT_WRITE_CREDENTIAL_LOCATORS = _agent_api.write_credential_locators
_AGENT_SECRET_SUFFIXES = _agent_api.secret_suffixes
_AGENT_SECRET_BARE_NAMES = _agent_api.secret_bare_names
_AGENT_PROVIDER_AUTH_ALLOWLIST = _agent_api.provider_auth_allowlist

_codex_last_message_file = _agent_api.codex_last_message_file
_read_last_message = _agent_api.read_last_message
_build_agent_result = _agent_api.build_agent_result
_codex_command = _agent_api.codex_command
_log_agent_spawn = _agent_api.log_agent_spawn
_run_codex = _agent_api.run_codex
_decode_claude_event = _agent_api.decode_claude_event
_iter_claude_events = _agent_api.iter_claude_events
_collect_claude_text_blocks = _agent_api.collect_claude_text_blocks
_claude_result_text = _agent_api.claude_result_text
_claude_assistant_text = _agent_api.claude_assistant_text
_collect_claude_message_candidates = _agent_api.collect_claude_message_candidates
_claude_last_message = _agent_api.claude_last_message
_claude_command = _agent_api.claude_command
_claude_process_last_message = _agent_api.claude_process_last_message
_run_claude = _agent_api.run_claude

_INTERRUPTED_RETURNCODES = frozenset((-signal.SIGTERM, -signal.SIGKILL))


def _communicate_bounded(
    proc: subprocess.Popen,
    timeout: float,
) -> Optional[tuple[str, str]]:
    """Communicate within a wall-clock cap, returning ``None`` on timeout."""
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return stdout or "", stderr or ""


def _process_group_alive(process_group_id: int) -> bool:
    """Probe whether a process group still contains a live member."""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _sigkill_unless_group_gone(
    proc: subprocess.Popen,
    timeout: float,
) -> None:
    """Wait for the leader, then SIGKILL any surviving process group."""
    leader_exited = True
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        leader_exited = False
    if leader_exited and not _process_group_alive(proc.pid):
        return
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def terminate_all_running(grace: float = 5.0) -> int:
    """SIGTERM every registered group, then SIGKILL deadline stragglers."""
    with _running_procs_lock:
        running_procs = list(_running_procs)
    if not running_procs:
        return 0
    for proc in running_procs:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    for proc in running_procs:
        remaining = max(0, deadline - time.monotonic())
        _sigkill_unless_group_gone(proc, remaining)
    return len(running_procs)


def _run_subprocess(
    command: list[str],
    cwd: Path,
    environ: dict[str, str],
    timeout: int,
) -> _SubprocessResult:
    """Run one agent in a registered, independently killable process group."""
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    with _registered(proc):
        drained = _communicate_bounded(proc, timeout)
        if drained is None:
            _terminate_process_group(proc)
            drained = _communicate_bounded(proc, 10)
            stdout, stderr = ("", "") if drained is None else drained
            return _SubprocessResult(stdout, stderr, -1, True, False)
        stdout, stderr = drained
        interrupted = proc.returncode in _INTERRUPTED_RETURNCODES
        return _SubprocessResult(
            stdout,
            stderr,
            proc.returncode,
            False,
            interrupted,
        )


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM one process group, then SIGKILL it if anything survives."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    _sigkill_unless_group_gone(proc, timeout=5)


def run_agent(
    backend: str,
    prompt: str,
    cwd: Path,
    *,
    options: Optional[AgentRunOptions] = None,
    **option_fields: Unpack[_AgentRunOptionFields],
) -> AgentResult:
    """Dispatch to Codex or Claude with normalized optional controls."""
    run_options = _resolve_agent_run_options(options, option_fields)
    if backend == "codex":
        backend_runner = _run_codex
    elif backend == "claude":
        backend_runner = _run_claude
    else:
        raise ValueError(
            f"unknown agent backend {backend!r}; expected 'codex' or 'claude'",
        )
    return backend_runner(prompt, cwd, options=run_options)
