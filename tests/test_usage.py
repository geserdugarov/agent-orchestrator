# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import unittest

from orchestrator.usage import (
    SkillTriggers,
    UsageMetrics,
    parse_agent_skills,
    parse_agent_usage,
    parse_claude_skills,
    parse_claude_usage,
    parse_codex_skills,
    parse_codex_usage,
)


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(ev) for ev in events)


class ClaudeStreamJsonTest(unittest.TestCase):
    """Synthetic ``claude -p --output-format stream-json`` runs.

    Final assistant frame per ``message.id`` wins (claude streams partial
    usage on intermediate frames); per-model totals roll up into the
    flattened ``UsageMetrics`` shape.
    """

    def test_extracts_tokens_model_and_estimates_cost(self) -> None:
        stdout = _jsonl(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": 5000,
                        "output_tokens": 200,
                    },
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 150,
                        "cache_creation_input_tokens": 1200,
                        "cache_read_input_tokens": 6000,
                        "output_tokens": 300,
                    },
                },
            },
            {"type": "result", "num_turns": 3},
        )
        m = parse_claude_usage(stdout)
        self.assertEqual(m.backend, "claude")
        self.assertEqual(m.models, ("claude-sonnet-4-6",))
        self.assertEqual(m.input_tokens, 150)
        self.assertEqual(m.output_tokens, 300)
        self.assertEqual(m.cache_read_tokens, 6000)
        self.assertEqual(m.cache_write_tokens, 1200)
        self.assertEqual(m.cached_tokens, 0)
        self.assertEqual(m.turns, 3)
        # sonnet rates: input=3, cw5m=3.75, cr=0.30, output=15 (per 1M)
        expected = (
            150 * 3 + 1200 * 3.75 + 6000 * 0.30 + 300 * 15
        ) / 1_000_000
        self.assertEqual(m.cost_source, "estimated")
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_structured_cache_creation_splits_5m_and_1h(self) -> None:
        # The structured form (``cache_creation.ephemeral_*_input_tokens``)
        # bills 5m and 1h cache writes at different rates; the parser must
        # keep them separate rather than collapse both onto the 5m bucket.
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-4-7",
                    "usage": {
                        "input_tokens": 0,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 1000,
                            "ephemeral_1h_input_tokens": 500,
                        },
                        "cache_read_input_tokens": 0,
                        "output_tokens": 100,
                    },
                },
            },
        )
        m = parse_claude_usage(stdout)
        # opus-4-7 rates: input=5, cw5m=6.25, cw1h=10, cr=0.50, output=25
        expected = (
            0 * 5 + 1000 * 6.25 + 500 * 10 + 0 * 0.50 + 100 * 25
        ) / 1_000_000
        self.assertEqual(m.cache_write_tokens, 1500)
        self.assertEqual(m.cost_source, "estimated")
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_reported_total_cost_overrides_estimate(self) -> None:
        # Even when we *could* compute an estimate, the agent's own
        # ``total_cost_usd`` on the result frame is authoritative -- it
        # already accounts for any pricing nuance we may have missed.
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 200,
                    },
                },
            },
            {"type": "result", "total_cost_usd": 0.42, "num_turns": 1},
        )
        m = parse_claude_usage(stdout)
        self.assertEqual(m.cost_source, "reported")
        self.assertEqual(m.cost_usd, 0.42)

    def test_unknown_model_yields_unknown_price(self) -> None:
        # Usage is present but no first-party rates match the SKU; we must
        # report unknown-price rather than guess at zero cost.
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "model": "third-party-model-x",
                    "usage": {"input_tokens": 100, "output_tokens": 200},
                },
            },
        )
        m = parse_claude_usage(stdout)
        self.assertEqual(m.cost_source, "unknown-price")
        self.assertIsNone(m.cost_usd)
        self.assertEqual(m.input_tokens, 100)
        self.assertEqual(m.output_tokens, 200)

    def test_no_usage_events_returns_no_usage(self) -> None:
        stdout = _jsonl(
            {"type": "system", "subtype": "init"},
            {"type": "result", "num_turns": 0},
        )
        m = parse_claude_usage(stdout)
        self.assertEqual(m.cost_source, "no-usage")
        self.assertIsNone(m.cost_usd)
        self.assertEqual(m.input_tokens, 0)
        self.assertEqual(m.output_tokens, 0)
        self.assertEqual(m.models, ())

    def test_malformed_lines_are_skipped(self) -> None:
        # A banner line, a partial flush, and an outright truncated JSON
        # frame must not poison the rest of the stream. Real claude runs
        # do occasionally splice progress text into stdout.
        good = json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        })
        stdout = "\n".join([
            "starting claude...",
            '{"type":"assistant","message"',
            good,
            "",
            "  ",
            "not json either",
        ])
        m = parse_claude_usage(stdout)
        self.assertEqual(m.input_tokens, 10)
        self.assertEqual(m.output_tokens, 20)
        self.assertEqual(m.cost_source, "estimated")

    def test_empty_stdout(self) -> None:
        m = parse_claude_usage("")
        self.assertEqual(m, UsageMetrics(backend="claude"))

    def test_multiple_models_aggregate_when_all_priced(self) -> None:
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_a",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_b",
                    "model": "claude-haiku-3-5",
                    "usage": {"input_tokens": 200, "output_tokens": 100},
                },
            },
        )
        m = parse_claude_usage(stdout)
        self.assertEqual(set(m.models), {"claude-sonnet-4-6", "claude-haiku-3-5"})
        self.assertEqual(m.input_tokens, 300)
        self.assertEqual(m.output_tokens, 150)
        self.assertEqual(m.cost_source, "estimated")
        # sonnet: input=3, output=15; haiku-3-5: input=0.80, output=4
        expected = (
            (100 * 3 + 50 * 15) + (200 * 0.80 + 100 * 4)
        ) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)


class CodexJsonTest(unittest.TestCase):
    """Synthetic ``codex exec --json`` runs.

    Codex emits cumulative usage on each event; the parser takes the
    final non-zero record as the authoritative total rather than summing
    deltas.
    """

    def test_extracts_tokens_model_and_estimates_cost(self) -> None:
        stdout = _jsonl(
            {"type": "task_started", "session_id": "11111111-2222-3333-4444-555555555555"},
            {
                "type": "turn_complete",
                "model": "gpt-5-codex",
                "usage": {
                    "input_tokens": 500,
                    "cached_input_tokens": 100,
                    "output_tokens": 200,
                },
            },
            {
                "type": "turn_complete",
                "model": "gpt-5-codex",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 200,
                    "output_tokens": 400,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.backend, "codex")
        self.assertEqual(m.models, ("gpt-5-codex",))
        # Cumulative: final usage record wins (NOT sum of two events).
        self.assertEqual(m.input_tokens, 1000)
        self.assertEqual(m.cached_tokens, 200)
        self.assertEqual(m.output_tokens, 400)
        self.assertEqual(m.cache_read_tokens, 0)
        self.assertEqual(m.cache_write_tokens, 0)
        # gpt-5-codex rates: input=1.25, cached=0.125, output=10
        uncached = 1000 - 200
        expected = (
            uncached * 1.25 + 200 * 0.125 + 400 * 10
        ) / 1_000_000
        self.assertEqual(m.cost_source, "estimated")
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)
        self.assertEqual(m.turns, 2)

    def test_picks_up_nested_usage_and_num_turns(self) -> None:
        # Codex sometimes nests usage under ``info.total_token_usage`` and
        # publishes ``num_turns`` deep inside a payload object; both must
        # still be reachable via the recursive search.
        stdout = _jsonl(
            {
                "type": "session_summary",
                "payload": {
                    "info": {
                        "model": "gpt-5-mini",
                        "total_token_usage": {
                            "input_tokens": 800,
                            "cached_input_tokens": 0,
                            "output_tokens": 100,
                        },
                        "num_turns": 7,
                    },
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.models, ("gpt-5-mini",))
        self.assertEqual(m.input_tokens, 800)
        self.assertEqual(m.output_tokens, 100)
        self.assertEqual(m.turns, 7)
        # gpt-5-mini rates: input=0.25, cached=0.025, output=2
        expected = (800 * 0.25 + 0 + 100 * 2) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_reported_total_cost_overrides_estimate(self) -> None:
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5-codex",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 0,
                    "output_tokens": 100,
                },
            },
            {"type": "task_complete", "total_cost_usd": 0.07, "num_turns": 1},
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "reported")
        self.assertEqual(m.cost_usd, 0.07)

    def test_unknown_model_yields_unknown_price(self) -> None:
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "made-up-vendor-mini",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 0,
                    "output_tokens": 50,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "unknown-price")
        self.assertIsNone(m.cost_usd)
        self.assertEqual(m.input_tokens, 100)
        self.assertEqual(m.output_tokens, 50)

    def test_fallback_model_used_when_events_omit_one(self) -> None:
        # The CLI sometimes streams usage events without echoing the model
        # name; callers can pass the configured `-m` value as a fallback so
        # an estimate is still possible.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 0,
                    "output_tokens": 50,
                },
            },
        )
        m = parse_codex_usage(stdout, fallback_model="gpt-5-codex")
        self.assertEqual(m.cost_source, "estimated")
        # Models list stays anchored on what the stream actually emitted;
        # the fallback only feeds the price lookup.
        self.assertEqual(m.models, ("gpt-5-codex",))
        assert m.cost_usd is not None
        expected = (100 * 1.25 + 0 + 50 * 10) / 1_000_000
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_cached_tokens_without_cached_rate_blocks_estimate(self) -> None:
        # A model whose published price table has no cached rate cannot be
        # estimated when the run actually used cache reads -- billing those
        # at the input rate would overcharge. Defer to unknown-price.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.5-pro",
                "usage": {
                    "input_tokens": 500,
                    "cached_input_tokens": 100,
                    "output_tokens": 200,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "unknown-price")
        self.assertIsNone(m.cost_usd)

    def test_gpt_5_5_usage_yields_estimated_cost(self) -> None:
        # gpt-5.5 is in the priced family table; usage that names it
        # explicitly must produce an `estimated` cost rather than
        # falling through to `unknown-price`. Pricing-coverage guard:
        # if the row gets accidentally dropped from `_CODEX_RATES`
        # the test fails loudly and the dashboard's
        # `cost_source='unknown-price'` cohort gains a regression
        # before any operator notices.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.5",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 200,
                    "output_tokens": 400,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "estimated")
        self.assertEqual(m.models, ("gpt-5.5",))
        # gpt-5.5 rates: input=5, cached=0.50, output=30 (per 1M)
        uncached = 1000 - 200
        expected = (
            uncached * 5 + 200 * 0.50 + 400 * 30
        ) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_gpt_5_5_reported_cost_wins_over_estimate(self) -> None:
        # Even when usage matches the priced gpt-5.5 family, a CLI-
        # reported `total_cost_usd` on the terminal frame is the
        # authoritative figure (it already accounts for any pricing
        # nuance our table may have missed). Precedence guard so a
        # future change to the priced-model path does not start
        # overriding reported values.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.5",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 0,
                    "output_tokens": 200,
                },
            },
            {"type": "task_complete", "total_cost_usd": 0.99, "num_turns": 1},
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "reported")
        self.assertEqual(m.cost_usd, 0.99)

    def test_gpt_5_5_long_context_session_uses_tiered_pricing(self) -> None:
        # GPT-5.5 prompts whose total input token count exceeds 272K
        # are billed across the whole session at 2x the input rate
        # and 1.5x the output rate (per OpenAI's published long-
        # context pricing). A no-reported-cost Codex run at 300K
        # input must record the elevated estimate, not the flat-rate
        # one. Pinning the threshold here means a future table edit
        # that drops the tier silently regresses the dashboard cost
        # column for long-context sessions before any operator
        # notices the under-reporting.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.5",
                "usage": {
                    "input_tokens": 300_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "estimated")
        # Long-context tier: input * 5 * 2 + output * 30 * 1.5, /1M.
        expected = (
            300_000 * 5 * 2.0 + 1_000 * 30 * 1.5
        ) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_gpt_5_5_at_or_under_threshold_uses_flat_rate(self) -> None:
        # The tier applies strictly when input > threshold; at or
        # under 272K the standard flat rates apply unchanged. This
        # is the boundary regression guard for the new long-context
        # branch.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.5",
                "usage": {
                    "input_tokens": 272_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "estimated")
        # Flat rate: input * 5 + output * 30, /1M (no multipliers).
        expected = (272_000 * 5 + 1_000 * 30) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_gpt_5_5_pro_long_context_stays_flat_priced(self) -> None:
        # OpenAI's official gpt-5.5-pro docs list flat $30 / $180
        # with no >272K multiplier and no cached discount. The tier
        # the standard gpt-5.5 and gpt-5.4-pro entries carry must
        # therefore NOT be inherited by gpt-5.5-pro -- otherwise a
        # no-reported-cost pro run would silently overestimate.
        # Cached tokens stay at 0 here so the estimate path runs at
        # all (gpt-5.5-pro's `cached=None` blocks the estimate when
        # the run carries any cached input -- see
        # test_cached_tokens_without_cached_rate_blocks_estimate).
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.5-pro",
                "usage": {
                    "input_tokens": 300_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "estimated")
        # Flat pro rates: input=30, output=180; NO multipliers.
        expected = (300_000 * 30 + 1_000 * 180) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_gpt_5_4_long_context_session_uses_tiered_pricing(self) -> None:
        # gpt-5.4 carries the same >272K input long-context tier as
        # gpt-5.5 per OpenAI's GPT-5.4 pricing docs: 2x input, 1.5x
        # output. Same regression-guard shape as the gpt-5.5 test --
        # a flat-rate fallback would silently undercount real runs.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.4",
                "usage": {
                    "input_tokens": 300_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "estimated")
        # gpt-5.4 rates: input=2.50, output=15; long-context 2x / 1.5x.
        expected = (
            300_000 * 2.50 * 2.0 + 1_000 * 15 * 1.5
        ) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_gpt_5_4_pro_long_context_session_uses_tiered_pricing(self) -> None:
        # gpt-5.4-pro mirrors gpt-5.5-pro: same threshold + multipliers.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.4-pro",
                "usage": {
                    "input_tokens": 300_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "estimated")
        expected = (
            300_000 * 30 * 2.0 + 1_000 * 180 * 1.5
        ) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_gpt_5_4_mini_and_nano_stay_flat_priced(self) -> None:
        # The long-context tier is documented only for the standard
        # and pro tiers of GPT-5.4 / GPT-5.5. Mini / nano stay on
        # flat pricing; pin the contract so a future copy-paste edit
        # does not over-tier them and silently overcharge.
        for model, rates in (
            ("gpt-5.4-mini", {"input": 0.75, "output": 4.50}),
            ("gpt-5.4-nano", {"input": 0.20, "output": 1.25}),
        ):
            with self.subTest(model=model):
                stdout = _jsonl(
                    {
                        "type": "turn_complete",
                        "model": model,
                        "usage": {
                            "input_tokens": 300_000,
                            "cached_input_tokens": 0,
                            "output_tokens": 1_000,
                        },
                    },
                )
                m = parse_codex_usage(stdout)
                self.assertEqual(m.cost_source, "estimated")
                expected = (
                    300_000 * rates["input"] + 1_000 * rates["output"]
                ) / 1_000_000
                assert m.cost_usd is not None
                self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_gpt_5_2_pro_uses_its_own_rate_not_base(self) -> None:
        # `_codex_rates` is prefix-matched on insertion order, so a
        # missing explicit `gpt-5.2-pro` entry would silently fall
        # through to `gpt-5.2`'s $1.75 / $14 rates and undercount
        # by an order of magnitude. Pin the pro rate so an accidental
        # entry removal or reorder fails loudly here.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.2-pro",
                "usage": {
                    "input_tokens": 100_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "estimated")
        # Per OpenAI's gpt-5.2-pro page: $21 / $168, no cached rate.
        expected = (100_000 * 21 + 1_000 * 168) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_gpt_5_2_pro_cached_tokens_block_estimate(self) -> None:
        # The pro tier publishes no cached-input discount; a run with
        # cached tokens must surface as `unknown-price` rather than
        # bill those tokens at the input rate (overcharge) or the
        # fallthrough sibling's cached rate (undercharge).
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.2-pro",
                "usage": {
                    "input_tokens": 100_000,
                    "cached_input_tokens": 50_000,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "unknown-price")
        self.assertIsNone(m.cost_usd)

    def test_gpt_5_pro_uses_its_own_rate_not_base(self) -> None:
        # Same prefix-fallthrough guard as gpt-5.2-pro: `gpt-5-pro`
        # would otherwise hit the `gpt-5` entry ($1.25 / $10) and
        # undercount by an order of magnitude.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5-pro",
                "usage": {
                    "input_tokens": 100_000,
                    "cached_input_tokens": 0,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "estimated")
        # Per OpenAI's gpt-5-pro page: $15 / $120, no cached rate.
        expected = (100_000 * 15 + 1_000 * 120) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_gpt_5_pro_cached_tokens_block_estimate(self) -> None:
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5-pro",
                "usage": {
                    "input_tokens": 100_000,
                    "cached_input_tokens": 50_000,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "unknown-price")
        self.assertIsNone(m.cost_usd)

    def test_gpt_5_5_long_context_cached_tokens_also_tier_up(self) -> None:
        # Cached input tokens are still input billing -- the long-
        # context multiplier must apply to them too. Otherwise a
        # cache-heavy session over the threshold would silently
        # under-report against OpenAI's actual bill.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "gpt-5.5",
                "usage": {
                    "input_tokens": 300_000,
                    "cached_input_tokens": 100_000,
                    "output_tokens": 1_000,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "estimated")
        uncached = 300_000 - 100_000
        expected = (
            uncached * 5 * 2.0
            + 100_000 * 0.50 * 2.0
            + 1_000 * 30 * 1.5
        ) / 1_000_000
        assert m.cost_usd is not None
        self.assertAlmostEqual(m.cost_usd, expected, places=9)

    def test_truly_unknown_model_remains_unknown_price(self) -> None:
        # The unknown-price exposure contract: a SKU with no priced
        # family at all leaves cost_usd None and cost_source
        # `unknown-price` so the dashboard surfaces a pricing-table
        # gap rather than a silently-wrong zero.
        stdout = _jsonl(
            {
                "type": "turn_complete",
                "model": "third-party-unpriced-model",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 0,
                    "output_tokens": 50,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "unknown-price")
        self.assertIsNone(m.cost_usd)
        self.assertEqual(m.input_tokens, 100)
        self.assertEqual(m.output_tokens, 50)

    def test_no_usage_events(self) -> None:
        stdout = _jsonl(
            {"type": "task_started"},
            {"type": "thought", "text": "thinking"},
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.cost_source, "no-usage")
        self.assertIsNone(m.cost_usd)
        self.assertEqual(m.input_tokens, 0)
        self.assertEqual(m.output_tokens, 0)
        self.assertEqual(m.models, ())
        self.assertIsNone(m.turns)

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps({
            "type": "turn_complete",
            "model": "gpt-5-codex",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 5,
            },
        })
        stdout = "\n".join([
            "codex starting...",
            '{"truncated":',
            "",
            good,
            "trailing-noise",
        ])
        m = parse_codex_usage(stdout)
        self.assertEqual(m.input_tokens, 10)
        self.assertEqual(m.output_tokens, 5)
        self.assertEqual(m.cost_source, "estimated")

    def test_turns_falls_back_to_turn_complete_count(self) -> None:
        # When ``num_turns`` is absent, the count of ``turn_complete``
        # events is the next-best signal of how many turns ran.
        stdout = _jsonl(
            {"type": "task_started"},
            {
                "type": "turn_complete",
                "model": "gpt-5-codex",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "output_tokens": 5,
                },
            },
            {
                "type": "turn_complete",
                "model": "gpt-5-codex",
                "usage": {
                    "input_tokens": 20,
                    "cached_input_tokens": 0,
                    "output_tokens": 10,
                },
            },
        )
        m = parse_codex_usage(stdout)
        self.assertEqual(m.turns, 2)


class DispatcherTest(unittest.TestCase):
    """``parse_agent_usage`` is a thin dispatcher over the per-backend parsers."""

    def test_routes_claude(self) -> None:
        m = parse_agent_usage("claude", "")
        self.assertEqual(m.backend, "claude")

    def test_routes_codex(self) -> None:
        m = parse_agent_usage("codex", "")
        self.assertEqual(m.backend, "codex")

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_agent_usage("gemini", "")


class UsageMetricsTest(unittest.TestCase):
    def test_to_dict_round_trips_via_json(self) -> None:
        m = UsageMetrics(
            backend="codex",
            models=("gpt-5-codex",),
            turns=3,
            input_tokens=100,
            output_tokens=50,
            cached_tokens=10,
            cost_usd=0.01,
            cost_source="estimated",
        )
        encoded = json.dumps(m.to_dict(), sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["backend"], "codex")
        self.assertEqual(decoded["models"], ["gpt-5-codex"])
        self.assertEqual(decoded["turns"], 3)
        self.assertEqual(decoded["cost_source"], "estimated")


class ClaudeSkillTriggerTest(unittest.TestCase):
    """``parse_claude_skills`` over synthetic ``stream-json`` runs.

    Skill invocations surface as ``Skill`` ``tool_use`` blocks inside
    ``assistant`` messages; the parser reads only ``input.skill``, keeps
    first-seen order, de-duplicates names, and counts repeats.
    """

    def test_order_dedup_and_counts(self) -> None:
        stdout = _jsonl(
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "text", "text": "reading the guide"},
                        {"type": "tool_use", "name": "Skill",
                         "input": {"skill": "develop"}},
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_2",
                    "content": [
                        {"type": "tool_use", "name": "Read",
                         "input": {"file_path": "x.py"}},
                        {"type": "tool_use", "name": "Skill",
                         "input": {"skill": "review"}},
                        {"type": "tool_use", "name": "Skill",
                         "input": {"skill": "develop"}},
                    ],
                },
            },
            {"type": "result", "num_turns": 2},
        )
        s = parse_claude_skills(stdout)
        # First-seen order, de-duplicated.
        self.assertEqual(s.triggered, ("develop", "review"))
        # `develop` fired twice (across two messages), `review` once.
        self.assertEqual(s.trigger_counts, {"develop": 2, "review": 1})
        # Offered set stays empty until its stream source is captured.
        self.assertEqual(s.available, ())

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "develop"}},
                ],
            },
        })
        stdout = "\n".join([
            "starting claude...",
            '{"type":"assistant","message"',
            good,
            "",
            "not json either",
        ])
        s = parse_claude_skills(stdout)
        self.assertEqual(s.triggered, ("develop",))
        self.assertEqual(s.trigger_counts, {"develop": 1})

    def test_skill_free_stream_is_empty(self) -> None:
        # Text and non-Skill tool_use blocks must not register as triggers.
        stdout = _jsonl(
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "text", "text": "no skills here"},
                        {"type": "tool_use", "name": "Read",
                         "input": {"file_path": "x.py"}},
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            },
            {"type": "result", "num_turns": 1},
        )
        self.assertEqual(parse_claude_skills(stdout), SkillTriggers())

    def test_malformed_skill_blocks_are_ignored(self) -> None:
        # Missing ``input``, missing/empty ``skill``, and non-dict content
        # entries all skip silently rather than raise.
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "tool_use", "name": "Skill"},
                        {"type": "tool_use", "name": "Skill", "input": {}},
                        {"type": "tool_use", "name": "Skill",
                         "input": {"skill": ""}},
                        "not-a-block",
                        {"type": "tool_use", "name": "Skill",
                         "input": {"skill": "develop"}},
                    ],
                },
            },
        )
        s = parse_claude_skills(stdout)
        self.assertEqual(s.triggered, ("develop",))
        self.assertEqual(s.trigger_counts, {"develop": 1})

    def test_ignores_skill_args_for_privacy(self) -> None:
        # `input.args` can echo issue / user content; only the name is read.
        secret = "user secret: api_key=sk-deadbeef"
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "tool_use", "name": "Skill",
                         "input": {"skill": "develop", "args": secret}},
                    ],
                },
            },
        )
        s = parse_claude_skills(stdout)
        self.assertEqual(s.triggered, ("develop",))
        self.assertEqual(s.trigger_counts, {"develop": 1})
        self.assertNotIn(secret, repr(s))

    def test_empty_stdout(self) -> None:
        self.assertEqual(parse_claude_skills(""), SkillTriggers())


class CodexSkillTriggerTest(unittest.TestCase):
    """``parse_codex_skills`` is best-effort over the ``codex exec --json`` stream.

    The exact codex skill-event shape is an open capture task, so the parser
    attempts two plausible shapes rather than hardcoding empty, and returns an
    empty result -- never an exception -- when neither is present.
    """

    def test_extracts_skill_from_function_call(self) -> None:
        stdout = _jsonl(
            {"type": "task_started"},
            {"type": "item.completed", "item": {
                "type": "function_call", "name": "Skill",
                "arguments": json.dumps({"skill": "review", "args": "..."})}},
            {"type": "item.completed", "item": {
                "type": "function_call", "name": "Skill",
                "arguments": json.dumps({"skill": "review"})}},
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("review",))
        self.assertEqual(s.trigger_counts, {"review": 2})

    def test_extracts_skill_from_dedicated_event_order_and_counts(self) -> None:
        stdout = _jsonl(
            {"type": "skill_invoked", "skill": "develop"},
            {"type": "skill_invoked", "skill": "review"},
            {"type": "skill_invoked", "skill": "develop"},
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("develop", "review"))
        self.assertEqual(s.trigger_counts, {"develop": 2, "review": 1})

    def test_skill_free_usage_stream_is_empty(self) -> None:
        # A normal usage-only run (model + token events) carries no skill
        # marker; the parser must not false-positive on it.
        stdout = _jsonl(
            {"type": "task_started"},
            {"type": "turn_complete", "model": "gpt-5-codex",
             "usage": {"input_tokens": 100, "cached_input_tokens": 0,
                       "output_tokens": 50}},
            {"type": "task_complete", "total_cost_usd": 0.01, "num_turns": 1},
        )
        self.assertEqual(parse_codex_skills(stdout), SkillTriggers())

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps({"type": "skill_invoked", "skill": "develop"})
        stdout = "\n".join([
            "codex starting...",
            '{"truncated":',
            good,
            "trailing-noise",
        ])
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("develop",))
        self.assertEqual(s.trigger_counts, {"develop": 1})

    def test_ignores_skill_args_for_privacy(self) -> None:
        secret = "user secret payload"
        stdout = _jsonl(
            {"type": "item.completed", "item": {
                "type": "function_call", "name": "Skill",
                "arguments": json.dumps({"skill": "review", "args": secret})}},
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("review",))
        self.assertNotIn(secret, repr(s))

    def test_empty_stdout(self) -> None:
        self.assertEqual(parse_codex_skills(""), SkillTriggers())


class SkillDispatcherTest(unittest.TestCase):
    """``parse_agent_skills`` routes by backend, mirroring ``parse_agent_usage``."""

    def test_routes_claude(self) -> None:
        # An assistant/tool_use stream is recognized only by the claude path.
        stdout = _jsonl({
            "type": "assistant",
            "message": {"id": "m", "content": [
                {"type": "tool_use", "name": "Skill",
                 "input": {"skill": "develop"}}]}})
        self.assertEqual(parse_agent_skills("claude", stdout).triggered,
                         ("develop",))

    def test_routes_codex(self) -> None:
        # A dedicated skill event is recognized only by the codex path; the
        # claude parser would return empty on it, so a non-empty result here
        # proves the codex parser ran.
        stdout = _jsonl({"type": "skill_invoked", "skill": "review"})
        self.assertEqual(parse_agent_skills("codex", stdout).triggered,
                         ("review",))
        self.assertEqual(parse_claude_skills(stdout), SkillTriggers())

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_agent_skills("gemini", "")


if __name__ == "__main__":
    unittest.main()
