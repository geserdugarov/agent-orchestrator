# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Static compatibility inventory for the agent runtime façade.

Result / option models, credential filtering, and session-id / Claude
final-message parsing have direct owners in the ``agents`` package (``models``
/ ``environment`` / ``sessions``). This inventory aggregates the backend and
shared-runner leaves the façade re-exports.
"""
from __future__ import annotations

from orchestrator import _agent_claude, _agent_codex, _agent_runner_common

codex_last_message_file = _agent_codex.codex_last_message_file
read_last_message = _agent_codex.read_last_message
build_agent_result = _agent_runner_common.build_agent_result
codex_command = _agent_codex.codex_command
log_agent_spawn = _agent_runner_common.log_agent_spawn
run_codex = _agent_codex.run_codex
claude_command = _agent_claude.claude_command
claude_process_last_message = _agent_claude.claude_process_last_message
run_claude = _agent_claude.run_claude
