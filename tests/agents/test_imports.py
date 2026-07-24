# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Clean-process import checks for the agents package and its leaves."""

from __future__ import annotations

import subprocess
import sys
import typing
import unittest

from orchestrator import _agent_claude, _agent_codex, _agent_runner_common


_MODULES = (
    "orchestrator.agents",
    "orchestrator.agents.models",
    "orchestrator.agents.environment",
    "orchestrator._agent_api",
    "orchestrator._agent_runner_common",
    "orchestrator._agent_codex",
    "orchestrator._agent_claude",
)

# Retained-leaf functions annotated against the `models` owner. Their hints
# must resolve at runtime, so the owner stays importable at module scope
# rather than only for static type checkers.
_OWNER_ANNOTATED_FUNCS = (
    _agent_codex.codex_command,
    _agent_codex.run_codex,
    _agent_claude.claude_command,
    _agent_claude.claude_process_last_message,
    _agent_claude.run_claude,
    _agent_runner_common.build_agent_result,
    _agent_runner_common.log_agent_spawn,
)


class CleanProcessImportTest(unittest.TestCase):
    """Each agent module imports standalone in a fresh interpreter.

    The package `__init__` facade and the retained `_agent_*` leaves depend
    on each other; importing any leaf before the package must not fail with a
    partially-initialized-module error, so the owners are the only agent-package
    import the leaves take at module load. A subprocess per module gives each
    one a clean `sys.modules` no other test has already populated.
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
    """Retained leaves keep their owner-typed annotations runtime-resolvable.

    The leaves annotate against the `models` owner, and
    `typing.get_type_hints()` -- exercised by tooling and introspection --
    evaluates those annotations in each leaf's globals. The owner names must
    therefore be bound at runtime, not only for static type checkers.
    """

    def test_leaf_function_hints_resolve(self) -> None:
        for owner_annotated in _OWNER_ANNOTATED_FUNCS:
            with self.subTest(function=owner_annotated.__qualname__):
                # An unbound owner name surfaces here as NameError.
                typing.get_type_hints(owner_annotated)
