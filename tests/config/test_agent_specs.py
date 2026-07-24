# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused configuration behavior tests."""

import unittest

from tests import config_reload_helpers as _reload
from tests import config_test_values as _config_cases


class AgentSpecParsingConfigTest(unittest.TestCase):
    """`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` accept shell-like
    command specs: a backend name optionally followed by backend-CLI args
    (`codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`). Bare backend
    names keep working unchanged.
    """

    def test_bare_backend_has_no_extra_args(self) -> None:
        config = _reload.load_config()
        self.assertEqual(config.DEV_AGENT, _config_cases._CLAUDE)
        self.assertEqual(config.DEV_AGENT_ARGS, ())
        self.assertEqual(config.REVIEW_AGENT, _config_cases._CODEX)
        self.assertEqual(config.REVIEW_AGENT_ARGS, ())
        self.assertEqual(config.DECOMPOSE_AGENT, _config_cases._CLAUDE)
        self.assertEqual(config.DECOMPOSE_AGENT_ARGS, ())

    def test_parses_quoted_codex_spec(self) -> None:
        # Exact spec shape from the issue body. shlex must keep the
        # `-c key="value"` token whole even though it contains both
        # quotes and an `=`; if the parser splits on whitespace naively
        # the value half would be dropped.
        config = _reload.load_config(
            {
                _config_cases._DEV_AGENT_ENV: ("codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"),
            }
        )
        self.assertEqual(config.DEV_AGENT, _config_cases._CODEX)
        self.assertEqual(
            config.DEV_AGENT_ARGS,
            (_config_cases._MODEL_FLAG, "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"'),
        )

    def test_parses_claude_spec_with_flags(self) -> None:
        config = _reload.load_config(
            {
                _config_cases._REVIEW_AGENT_ENV: "claude --model claude-opus-4-7 --effort high",
            }
        )
        self.assertEqual(config.REVIEW_AGENT, _config_cases._CLAUDE)
        self.assertEqual(
            config.REVIEW_AGENT_ARGS,
            ("--model", "claude-opus-4-7", "--effort", "high"),
        )

    def test_per_role_args_are_independent(self) -> None:
        # Two roles sharing a backend keep distinct args so a deployment
        # can run e.g. `codex -m gpt-5.5` for dev and `codex` for review.
        config = _reload.load_config(
            {
                _config_cases._DEV_AGENT_ENV: "codex -m gpt-5.5",
                _config_cases._REVIEW_AGENT_ENV: _config_cases._CODEX,
                _config_cases._DECOMPOSE_AGENT_ENV: "claude --model claude-opus-4-7",
            }
        )
        self.assertEqual(config.DEV_AGENT_ARGS, (_config_cases._MODEL_FLAG, "gpt-5.5"))
        self.assertEqual(config.REVIEW_AGENT_ARGS, ())
        self.assertEqual(
            config.DECOMPOSE_AGENT_ARGS,
            ("--model", "claude-opus-4-7"),
        )

    def test_first_token_case_normalized(self) -> None:
        # The bare-form parser tolerates ` CODEX `; the spec form should
        # behave identically so legacy values like `DEV_AGENT=Codex` keep
        # parsing the same way after the shell-spec rollout.
        config = _reload.load_config({_config_cases._DEV_AGENT_ENV: "  CODEX -m foo"})
        self.assertEqual(config.DEV_AGENT, _config_cases._CODEX)
        self.assertEqual(config.DEV_AGENT_ARGS, (_config_cases._MODEL_FLAG, "foo"))


class AgentSpecErrorConfigTest(unittest.TestCase):
    """`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` accept shell-like
    command specs: a backend name optionally followed by backend-CLI args
    (`codex -m gpt-5.5 -c 'model_reasoning_effort="xhigh"'`). Bare backend
    names keep working unchanged.
    """

    def test_empty_spec_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message({_config_cases._DEV_AGENT_ENV: "   "})
        self.assertIn(_config_cases._DEV_AGENT_ENV, error_message)
        self.assertIn("empty", error_message)

    def test_unknown_first_token_aborts_at_import(self) -> None:
        error_message = _reload.config_error_message(
            {
                _config_cases._DEV_AGENT_ENV: "gemini --model g-1",
            }
        )
        self.assertIn(_config_cases._DEV_AGENT_ENV, error_message)
        self.assertIn(_config_cases._INVALID_AGENT, error_message)

    def test_unterminated_quote_aborts_at_import(self) -> None:
        # shlex.split raises ValueError on an unbalanced quote; the
        # importer must surface that as a SystemExit so the orchestrator
        # never starts with an unparseable spec.
        error_message = _reload.config_error_message(
            {
                _config_cases._DEV_AGENT_ENV: "codex -c 'unterminated",
            }
        )
        self.assertIn(_config_cases._DEV_AGENT_ENV, error_message)
