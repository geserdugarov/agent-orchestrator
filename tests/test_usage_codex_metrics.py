# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import unittest

from orchestrator import usage as _usage
from tests import usage_assertions as _assertions
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_codex_events as _codex


class CodexUsageAggregationTest(unittest.TestCase):
    """Synthetic ``codex exec --json`` runs.

    Codex emits cumulative usage on each event; the parser takes the
    final non-zero record as the authoritative total rather than summing
    deltas.
    """

    def test_extracts_tokens_model_and_estimates_cost(self) -> None:
        stdout = _jsonl.jsonl(
            _codex.task_started(session_id="11111111-2222-3333-4444-555555555555"),
            _codex.turn_complete(
                model=_usage_cases.GPT_FIVE_CODEX,
                input=_usage_cases.TOKEN_COUNT_FIVE_HUNDRED,
                cached=100,
                output=_usage_cases.TOKEN_COUNT_TWO_HUNDRED,
            ),
            _codex.turn_complete(
                model=_usage_cases.GPT_FIVE_CODEX,
                input=_usage_cases.CODEX_FINAL_INPUT_TOKENS,
                cached=_usage_cases.CODEX_FINAL_CACHED_TOKENS,
                output=_usage_cases.CODEX_FINAL_OUTPUT_TOKENS,
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        # Cumulative: final usage record wins (NOT sum of two events).
        self.assertEqual(
            (
                metrics.backend,
                metrics.models,
                (
                    metrics.input_tokens,
                    metrics.cached_tokens,
                    metrics.output_tokens,
                ),
                (metrics.cache_read_tokens, metrics.cache_write_tokens),
                metrics.turns,
            ),
            (
                _usage_cases.CODEX,
                (_usage_cases.GPT_FIVE_CODEX,),
                (
                    _usage_cases.CODEX_FINAL_INPUT_TOKENS,
                    _usage_cases.CODEX_FINAL_CACHED_TOKENS,
                    _usage_cases.CODEX_FINAL_OUTPUT_TOKENS,
                ),
                (0, 0),
                2,
            ),
        )
        # gpt-5-codex rates: input=1.25, cached=0.125, output=10
        uncached = _usage_cases.CODEX_FINAL_INPUT_TOKENS - _usage_cases.CODEX_FINAL_CACHED_TOKENS
        expected = (
            uncached * _usage_cases.PRICE_RATE_ONE_AND_QUARTER
            + _usage_cases.CODEX_FINAL_CACHED_TOKENS * _usage_cases.PRICE_RATE_ONE_EIGHTH
            + _usage_cases.CODEX_FINAL_OUTPUT_TOKENS * 10
        ) / _usage_cases.TOKENS_PER_MILLION
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        _assertions.assert_cost(self, metrics, expected, places=9)

    def test_picks_up_nested_usage_and_num_turns(self) -> None:
        # Codex sometimes nests usage under ``info.total_token_usage`` and
        # publishes ``num_turns`` deep inside a payload object; both must
        # still be reachable via the recursive search.
        stdout = _jsonl.jsonl(
            {
                _usage_cases.TYPE_FIELD: "session_summary",
                "payload": {
                    "info": {
                        _usage_cases.MODEL_FIELD: _usage_cases.GPT_FIVE_MINI,
                        "total_token_usage": _codex.usage(
                            input=_usage_cases.TOKEN_COUNT_EIGHT_HUNDRED, cached=0, output=100
                        ),
                        "num_turns": 7,
                    },
                },
            },
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.models, (_usage_cases.GPT_FIVE_MINI,))
        self.assertEqual(metrics.input_tokens, _usage_cases.TOKEN_COUNT_EIGHT_HUNDRED)
        self.assertEqual(metrics.output_tokens, 100)
        self.assertEqual(metrics.turns, 7)
        # gpt-5-mini rates: input=0.25, cached=0.025, output=2
        expected = (
            _usage_cases.TOKEN_COUNT_EIGHT_HUNDRED * _usage_cases.PRICE_RATE_ONE_QUARTER + 100 * 2
        ) / _usage_cases.TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_reported_total_cost_overrides_estimate(self) -> None:
        stdout = _jsonl.jsonl(
            _codex.turn_complete(model=_usage_cases.GPT_FIVE_CODEX, input=1000, cached=0, output=100),
            _codex.task_complete(total_cost_usd=_usage_cases.CODEX_REPORTED_COST_USD, num_turns=1),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "reported")
        self.assertEqual(metrics.cost_usd, _usage_cases.CODEX_REPORTED_COST_USD)

    def test_unknown_model_yields_unknown_price(self) -> None:
        stdout = _jsonl.jsonl(
            _codex.turn_complete(
                model="made-up-vendor-mini", input=100, cached=0, output=_usage_cases.TOKEN_COUNT_FIFTY
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.UNKNOWN_COST_SOURCE)
        self.assertIsNone(metrics.cost_usd)
        self.assertEqual(metrics.input_tokens, 100)
        self.assertEqual(metrics.output_tokens, _usage_cases.TOKEN_COUNT_FIFTY)

    def test_fallback_model_used_when_events_omit_one(self) -> None:
        # The CLI sometimes streams usage events without echoing the model
        # name; callers can pass the configured `-m` value as a fallback so
        # an estimate is still possible.
        stdout = _jsonl.jsonl(
            _codex.turn_complete(input=100, cached=0, output=_usage_cases.TOKEN_COUNT_FIFTY),
        )
        metrics = _usage.parse_codex_usage(stdout, fallback_model=_usage_cases.GPT_FIVE_CODEX)
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        # Models list stays anchored on what the stream actually emitted;
        # the fallback only feeds the price lookup.
        self.assertEqual(metrics.models, (_usage_cases.GPT_FIVE_CODEX,))
        assert metrics.cost_usd is not None
        expected = (
            100 * _usage_cases.PRICE_RATE_ONE_AND_QUARTER + _usage_cases.TOKEN_COUNT_FIFTY * 10
        ) / _usage_cases.TOKENS_PER_MILLION
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_no_cached_rate_blocks_cost_estimate(self) -> None:
        # A model whose published price table has no cached rate cannot be
        # estimated when the run actually used cache reads -- billing those
        # at the input rate would overcharge. Defer to unknown-price.
        stdout = _jsonl.jsonl(
            _codex.turn_complete(
                model="gpt-5.5-pro",
                input=_usage_cases.TOKEN_COUNT_FIVE_HUNDRED,
                cached=100,
                output=_usage_cases.TOKEN_COUNT_TWO_HUNDRED,
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.UNKNOWN_COST_SOURCE)
        self.assertIsNone(metrics.cost_usd)
