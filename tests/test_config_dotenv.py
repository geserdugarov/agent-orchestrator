# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused configuration behavior tests."""

import unittest

from tests import config_test_support as _support
from tests import config_test_values as _config_cases


class DotenvQuoteStrippingTest(unittest.TestCase):
    """Ensure dotenv parsing removes only one matched outer quote pair."""

    def test_keeps_inner_quote_pairs(self) -> None:
        from orchestrator.config import _strip_dotenv_quotes

        # Inner double quotes and the command's trailing single quote are
        # payload, rather than an outer pair around the entire value.
        raw = "codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'"
        self.assertEqual(_strip_dotenv_quotes(raw), raw)

    def test_unwraps_matched_outer_pair(self) -> None:
        from orchestrator.config import _strip_dotenv_quotes

        # Operator-written `KEY="value with spaces"` -- a single matched
        # outer pair IS unwrapped so existing dotenv conventions keep
        # working.
        self.assertEqual(
            _strip_dotenv_quotes('"value with spaces"'),
            "value with spaces",
        )
        self.assertEqual(
            _strip_dotenv_quotes("'single quoted'"),
            "single quoted",
        )

    def test_keeps_mismatched_outer_pair(self) -> None:
        from orchestrator.config import _strip_dotenv_quotes

        # A `"...'` mismatch is more likely a typo than a quoting
        # convention; leaving it intact surfaces the problem at the
        # downstream parser instead of silently corrupting the value.
        self.assertEqual(_strip_dotenv_quotes("\"mismatched'"), "\"mismatched'")

    def test_quoted_codex_spec_round_trips(self) -> None:
        # The exact spec shape advertised in .env.example.advanced and
        # the issue body must parse cleanly when supplied through .env,
        # not just when injected directly into os.environ.
        body = "DEV_AGENT=codex -m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"'\n"
        config = _support.load_config_from_dotenv(body)
        self.assertEqual(config.DEV_AGENT, _config_cases._CODEX)
        self.assertEqual(
            config.DEV_AGENT_ARGS,
            (_config_cases._MODEL_FLAG, "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"'),
        )

    def test_outer_double_quoted_value_unwraps(self) -> None:
        # Backward-compat for operators who wrap their values in outer
        # double quotes (a common dotenv convention).
        body = 'REVIEW_AGENT="claude --model claude-opus-4-7"\n'
        config = _support.load_config_from_dotenv(body)
        self.assertEqual(config.REVIEW_AGENT, _config_cases._CLAUDE)
        self.assertEqual(
            config.REVIEW_AGENT_ARGS,
            ("--model", "claude-opus-4-7"),
        )
