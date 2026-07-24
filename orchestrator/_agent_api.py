# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Static compatibility inventory for the agent runtime façade.

Result / option models, credential filtering, session-id / Claude
final-message parsing, and shared dispatch have direct owners in the ``agents``
package (``models`` / ``environment`` / ``sessions`` / ``runner``). This
inventory aggregates the codex/claude backend leaves the façade re-exports.
"""
from __future__ import annotations

from orchestrator import _agent_claude, _agent_codex

codex_last_message_file = _agent_codex.codex_last_message_file
read_last_message = _agent_codex.read_last_message
codex_command = _agent_codex.codex_command
run_codex = _agent_codex.run_codex
claude_command = _agent_claude.claude_command
claude_process_last_message = _agent_claude.claude_process_last_message
run_claude = _agent_claude.run_claude
