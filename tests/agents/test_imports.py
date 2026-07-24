# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Clean-process import checks for the agents package and its leaves."""

from __future__ import annotations

import subprocess
import sys
import typing
import unittest

from orchestrator import _agent_claude
from orchestrator import agents as _agents
from orchestrator.agents import models as _agent_models
from orchestrator.agents import processes as _agent_processes
from orchestrator.agents import runner as _agent_runner
from orchestrator.agents.backends import codex as _agent_codex


_MODULES = (
    "orchestrator.agents",
    "orchestrator.agents.models",
    "orchestrator.agents.environment",
    "orchestrator.agents.sessions",
    "orchestrator.agents.processes",
    "orchestrator.agents.runner",
    "orchestrator.agents.backends",
    "orchestrator.agents.backends.codex",
    "orchestrator._agent_api",
    "orchestrator._agent_claude",
)

# Agent-package functions annotated against the `models` owner -- the runner
# owner plus the Codex backend and retained Claude leaf. Their hints must
# resolve at runtime, so the owner stays importable at module scope rather than
# only for static type checkers.
_OWNER_ANNOTATED_FUNCS = (
    _agent_codex.codex_command,
    _agent_codex.run_codex,
    _agent_claude.claude_command,
    _agent_claude.claude_process_last_message,
    _agent_claude.run_claude,
    _agent_runner.resolve_agent_run_options,
    _agent_runner.run_agent,
    _agent_runner.build_agent_result,
    _agent_runner.log_agent_spawn,
)


class CleanProcessImportTest(unittest.TestCase):
    """Each agent module imports standalone in a fresh interpreter.

    The package `__init__` facade, the `agents.backends.codex` backend, and
    the retained `_agent_*` leaves depend on each other; importing any of them
    before the package must not fail with a partially-initialized-module error,
    so the owners are the only agent-package import the backend and leaves take
    at module load. A subprocess per module gives each one a clean `sys.modules`
    no other test has already populated.
    """

    def test_each_module_imports_standalone(self) -> None:
        for module in _MODULES:
            with self.subTest(module=module):
                completed = subprocess.run(
                    [sys.executable, "-c", f"import {module}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, msg=completed.stderr)


class RuntimeAnnotationTest(unittest.TestCase):
    """Owner-typed annotations stay runtime-resolvable across the package.

    The runner owner, the Codex backend, and the Claude leaf annotate against
    the `models` owner, and `typing.get_type_hints()` -- exercised by tooling and
    introspection -- evaluates those annotations in each function's globals.
    The owner names must therefore be bound at runtime, not only for static
    type checkers.
    """

    def test_leaf_function_hints_resolve(self) -> None:
        for owner_annotated in _OWNER_ANNOTATED_FUNCS:
            with self.subTest(function=owner_annotated.__qualname__):
                # An unbound owner name surfaces here as NameError.
                typing.get_type_hints(owner_annotated)


class PublicSurfaceTest(unittest.TestCase):
    """The facade publishes a narrow `__all__` backed by owner identities."""

    def test_all_names_the_narrow_public_surface(self) -> None:
        self.assertEqual(
            _agents.__all__,
            (
                "AgentResult",
                "AgentRunOptions",
                "CodexResult",
                "run_agent",
                "terminate_all_running",
            ),
        )

    def test_public_names_are_owner_re_exports(self) -> None:
        # Each public name resolves to the owning module's object rather than a
        # copy, so a caller reaching through the facade sees the owner's
        # definition and a monkeypatch on it stays observable.
        self.assertIs(_agents.run_agent, _agent_runner.run_agent)
        self.assertIs(_agents.AgentResult, _agent_models.AgentResult)
        self.assertIs(_agents.AgentRunOptions, _agent_models.AgentRunOptions)
        self.assertIs(_agents.CodexResult, _agent_models.CodexResult)
        self.assertIs(
            _agents.terminate_all_running,
            _agent_processes.terminate_all_running,
        )
