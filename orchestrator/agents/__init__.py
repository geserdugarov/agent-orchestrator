# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable agent API and hardened subprocess-group lifecycle.

Result and option models live in the ``models`` owner, credential filtering /
injected git identity in the ``environment`` owner, and session-id / Claude
final-message parsing in the ``sessions`` owner; backend commands and shared
runner helpers remain in focused private leaves. Process creation remains here
so the historical ``orchestrator.agents.subprocess.Popen`` patch point and
shared shutdown registry retain their exact behavior.

The retained leaves import the ``models`` / ``environment`` / ``sessions``
owners directly at module load; to keep those imports free of a
package-initialization cycle, the backend / runner re-exports this facade owes
``_agent_api`` are resolved lazily by ``__getattr__`` rather than bound at
import time.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional, Unpack

from orchestrator.agents import environment as _agent_environment
from orchestrator.agents import models as _agent_models
from orchestrator.agents import sessions as _agent_sessions
from orchestrator import _agent_process_registry

_running_procs = _agent_process_registry._running_procs
_running_procs_lock = _agent_process_registry._running_procs_lock
_register_proc = _agent_process_registry.register_proc
_unregister_proc = _agent_process_registry.unregister_proc
_registered = _agent_process_registry.registered

AgentResult = _agent_models.AgentResult
CodexResult = _agent_models.CodexResult
AgentRunOptions = _agent_models.AgentRunOptions
_AgentRunOptionFields = _agent_models.AgentRunOptionFields
_SubprocessResult = _agent_models.SubprocessResult
_resolve_agent_run_options = _agent_models.resolve_agent_run_options

_filter_agent_env = _agent_environment.filter_agent_env
_is_secret_shaped = _agent_environment.is_secret_shaped
_agent_env_key_allowed = _agent_environment._env_key_allowed
_agent_env = _agent_environment.agent_env
_FORBIDDEN_AGENT_ENV = _agent_environment._FORBIDDEN_AGENT_ENV
_AGENT_WRITE_CREDENTIAL_LOCATORS = _agent_environment._AGENT_WRITE_CREDENTIAL_LOCATORS
_AGENT_SECRET_SUFFIXES = _agent_environment._AGENT_SECRET_SUFFIXES
_AGENT_SECRET_BARE_NAMES = _agent_environment._AGENT_SECRET_BARE_NAMES
_AGENT_PROVIDER_AUTH_ALLOWLIST = _agent_environment._AGENT_PROVIDER_AUTH_ALLOWLIST

parse_session_id = _agent_sessions.parse_session_id
_first_nested_uuid = _agent_sessions._first_nested_uuid
_walk_mapping_for_uuid = _agent_sessions._walk_mapping_for_uuid
_walk_for_uuid = _agent_sessions._walk_for_uuid
_UUID_RE = _agent_sessions._UUID_RE
_PRIORITY_KEYS = _agent_sessions._PRIORITY_KEYS
_decode_claude_event = _agent_sessions._decode_claude_event
_iter_claude_events = _agent_sessions._iter_claude_events
_collect_claude_text_blocks = _agent_sessions._collect_claude_text_blocks
_claude_result_text = _agent_sessions._claude_result_text
_claude_assistant_text = _agent_sessions._claude_assistant_text
_collect_claude_message_candidates = _agent_sessions._collect_claude_message_candidates
_claude_last_message = _agent_sessions.claude_last_message

# Facade re-export -> `_agent_api` attribute. Resolved lazily (see
# `__getattr__`) so importing a retained leaf -- which reaches its owners
# through this package -- never re-enters `_agent_api` mid-initialization.
# An immutable tuple avoids a mutable module-level constant.
_LAZY_API_EXPORTS = (
    ("_codex_last_message_file", "codex_last_message_file"),
    ("_read_last_message", "read_last_message"),
    ("_build_agent_result", "build_agent_result"),
    ("_codex_command", "codex_command"),
    ("_log_agent_spawn", "log_agent_spawn"),
    ("_run_codex", "run_codex"),
    ("_claude_command", "claude_command"),
    ("_claude_process_last_message", "claude_process_last_message"),
    ("_run_claude", "run_claude"),
)

_INTERRUPTED_RETURNCODES = frozenset((-signal.SIGTERM, -signal.SIGKILL))


def __getattr__(name: str) -> object:
    """Resolve an `_agent_api`-owned re-export lazily on attribute access."""
    for export_name, api_attr in _LAZY_API_EXPORTS:
        if export_name == name:
            from orchestrator import _agent_api

            return getattr(_agent_api, api_attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    # Reach the backend runner through the facade module so a test that
    # patches `agents._run_codex` / `agents._run_claude` intercepts dispatch;
    # a plain attribute read honors an installed override before falling back
    # to the lazy `__getattr__` resolution.
    from orchestrator import agents
    if backend == "codex":
        backend_runner = agents._run_codex
    elif backend == "claude":
        backend_runner = agents._run_claude
    else:
        raise ValueError(
            f"unknown agent backend {backend!r}; expected 'codex' or 'claude'",
        )
    return backend_runner(prompt, cwd, options=run_options)
