# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable agent API over the agent-package owners.

Result and option models live in the ``models`` owner, credential filtering /
injected git identity in the ``environment`` owner, session-id / Claude
final-message parsing in the ``sessions`` owner, the shared process registry /
subprocess-group lifecycle in the ``processes`` owner, and shared dispatch --
backend selection, result assembly, and spawn logging -- in the ``runner``
owner; backend commands remain in focused private leaves. This facade
re-exports the narrow public surface (``__all__``): the model types, the
``run_agent`` dispatch entry, and the ``terminate_all_running`` shutdown hook.
Runners and the verify runner reach the process owner directly.

The retained leaves import the ``models`` / ``environment`` / ``sessions`` /
``processes`` / ``runner`` owners directly at module load; to keep those imports
free of a package-initialization cycle, the backend re-exports this facade owes
``_agent_api`` are resolved lazily by ``__getattr__`` rather than bound at
import time.
"""
from __future__ import annotations

from orchestrator.agents import environment as _agent_environment
from orchestrator.agents import models as _agent_models
from orchestrator.agents import processes as _agent_processes
from orchestrator.agents import runner as _agent_runner
from orchestrator.agents import sessions as _agent_sessions

__all__ = (
    "AgentResult",
    "AgentRunOptions",
    "CodexResult",
    "run_agent",
    "terminate_all_running",
)

terminate_all_running = _agent_processes.terminate_all_running
run_agent = _agent_runner.run_agent

AgentResult = _agent_models.AgentResult
CodexResult = _agent_models.CodexResult
AgentRunOptions = _agent_models.AgentRunOptions
_AgentRunOptionFields = _agent_models.AgentRunOptionFields
_SubprocessResult = _agent_models.SubprocessResult

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
# `run_agent` reads `_run_codex` / `_run_claude` off the facade at dispatch
# time, so those aliases stay a patchable attribute. An immutable tuple avoids
# a mutable module-level constant.
_LAZY_API_EXPORTS = (
    ("_codex_last_message_file", "codex_last_message_file"),
    ("_read_last_message", "read_last_message"),
    ("_codex_command", "codex_command"),
    ("_run_codex", "run_codex"),
    ("_claude_command", "claude_command"),
    ("_claude_process_last_message", "claude_process_last_message"),
    ("_run_claude", "run_claude"),
)


def __getattr__(name: str) -> object:
    """Resolve an `_agent_api`-owned re-export lazily on attribute access."""
    for export_name, api_attr in _LAZY_API_EXPORTS:
        if export_name == name:
            from orchestrator import _agent_api

            return getattr(_agent_api, api_attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
