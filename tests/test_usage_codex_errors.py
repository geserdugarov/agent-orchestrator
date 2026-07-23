# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import json
import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_codex_events as _codex


class CodexUsageErrorTest(unittest.TestCase):
    """Synthetic ``codex exec --json`` runs.

    Codex emits cumulative usage on each event; the parser takes the
    final non-zero record as the authoritative total rather than summing
    deltas.
    """

    def test_unknown_model_keeps_unknown_price(self) -> None:
        # The unknown-price exposure contract: a SKU with no priced
        # family at all leaves cost_usd None and cost_source
        # `unknown-price` so the dashboard surfaces a pricing-table
        # gap rather than a silently-wrong zero.
        stdout = _jsonl.jsonl(
            _codex.turn_complete(
                model="third-party-unpriced-model", input=100, cached=0, output=_usage_cases.TOKEN_COUNT_FIFTY
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.UNKNOWN_COST_SOURCE)
        self.assertIsNone(metrics.cost_usd)
        self.assertEqual(metrics.input_tokens, 100)
        self.assertEqual(metrics.output_tokens, _usage_cases.TOKEN_COUNT_FIFTY)

    def test_no_usage_events(self) -> None:
        stdout = _jsonl.jsonl(
            _codex.task_started(),
            {_usage_cases.TYPE_FIELD: "thought", _usage_cases.TEXT_FIELD: "thinking"},
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "no-usage")
        self.assertIsNone(metrics.cost_usd)
        self.assertEqual(metrics.input_tokens, 0)
        self.assertEqual(metrics.output_tokens, 0)
        self.assertEqual(metrics.models, ())
        self.assertIsNone(metrics.turns)

    def test_malformed_lines_are_skipped(self) -> None:
        good_event = _codex.turn_complete(
            model=_usage_cases.GPT_FIVE_CODEX,
            input=10,
            cached=0,
            output=5,
        )
        good = json.dumps(good_event)
        stdout = "\n".join(
            [
                "codex starting...",
                '{"truncated":',
                "",
                good,
                "trailing-noise",
            ]
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.input_tokens, 10)
        self.assertEqual(metrics.output_tokens, 5)
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)

    def test_turns_falls_back_to_turn_complete_count(self) -> None:
        # When ``num_turns`` is absent, the count of ``turn_complete``
        # events is the next-best signal of how many turns ran.
        stdout = _jsonl.jsonl(
            _codex.task_started(),
            _codex.turn_complete(model=_usage_cases.GPT_FIVE_CODEX, input=10, cached=0, output=5),
            _codex.turn_complete(
                model=_usage_cases.GPT_FIVE_CODEX, input=_usage_cases.TOKEN_COUNT_TWENTY, cached=0, output=10
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.turns, 2)
