# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import json
import unittest

from orchestrator import usage as _usage
from tests import usage_assertions as _assertions
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_claude_events as _claude


class ClaudeUsageAggregationTest(unittest.TestCase):
    """Synthetic ``claude -p --output-format stream-json`` runs.

    Final assistant frame per ``message.id`` wins (claude streams partial
    usage on intermediate frames); per-model totals roll up into the
    flattened ``_usage.UsageMetrics`` shape.
    """

    def test_extracts_tokens_model_and_estimates_cost(self) -> None:
        stdout = _jsonl.jsonl(
            _claude.system_init(session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            _claude.assistant(
                model=_usage_cases.SONNET,
                usage=_claude.usage(
                    input=100,
                    cache_write=1000,
                    cache_read=_usage_cases.TOKEN_COUNT_FIVE_THOUSAND,
                    output=_usage_cases.TOKEN_COUNT_TWO_HUNDRED,
                ),
            ),
            _claude.assistant(
                model=_usage_cases.SONNET,
                usage=_claude.usage(
                    input=_usage_cases.CLAUDE_FINAL_INPUT_TOKENS,
                    cache_write=_usage_cases.CLAUDE_FINAL_CACHE_WRITE_TOKENS,
                    cache_read=_usage_cases.CLAUDE_FINAL_CACHE_READ_TOKENS,
                    output=_usage_cases.CLAUDE_FINAL_OUTPUT_TOKENS,
                ),
            ),
            _claude.terminal_result(num_turns=3),
        )
        metrics = _usage.parse_claude_usage(stdout)
        self.assertEqual(
            (
                metrics.backend,
                metrics.models,
                (
                    metrics.input_tokens,
                    metrics.output_tokens,
                    metrics.cache_read_tokens,
                    metrics.cache_write_tokens,
                ),
                metrics.cached_tokens,
                metrics.turns,
            ),
            (
                _usage_cases.CLAUDE,
                (_usage_cases.SONNET,),
                (
                    _usage_cases.CLAUDE_FINAL_INPUT_TOKENS,
                    _usage_cases.CLAUDE_FINAL_OUTPUT_TOKENS,
                    _usage_cases.CLAUDE_FINAL_CACHE_READ_TOKENS,
                    _usage_cases.CLAUDE_FINAL_CACHE_WRITE_TOKENS,
                ),
                0,
                3,
            ),
        )
        # sonnet rates: input=3, cw5m=3.75, cr=0.30, output=15 (per 1M)
        expected = (
            _usage_cases.CLAUDE_FINAL_INPUT_TOKENS * 3
            + _usage_cases.CLAUDE_FINAL_CACHE_WRITE_TOKENS * _usage_cases.PRICE_RATE_THREE_AND_THREE_QUARTERS
            + _usage_cases.CLAUDE_FINAL_CACHE_READ_TOKENS * _usage_cases.PRICE_RATE_THREE_TENTHS
            + _usage_cases.CLAUDE_FINAL_OUTPUT_TOKENS * _usage_cases.PRICE_RATE_FIFTEEN
        ) / _usage_cases.TOKENS_PER_MILLION
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        _assertions.assert_cost(self, metrics, expected, places=9)

    def test_cache_creation_keeps_ttl_buckets(self) -> None:
        # The structured form (``cache_creation.ephemeral_*_input_tokens``)
        # bills 5m and 1h cache writes at different rates; the parser must
        # keep them separate rather than collapse both onto the 5m bucket.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                model=_usage_cases.OPUS_FOUR_SEVEN,
                usage=_claude.usage(
                    input=0,
                    cache_five_minute=_usage_cases.CLAUDE_FIVE_MINUTE_CACHE_TOKENS,
                    cache_one_hour=_usage_cases.CLAUDE_ONE_HOUR_CACHE_TOKENS,
                    output=100,
                ),
            ),
        )
        metrics = _usage.parse_claude_usage(stdout)
        # opus-4-7 rates: input=5, cw5m=6.25, cw1h=10, cr=0.50, output=25
        expected = (
            _usage_cases.CLAUDE_FIVE_MINUTE_CACHE_TOKENS * _usage_cases.PRICE_RATE_SIX_AND_QUARTER
            + _usage_cases.CLAUDE_ONE_HOUR_CACHE_TOKENS * 10
            + 100 * _usage_cases.PRICE_RATE_TWENTY_FIVE
        ) / _usage_cases.TOKENS_PER_MILLION
        self.assertEqual(
            metrics.cache_write_tokens,
            _usage_cases.CLAUDE_COMBINED_CACHE_WRITE_TOKENS,
        )
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_reported_total_cost_overrides_estimate(self) -> None:
        # Even when we *could* compute an estimate, the agent's own
        # ``total_cost_usd`` on the result frame is authoritative -- it
        # already accounts for any pricing nuance we may have missed.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                model=_usage_cases.SONNET, usage=_claude.usage(input=100, output=_usage_cases.TOKEN_COUNT_TWO_HUNDRED)
            ),
            _claude.terminal_result(
                total_cost_usd=_usage_cases.CLAUDE_REPORTED_COST_USD,
                num_turns=1,
            ),
        )
        metrics = _usage.parse_claude_usage(stdout)
        self.assertEqual(metrics.cost_source, "reported")
        self.assertEqual(metrics.cost_usd, _usage_cases.CLAUDE_REPORTED_COST_USD)

    def test_multiple_models_sum_when_all_priced(self) -> None:
        stdout = _jsonl.jsonl(
            _claude.assistant(
                id="msg_a",
                model=_usage_cases.SONNET,
                usage=_claude.usage(input=100, output=_usage_cases.TOKEN_COUNT_FIFTY),
            ),
            _claude.assistant(
                id="msg_b",
                model=_usage_cases.HAIKU,
                usage=_claude.usage(input=_usage_cases.TOKEN_COUNT_TWO_HUNDRED, output=100),
            ),
        )
        metrics = _usage.parse_claude_usage(stdout)
        self.assertEqual(set(metrics.models), {_usage_cases.SONNET, _usage_cases.HAIKU})
        self.assertEqual(metrics.input_tokens, _usage_cases.TOKEN_COUNT_THREE_HUNDRED)
        self.assertEqual(metrics.output_tokens, _usage_cases.COMBINED_OUTPUT_TOKENS)
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        # sonnet: input=3, output=15; haiku-3-5: input=0.80, output=4
        sonnet_cost = 100 * 3 + _usage_cases.TOKEN_COUNT_FIFTY * _usage_cases.PRICE_RATE_FIFTEEN
        haiku_cost = _usage_cases.TOKEN_COUNT_TWO_HUNDRED * _usage_cases.PRICE_RATE_FOUR_FIFTHS + 100 * 4
        expected = (sonnet_cost + haiku_cost) / _usage_cases.TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)


class ClaudeUsageErrorTest(unittest.TestCase):
    """Synthetic ``claude -p --output-format stream-json`` runs.

    Final assistant frame per ``message.id`` wins (claude streams partial
    usage on intermediate frames); per-model totals roll up into the
    flattened ``_usage.UsageMetrics`` shape.
    """

    def test_unknown_model_yields_unknown_price(self) -> None:
        # Usage is present but no first-party rates match the SKU; we must
        # report unknown-price rather than guess at zero cost.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                model=_usage_cases.UNKNOWN_MODEL,
                usage=_claude.usage(input=100, output=_usage_cases.TOKEN_COUNT_TWO_HUNDRED),
            ),
        )
        metrics = _usage.parse_claude_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.UNKNOWN_COST_SOURCE)
        self.assertIsNone(metrics.cost_usd)
        self.assertEqual(metrics.input_tokens, 100)
        self.assertEqual(metrics.output_tokens, _usage_cases.TOKEN_COUNT_TWO_HUNDRED)

    def test_no_usage_events_returns_no_usage(self) -> None:
        stdout = _jsonl.jsonl(
            _claude.system_init(),
            _claude.terminal_result(num_turns=0),
        )
        metrics = _usage.parse_claude_usage(stdout)
        self.assertEqual(metrics.cost_source, "no-usage")
        self.assertIsNone(metrics.cost_usd)
        self.assertEqual(metrics.input_tokens, 0)
        self.assertEqual(metrics.output_tokens, 0)
        self.assertEqual(metrics.models, ())

    def test_malformed_lines_are_skipped(self) -> None:
        # A banner line, a partial flush, and an outright truncated JSON
        # frame must not poison the rest of the stream. Real claude runs
        # do occasionally splice progress text into stdout.
        good = json.dumps(
            _claude.assistant(
                model=_usage_cases.SONNET, usage=_claude.usage(input=10, output=_usage_cases.TOKEN_COUNT_TWENTY)
            )
        )
        stdout = "\n".join(
            [
                "starting claude...",
                '{"type":"assistant","message"',
                good,
                "",
                "  ",
                "not json either",
            ]
        )
        metrics = _usage.parse_claude_usage(stdout)
        self.assertEqual(metrics.input_tokens, 10)
        self.assertEqual(metrics.output_tokens, _usage_cases.TOKEN_COUNT_TWENTY)
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)

    def test_empty_stdout(self) -> None:
        metrics = _usage.parse_claude_usage("")
        self.assertEqual(metrics, _usage.UsageMetrics(backend=_usage_cases.CLAUDE))
