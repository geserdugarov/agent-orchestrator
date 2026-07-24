# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable agent API over the agent-package owners.

Result and option models live in the ``models`` owner, credential filtering /
injected git identity in the ``environment`` owner, session-id / Claude
final-message parsing in the ``sessions`` owner, the shared process registry /
subprocess-group lifecycle in the ``processes`` owner, and shared dispatch --
backend selection, result assembly, and spawn logging -- in the ``runner``
owner; per-backend command construction and execution live in the
``agents.backends`` subpackage (``codex`` and ``claude``). This facade
re-exports the narrow public surface (``__all__``): the model types, the
``run_agent`` dispatch entry, and the ``terminate_all_running`` shutdown hook.

``run_agent`` reaches the backend owner modules directly at dispatch time, and
runners plus the verify runner reach the process / environment owners directly,
so this facade carries no private backend or owner re-exports.
"""
from __future__ import annotations

from orchestrator.agents import models as _agent_models
from orchestrator.agents import processes as _agent_processes
from orchestrator.agents import runner as _agent_runner

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
