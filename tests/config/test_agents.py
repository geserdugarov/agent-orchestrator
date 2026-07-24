# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused configuration behavior tests."""

import unittest

from tests import config_reload_helpers as _reload
from tests import config_test_values as _config_cases


class HitlHandleConfigTest(unittest.TestCase):
    def test_formats_comma_handles_as_mentions(self) -> None:
        config = _reload.load_config({"HITL_HANDLE": "alice,bob"})

        self.assertEqual(config.HITL_HANDLES, (_config_cases._ALICE, _config_cases._BOB))
        self.assertEqual(config.HITL_HANDLE, "alice,bob")
        self.assertEqual(config.HITL_MENTIONS, "@alice @bob")

    def test_strips_spaces_at_signs_and_duplicates(self) -> None:
        config = _reload.load_config({"HITL_HANDLE": " @alice, bob, ,alice,@carol "})

        self.assertEqual(config.HITL_HANDLES, (_config_cases._ALICE, _config_cases._BOB, "carol"))
        self.assertEqual(config.HITL_MENTIONS, "@alice @bob @carol")

    def test_empty_config_keeps_existing_default(self) -> None:
        config = _reload.load_config({"HITL_HANDLE": ""})

        self.assertEqual(config.HITL_HANDLES, ("geserdugarov",))
        self.assertEqual(config.HITL_MENTIONS, "@geserdugarov")


class AgentGitIdentityConfigTest(unittest.TestCase):
    def test_defaults_to_orchestrator_identity(self) -> None:
        config = _reload.load_config()

        self.assertEqual(config.AGENT_GIT_NAME, "agent-orchestrator")
        self.assertEqual(
            config.AGENT_GIT_EMAIL,
            "agent-orchestrator@users.noreply.github.com",
        )

    def test_env_overrides_take_effect(self) -> None:
        config = _reload.load_config(
            {
                "AGENT_GIT_NAME": "Custom Bot",
                "AGENT_GIT_EMAIL": "bot@example.com",
            }
        )

        self.assertEqual(config.AGENT_GIT_NAME, "Custom Bot")
        self.assertEqual(config.AGENT_GIT_EMAIL, "bot@example.com")


class AgentBackendSelectionConfigTest(unittest.TestCase):
    """`DEV_AGENT` / `REVIEW_AGENT` are validated at import time so a typo
    aborts the process before the polling loop spins up."""

    def test_defaults_split_claude_dev_codex_review(self) -> None:
        config = _reload.load_config()
        self.assertEqual(config.DEV_AGENT, _config_cases._CLAUDE)
        self.assertEqual(config.REVIEW_AGENT, _config_cases._CODEX)

    def test_env_overrides_invert_split(self) -> None:
        config = _reload.load_config(
            {
                _config_cases._DEV_AGENT_ENV: _config_cases._CODEX,
                _config_cases._REVIEW_AGENT_ENV: _config_cases._CLAUDE,
            }
        )
        self.assertEqual(config.DEV_AGENT, _config_cases._CODEX)
        self.assertEqual(config.REVIEW_AGENT, _config_cases._CLAUDE)

    def test_case_and_whitespace_tolerated(self) -> None:
        config = _reload.load_config(
            {
                _config_cases._DEV_AGENT_ENV: "  CODEX ",
                _config_cases._REVIEW_AGENT_ENV: "Claude",
            }
        )
        self.assertEqual(config.DEV_AGENT, _config_cases._CODEX)
        self.assertEqual(config.REVIEW_AGENT, _config_cases._CLAUDE)

    def test_default_decompose_agent_is_claude(self) -> None:
        config = _reload.load_config()
        self.assertEqual(config.DECOMPOSE_AGENT, _config_cases._CLAUDE)

    def test_decompose_agent_env_override(self) -> None:
        config = _reload.load_config({_config_cases._DECOMPOSE_AGENT_ENV: _config_cases._CODEX})
        self.assertEqual(config.DECOMPOSE_AGENT, _config_cases._CODEX)


class AgentBackendErrorConfigTest(unittest.TestCase):
    """`DEV_AGENT` / `REVIEW_AGENT` are validated at import time so a typo
    aborts the process before the polling loop spins up."""

    def test_invalid_dev_agent_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message({_config_cases._DEV_AGENT_ENV: _config_cases._INVALID_AGENT})
        self.assertIn(_config_cases._DEV_AGENT_ENV, error_message)
        self.assertIn(_config_cases._INVALID_AGENT, error_message)

    def test_invalid_review_agent_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message({_config_cases._REVIEW_AGENT_ENV: "qwen"})
        self.assertIn(_config_cases._REVIEW_AGENT_ENV, error_message)

    def test_invalid_decompose_agent_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message(
            {
                _config_cases._DECOMPOSE_AGENT_ENV: _config_cases._INVALID_AGENT,
            }
        )
        self.assertIn(_config_cases._DECOMPOSE_AGENT_ENV, error_message)

    def test_decomposer_validated_when_feature_off(self) -> None:
        # Toggling DECOMPOSE back on later must not surface a fresh
        # "that env var was always invalid" failure.
        error_message = _reload.config_error_message(
            {
                _config_cases._DECOMPOSE_ENV: _config_cases._OFF,
                _config_cases._DECOMPOSE_AGENT_ENV: _config_cases._INVALID_AGENT,
            }
        )
        self.assertIn(_config_cases._DECOMPOSE_AGENT_ENV, error_message)
