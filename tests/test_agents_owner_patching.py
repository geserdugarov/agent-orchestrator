# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Backend runners route options and env through the package owners."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import agents as _agents
from orchestrator.agents import environment as _environment
from orchestrator.agents import models as _models
from tests import agent_test_support as _support
from tests import agent_test_values as _agent_cases

# (label, backend runner) pairs so each owner-routing assertion runs against
# both retained leaves without duplicating the body per backend.
_BACKENDS = (
    (_agent_cases._CODEX, _agents._run_codex),
    (_agent_cases._CLAUDE, _agents._run_claude),
)


class RunnerOwnerRoutingTest(unittest.TestCase):
    """Patching the `models` / `environment` owners intercepts both backends.

    The retained Codex / Claude leaves resolve run options through
    `models.resolve_agent_run_options` and the child environment through
    `environment.agent_env` -- the direct owners, not facade-captured
    aliases -- so a monkeypatch on the owner module is observed by the
    runner rather than silently bypassed.
    """

    def test_env_reaches_environment_owner(self) -> None:
        for label, backend_runner in _BACKENDS:
            with self.subTest(backend=label):
                marker_env = {"MARKER_ENV": "owner-sentinel"}
                with (
                    patch.object(
                        _environment,
                        "agent_env",
                        return_value=marker_env,
                    ) as env_owner,
                    patch(
                        _agent_cases._POPEN_TARGET,
                        return_value=_support.completed(),
                    ) as run_mock,
                ):
                    backend_runner(_agent_cases._PROMPT, _agent_cases._CWD)
                    self.assertEqual(env_owner.call_count, 1)
                    self.assertEqual(
                        run_mock.call_args.kwargs[_agent_cases._ENV_KWARG],
                        marker_env,
                    )

    def test_options_reach_models_owner(self) -> None:
        for label, backend_runner in _BACKENDS:
            with self.subTest(backend=label):
                with (
                    patch.object(
                        _models,
                        "resolve_agent_run_options",
                        wraps=_models.resolve_agent_run_options,
                    ) as resolve_owner,
                    patch(
                        _agent_cases._POPEN_TARGET,
                        return_value=_support.completed(),
                    ),
                ):
                    backend_runner(_agent_cases._PROMPT, _agent_cases._CWD)
                    self.assertEqual(resolve_owner.call_count, 1)
