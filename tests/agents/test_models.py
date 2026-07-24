# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Agent result and run-option model tests."""

from __future__ import annotations

import unittest

from orchestrator.agents import models as _models


class AgentResultTest(unittest.TestCase):
    def test_interrupted_defaults_false(self) -> None:
        # `interrupted` is optional at construction, so a result built
        # without it reads as a clean, non-interrupted run.
        agent_result = _models.AgentResult(
            session_id=None,
            last_message="",
            exit_code=0,
            timed_out=False,
            stdout="",
            stderr="",
        )
        self.assertFalse(agent_result.interrupted)
