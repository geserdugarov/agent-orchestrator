# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import unittest

from orchestrator import usage as _usage
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_codex_events as _codex


class CodexModelPricingTest(unittest.TestCase):
    """Synthetic ``codex exec --json`` runs.

    Codex emits cumulative usage on each event; the parser takes the
    final non-zero record as the authoritative total rather than summing
    deltas.
    """

    def test_gpt_five_four_tiers_long_context(self) -> None:
        # gpt-5.4 carries the same >272K input long-context tier as
        # gpt-5.5 per OpenAI's GPT-5.4 pricing docs: 2x input, 1.5x
        # output. Same regression-guard shape as the gpt-5.5 test --
        # a flat-rate fallback would silently undercount real runs.
        stdout = _jsonl.jsonl(
            _codex.turn_complete(
                model="gpt-5.4",
                input=_usage_cases.LONG_CONTEXT_INPUT_TOKENS,
                cached=0,
                output=_usage_cases.CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        # gpt-5.4 rates: input=2.50, output=15; long-context 2x / 1.5x.
        expected = (
            _usage_cases.LONG_CONTEXT_INPUT_TOKENS
            * _usage_cases.PRICE_RATE_TWO_AND_HALF
            * _usage_cases.LONG_CONTEXT_INPUT_MULTIPLIER
            + _usage_cases.CODEX_PRICING_OUTPUT_TOKENS
            * _usage_cases.PRICE_RATE_FIFTEEN
            * _usage_cases.LONG_CONTEXT_OUTPUT_MULTIPLIER
        ) / _usage_cases.TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_five_four_pro_uses_tiers(self) -> None:
        # gpt-5.4-pro mirrors gpt-5.5-pro: same threshold + multipliers.
        stdout = _jsonl.jsonl(
            _codex.turn_complete(
                model="gpt-5.4-pro",
                input=_usage_cases.LONG_CONTEXT_INPUT_TOKENS,
                cached=0,
                output=_usage_cases.CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        expected = (
            _usage_cases.LONG_CONTEXT_INPUT_TOKENS
            * _usage_cases.PRICE_RATE_THIRTY
            * _usage_cases.LONG_CONTEXT_INPUT_MULTIPLIER
            + _usage_cases.CODEX_PRICING_OUTPUT_TOKENS
            * _usage_cases.PRICE_RATE_ONE_EIGHTY
            * _usage_cases.LONG_CONTEXT_OUTPUT_MULTIPLIER
        ) / _usage_cases.TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_five_four_small_models_stay_flat(self) -> None:
        # The long-context tier is documented only for the standard
        # and pro tiers of GPT-5.4 / GPT-5.5. Mini / nano stay on
        # flat pricing; pin the contract so a future copy-paste edit
        # does not over-tier them and silently overcharge.
        for model, rates in (
            ("gpt-5.4-mini", {_usage_cases.INPUT_FIELD: 0.75, "output": 4.5}),
            ("gpt-5.4-nano", {_usage_cases.INPUT_FIELD: 0.2, "output": _usage_cases.PRICE_RATE_ONE_AND_QUARTER}),
        ):
            with self.subTest(model=model):
                stdout = _jsonl.jsonl(
                    _codex.turn_complete(
                        model=model,
                        input=_usage_cases.LONG_CONTEXT_INPUT_TOKENS,
                        cached=0,
                        output=_usage_cases.CODEX_PRICING_OUTPUT_TOKENS,
                    ),
                )
                metrics = _usage.parse_codex_usage(stdout)
                self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
                expected = (
                    _usage_cases.LONG_CONTEXT_INPUT_TOKENS * rates[_usage_cases.INPUT_FIELD]
                    + _usage_cases.CODEX_PRICING_OUTPUT_TOKENS * rates["output"]
                ) / _usage_cases.TOKENS_PER_MILLION
                assert metrics.cost_usd is not None
                self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_five_two_pro_uses_own_rate(self) -> None:
        # `_codex_rates` is prefix-matched on insertion order, so a
        # missing explicit `gpt-5.2-pro` entry would silently fall
        # through to `gpt-5.2`'s $1.75 / $14 rates and undercount
        # by an order of magnitude. Pin the pro rate so an accidental
        # entry removal or reorder fails loudly here.
        stdout = _jsonl.jsonl(
            _codex.turn_complete(
                model="gpt-5.2-pro",
                input=_usage_cases.PRO_PRICING_INPUT_TOKENS,
                cached=0,
                output=_usage_cases.CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        # Per OpenAI's gpt-5.2-pro page: $21 / $168, no cached rate.
        expected = (
            _usage_cases.PRO_PRICING_INPUT_TOKENS * _usage_cases.PRICE_RATE_TWENTY_ONE
            + _usage_cases.CODEX_PRICING_OUTPUT_TOKENS * _usage_cases.PRICE_RATE_ONE_SIXTY_EIGHT
        ) / _usage_cases.TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_five_two_pro_cache_blocks_estimate(self) -> None:
        # The pro tier publishes no cached-input discount; a run with
        # cached tokens must surface as `unknown-price` rather than
        # bill those tokens at the input rate (overcharge) or the
        # fallthrough sibling's cached rate (undercharge).
        stdout = _jsonl.jsonl(
            _codex.turn_complete(
                model="gpt-5.2-pro",
                input=_usage_cases.PRO_PRICING_INPUT_TOKENS,
                cached=_usage_cases.PRO_PRICING_CACHED_INPUT_TOKENS,
                output=_usage_cases.CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.UNKNOWN_COST_SOURCE)
        self.assertIsNone(metrics.cost_usd)

    def test_gpt_five_pro_uses_own_rate(self) -> None:
        # Same prefix-fallthrough guard as gpt-5.2-pro: `gpt-5-pro`
        # would otherwise hit the `gpt-5` entry ($1.25 / $10) and
        # undercount by an order of magnitude.
        stdout = _jsonl.jsonl(
            _codex.turn_complete(
                model="gpt-5-pro",
                input=_usage_cases.PRO_PRICING_INPUT_TOKENS,
                cached=0,
                output=_usage_cases.CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        # Per OpenAI's gpt-5-pro page: $15 / $120, no cached rate.
        expected = (
            _usage_cases.PRO_PRICING_INPUT_TOKENS * _usage_cases.PRICE_RATE_FIFTEEN
            + _usage_cases.CODEX_PRICING_OUTPUT_TOKENS * _usage_cases.PRICE_RATE_ONE_TWENTY
        ) / _usage_cases.TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_five_pro_cache_blocks_estimate(self) -> None:
        stdout = _jsonl.jsonl(
            _codex.turn_complete(
                model="gpt-5-pro",
                input=_usage_cases.PRO_PRICING_INPUT_TOKENS,
                cached=_usage_cases.PRO_PRICING_CACHED_INPUT_TOKENS,
                output=_usage_cases.CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = _usage.parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, _usage_cases.UNKNOWN_COST_SOURCE)
        self.assertIsNone(metrics.cost_usd)
