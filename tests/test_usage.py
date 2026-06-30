# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import unittest

from orchestrator.usage import (
    AgentTrajectory,
    SkillTriggers,
    TrajectoryStep,
    UsageMetrics,
    parse_agent_skills,
    parse_agent_trajectory,
    parse_agent_usage,
    parse_claude_skills,
    parse_claude_trajectory,
    parse_claude_usage,
    parse_codex_skills,
    parse_codex_trajectory,
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
    first-seen order, de-duplicates per-invocation by the block ``id``, and
    counts repeats. The offered set comes from the ``system``/``init``
    frame's ``skills`` array. Fixtures mirror the real captured shape: under
    ``--include-partial-messages`` the content array is partitioned one
    completed block per ``assistant`` frame (not a cumulative snapshot), so a
    ``tool_use`` block appears in exactly one frame and carries a unique id.
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
        # This `init` frame carries no `skills` array, so the offered set is
        # empty (the `available` source is read from `system/init.skills`
        # when present -- see `test_available_from_init_skills`).
        self.assertEqual(s.available, ())

    def test_partitioned_content_frames_keep_skill(self) -> None:
        # The real capture: `--include-partial-messages` emits one `assistant`
        # frame per completed content block, all sharing the message id. The
        # content array is partitioned across them -- a text block in its own
        # frame, then the `Skill` block in the next -- NOT a cumulative
        # snapshot. The old last-frame-wins logic would drop the trigger here
        # because the trailing frame's content has no skill; walking every
        # frame keeps it.
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "tool_use", "name": "Skill",
                         "id": "toolu_a",
                         "input": {"skill": "develop"}},
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "text", "text": "now I'll start"},
                    ],
                },
            },
            {"type": "result", "num_turns": 1},
        )
        s = parse_claude_skills(stdout)
        self.assertEqual(s.triggered, ("develop",))
        self.assertEqual(s.trigger_counts, {"develop": 1})

    def test_repeated_tool_use_id_counted_once(self) -> None:
        # Defensive: should a future stream repeat one block across frames
        # (the way the `usage` sub-object repeats), the shared `tool_use` id
        # de-dups it so a single invocation still counts once.
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "tool_use", "name": "Skill",
                         "id": "toolu_a",
                         "input": {"skill": "develop"}},
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "tool_use", "name": "Skill",
                         "id": "toolu_a",
                         "input": {"skill": "develop"}},
                        {"type": "tool_use", "name": "Skill",
                         "id": "toolu_b",
                         "input": {"skill": "review"}},
                    ],
                },
            },
            {"type": "result", "num_turns": 1},
        )
        s = parse_claude_skills(stdout)
        self.assertEqual(s.triggered, ("develop", "review"))
        self.assertEqual(s.trigger_counts, {"develop": 1, "review": 1})

    def test_distinct_ids_count_repeats(self) -> None:
        # Two genuine `develop` invocations carry distinct ids -> count 2.
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "tool_use", "name": "Skill",
                         "id": "toolu_a",
                         "input": {"skill": "develop"}},
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_2",
                    "content": [
                        {"type": "tool_use", "name": "Skill",
                         "id": "toolu_b",
                         "input": {"skill": "develop"}},
                    ],
                },
            },
        )
        s = parse_claude_skills(stdout)
        self.assertEqual(s.triggered, ("develop",))
        self.assertEqual(s.trigger_counts, {"develop": 2})

    def test_available_from_init_skills(self) -> None:
        # The offered set is read from the `system`/`init` frame's dedicated
        # `skills` array (confirmed against a real claude 2.1.x capture), and
        # is independent of what the run triggered: here `review` is offered
        # but never fired, while `develop` is both offered and triggered.
        stdout = _jsonl(
            {"type": "system", "subtype": "init",
             "skills": ["develop", "review", "verify"]},
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "tool_use", "name": "Skill",
                         "id": "toolu_a",
                         "input": {"skill": "develop"}},
                    ],
                },
            },
            {"type": "result", "num_turns": 1},
        )
        s = parse_claude_skills(stdout)
        self.assertEqual(s.available, ("develop", "review", "verify"))
        self.assertEqual(s.triggered, ("develop",))
        self.assertEqual(s.trigger_counts, {"develop": 1})

    def test_available_present_without_any_trigger(self) -> None:
        # Offered-but-not-triggered: `available` populated, `triggered` empty.
        stdout = _jsonl(
            {"type": "system", "subtype": "init",
             "skills": ["develop", "review"]},
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [{"type": "text", "text": "no skill used"}],
                },
            },
            {"type": "result", "num_turns": 1},
        )
        s = parse_claude_skills(stdout)
        self.assertEqual(s.available, ("develop", "review"))
        self.assertEqual(s.triggered, ())
        self.assertEqual(s.trigger_counts, {})

    def test_available_dedups_and_filters_non_strings(self) -> None:
        # Non-string entries filter out; duplicates collapse, first-seen order.
        stdout = _jsonl(
            {"type": "system", "subtype": "init",
             "skills": ["develop", "review", "develop", 42, None, "", "verify"]},
        )
        s = parse_claude_skills(stdout)
        self.assertEqual(s.available, ("develop", "review", "verify"))

    def test_available_empty_without_init_skills(self) -> None:
        # An init frame with no `skills` key, a non-list `skills`, and a
        # stream with no init frame at all all yield an empty offered set,
        # never an exception.
        for frame in (
            {"type": "system", "subtype": "init"},
            {"type": "system", "subtype": "init", "skills": "develop"},
            {"type": "system", "subtype": "status"},
        ):
            with self.subTest(frame=frame):
                s = parse_claude_skills(_jsonl(frame))
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


def _codex_cmd(item_id: str, command: str, *, started: bool = False,
               **extra: object) -> dict:
    """One ``codex exec --json`` ``command_execution`` event.

    Mirrors the real envelope a captured reviewer run emits: a
    ``command_execution`` ``item`` under an ``item.started`` /
    ``item.completed`` frame, carrying a shared ``id`` and the shell
    ``command``. Sanitized / minimal -- no raw prompts, diffs, or
    secrets, only the fields the parser reads.
    """
    item = {"id": item_id, "type": "command_execution", "command": command}
    item.update(extra)
    return {"type": "item.started" if started else "item.completed", "item": item}


class CodexSkillTriggerTest(unittest.TestCase):
    """``parse_codex_skills`` over the confirmed ``codex exec --json`` shape.

    Codex has no dedicated ``Skill`` tool: a captured reviewer run pinned the
    only observable trigger as a ``command_execution`` whose ``command`` opens a
    ``skills/<name>/SKILL.md`` file. The parser reads only the ``<name>`` path
    segment, dedups the started/completed pair codex emits per command by its
    shared ``item.id``, keeps first-seen order, and returns empty -- never an
    exception -- on a stream that opens no SKILL.md.
    """

    def test_extracts_skill_from_skill_md_read(self) -> None:
        # The confirmed shape: the reviewer opens the review skill's SKILL.md
        # via a shell command. Codex registers the skill under
        # ``$CODEX_HOME/skills/<name>/SKILL.md``; the read carries an absolute
        # path plus unrelated commands chained after it.
        cmd = ("/bin/bash -lc \"sed -n '1,220p' "
               "/home/u/.codex/skills/review/SKILL.md && git diff -- calc.py\"")
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            _codex_cmd("item_1", cmd, started=True, status="in_progress"),
            _codex_cmd("item_1", cmd, status="completed", exit_code=0),
            {"type": "turn.completed", "usage": {"input_tokens": 10,
                                                 "output_tokens": 5}},
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("review",))
        # started + completed echo the same command; the shared id counts once.
        self.assertEqual(s.trigger_counts, {"review": 1})
        self.assertEqual(s.available, ())

    def test_started_and_completed_not_double_counted(self) -> None:
        # Explicit dedup guard: a single SKILL.md read emits two frames sharing
        # one ``item.id`` -- they must collapse to one trigger.
        cmd = "/bin/bash -lc 'cat skills/develop/SKILL.md'"
        stdout = _jsonl(
            _codex_cmd("item_2", cmd, started=True, status="in_progress"),
            _codex_cmd("item_2", cmd, status="completed", exit_code=0),
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("develop",))
        self.assertEqual(s.trigger_counts, {"develop": 1})

    def test_project_local_skill_paths(self) -> None:
        # Codex discovers project-local skills too: a captured clean-CODEX_HOME
        # run read ``.agents/skills/review/SKILL.md`` directly. Both the
        # ``.agents/`` source and the ``.claude/`` symlink path resolve.
        stdout = _jsonl(
            _codex_cmd("item_1",
                       "/bin/bash -lc \"sed -n '1,200p' "
                       ".agents/skills/develop/SKILL.md\""),
            _codex_cmd("item_2",
                       "/bin/bash -lc 'cat .claude/skills/review/SKILL.md'"),
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("develop", "review"))
        self.assertEqual(s.trigger_counts, {"develop": 1, "review": 1})

    def test_order_dedup_and_counts_across_separate_reads(self) -> None:
        # Distinct ``item.id``s are separate reads: a skill opened in two
        # separate commands counts twice, mirroring the claude path, while the
        # ``triggered`` tuple keeps it once in first-seen order.
        stdout = _jsonl(
            _codex_cmd("item_1", "/bin/bash -lc 'cat skills/develop/SKILL.md'"),
            _codex_cmd("item_2", "/bin/bash -lc 'cat skills/review/SKILL.md'"),
            _codex_cmd("item_3", "/bin/bash -lc 'cat skills/develop/SKILL.md'"),
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("develop", "review"))
        self.assertEqual(s.trigger_counts, {"develop": 2, "review": 1})

    def test_multiple_skills_in_one_command(self) -> None:
        # One command that opens two SKILL.md files records both, in order.
        stdout = _jsonl(
            _codex_cmd("item_1",
                       "/bin/bash -lc 'cat skills/review/SKILL.md "
                       "skills/develop/SKILL.md'"),
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("review", "develop"))
        self.assertEqual(s.trigger_counts, {"review": 1, "develop": 1})

    def test_skill_free_usage_stream_is_empty(self) -> None:
        # A normal run (thread/turn frames, an agent message, a usage-bearing
        # turn.completed, and ordinary command_execution items that touch no
        # SKILL.md) carries no skill trigger; the parser must not false-positive.
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            _codex_cmd("item_1", "/bin/bash -lc 'git diff -- calc.py'"),
            {"type": "item.completed", "item": {
                "id": "item_2", "type": "agent_message", "text": "Approve."}},
            {"type": "turn.completed", "usage": {"input_tokens": 100,
                                                 "cached_input_tokens": 0,
                                                 "output_tokens": 50}},
        )
        self.assertEqual(parse_codex_skills(stdout), SkillTriggers())

    def test_non_skill_md_commands_are_ignored(self) -> None:
        # Touching the skills directory without opening a `<name>/SKILL.md`
        # file is not a trigger; nor is a path where `skills` is a substring of
        # a longer component (`myskills/`), which the boundary anchor rejects.
        stdout = _jsonl(
            _codex_cmd("item_1", "/bin/bash -lc 'ls -la skills/'"),
            _codex_cmd("item_2", "/bin/bash -lc 'grep -rn TODO skills/'"),
            _codex_cmd("item_3", "/bin/bash -lc 'cat myskills/review/SKILL.md'"),
            _codex_cmd("item_4", "/bin/bash -lc 'cat skills/review/README.md'"),
        )
        self.assertEqual(parse_codex_skills(stdout), SkillTriggers())

    def test_system_skill_subdir_is_not_matched(self) -> None:
        # Built-in skills nest under `skills/.system/<name>/SKILL.md`; their
        # SKILL.md is not directly under `skills/`, so the anchor skips them.
        stdout = _jsonl(
            _codex_cmd("item_1",
                       "/bin/bash -lc 'cat skills/.system/imagegen/SKILL.md'"),
        )
        self.assertEqual(parse_codex_skills(stdout), SkillTriggers())

    def test_aggregated_output_is_never_scanned(self) -> None:
        # The command's ``aggregated_output`` carries the file's contents and
        # other command output -- it can echo issue / user text and even a
        # SKILL.md path. The parser reads only ``command``; a command that
        # opens no SKILL.md records nothing even when its output mentions one.
        leaked = "secret: sk-deadbeef and skills/leaked/SKILL.md"
        stdout = _jsonl(
            _codex_cmd("item_1", "/bin/bash -lc 'git diff'",
                       aggregated_output=leaked),
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s, SkillTriggers())
        self.assertNotIn(leaked, repr(s))
        self.assertNotIn("leaked", repr(s))

    def test_only_the_name_segment_is_captured_for_privacy(self) -> None:
        # The command around the SKILL.md read can carry issue / user content;
        # only the `<name>` path segment is ever extracted, never the rest.
        secret = "user secret: api_key=sk-deadbeef"
        stdout = _jsonl(
            _codex_cmd("item_1",
                       "/bin/bash -lc \"cat skills/review/SKILL.md; "
                       f"echo '{secret}'\""),
        )
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("review",))
        self.assertNotIn(secret, repr(s))
        self.assertNotIn("sk-deadbeef", repr(s))

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps(
            _codex_cmd("item_1", "/bin/bash -lc 'cat skills/develop/SKILL.md'"))
        stdout = "\n".join([
            "codex starting...",
            '{"truncated":',
            good,
            "trailing-noise",
        ])
        s = parse_codex_skills(stdout)
        self.assertEqual(s.triggered, ("develop",))
        self.assertEqual(s.trigger_counts, {"develop": 1})

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
        # A codex SKILL.md-read command_execution is recognized only by the
        # codex path; the claude parser returns empty on it, so a non-empty
        # result here proves the codex parser ran.
        stdout = _jsonl(_codex_cmd(
            "item_1", "/bin/bash -lc 'cat skills/review/SKILL.md'"))
        self.assertEqual(parse_agent_skills("codex", stdout).triggered,
                         ("review",))
        self.assertEqual(parse_claude_skills(stdout), SkillTriggers())

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_agent_skills("gemini", "")


class ClaudeTrajectoryTest(unittest.TestCase):
    """``parse_claude_trajectory`` over synthetic ``stream-json`` runs.

    The init frame's ``tools`` array is the offered-tools set; in stream
    order, ``text`` blocks in ``assistant`` messages are ``assistant_message``
    turns and their ``tool_use`` blocks are calls, while ``text`` blocks in
    ``user`` messages are ``user_message`` turns and their ``tool_result``
    blocks are results (joined by ``tool_use_id``); the ``result`` frame's
    ``result`` string is the final output. Raw inputs / outputs / text ride
    along verbatim -- this layer classifies, it does not redact.
    """

    def test_extracts_tools_steps_skills_and_final_output(self) -> None:
        stdout = _jsonl(
            {"type": "system", "subtype": "init",
             "tools": ["Bash", "Read", "Skill"],
             "skills": ["develop", "review"]},
            {
                "type": "assistant",
                "message": {
                    "id": "msg_1",
                    "content": [
                        {"type": "text", "text": "let me look"},
                        {"type": "tool_use", "id": "toolu_a", "name": "Bash",
                         "input": {"command": "ls"}},
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_a",
                         "content": "calc.py\n"},
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "msg_2",
                    "content": [
                        {"type": "tool_use", "id": "toolu_b", "name": "Skill",
                         "input": {"skill": "develop"}},
                    ],
                },
            },
            {"type": "result", "result": "All done.", "num_turns": 2},
        )
        t = parse_claude_trajectory(stdout)
        self.assertEqual(t.backend, "claude")
        self.assertIsNone(t.system_prompt)
        self.assertEqual(t.tools, ("Bash", "Read", "Skill"))
        self.assertEqual(t.final_output, "All done.")
        # Skills reuse the names-only extractor (offered + triggered).
        self.assertEqual(t.skills.available, ("develop", "review"))
        self.assertEqual(t.skills.triggered, ("develop",))
        # Ordered timeline: assistant text -> call -> result -> call.
        self.assertEqual(len(t.steps), 4)
        self.assertEqual(
            t.steps[0],
            TrajectoryStep(kind="assistant_message", content="let me look"),
        )
        self.assertEqual(
            t.steps[1],
            TrajectoryStep(kind="tool_call", name="Bash", tool_id="toolu_a",
                           content={"command": "ls"}),
        )
        self.assertEqual(
            t.steps[2],
            TrajectoryStep(kind="tool_result", tool_id="toolu_a",
                           content="calc.py\n"),
        )
        self.assertEqual(
            t.steps[3],
            TrajectoryStep(kind="tool_call", name="Skill", tool_id="toolu_b",
                           content={"skill": "develop"}),
        )

    def test_captures_assistant_and_user_text_turns_in_stream_order(
        self,
    ) -> None:
        # Full timeline: an assistant text turn, then a tool call + its
        # result and a user text turn in the same user message, then a closing
        # assistant text turn -- text turns are preserved inline with the tool
        # steps, in stream order, alongside the unchanged final output.
        stdout = _jsonl(
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "id": "tu1", "name": "Read",
                 "input": {"file_path": "x.py"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu1",
                 "content": "file body"},
                {"type": "text", "text": "now fix it"}]}},
            {"type": "assistant", "message": {"id": "m2", "content": [
                {"type": "text", "text": "done"}]}},
            {"type": "result", "result": "all set"},
        )
        t = parse_claude_trajectory(stdout)
        self.assertEqual(
            [(s.kind, s.content) for s in t.steps],
            [
                ("assistant_message", "let me check"),
                ("tool_call", {"file_path": "x.py"}),
                ("tool_result", "file body"),
                ("user_message", "now fix it"),
                ("assistant_message", "done"),
            ],
        )
        # Text turns carry no tool name / id.
        first = t.steps[0]
        self.assertEqual(first.name, "")
        self.assertEqual(first.tool_id, "")
        self.assertEqual(t.final_output, "all set")

    def test_empty_or_nonstring_text_blocks_are_skipped(self) -> None:
        # An empty / missing / non-string text block does not create a
        # message step -- only non-empty string text turns are captured.
        stdout = _jsonl(
            {"type": "assistant", "message": {"id": "m", "content": [
                {"type": "text", "text": ""},
                {"type": "text"},
                {"type": "text", "text": 7}]}},
            {"type": "user", "message": {"content": [
                {"type": "text", "text": ""}]}},
        )
        self.assertEqual(parse_claude_trajectory(stdout).steps, ())

    def test_partial_frames_dedup_calls_and_results(self) -> None:
        # Defensive: a tool_use / tool_result block repeated across frames
        # (sharing its id) is one step, not two -- the same per-id de-dup
        # ``parse_claude_skills`` applies. Distinct ids stay distinct.
        stdout = _jsonl(
            {
                "type": "assistant",
                "message": {"id": "msg_1", "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "Bash",
                     "input": {"command": "ls"}}]},
            },
            {
                "type": "assistant",
                "message": {"id": "msg_1", "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "Bash",
                     "input": {"command": "ls"}}]},
            },
            {
                "type": "user",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "toolu_a",
                     "content": "out"}]},
            },
            {
                "type": "user",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "toolu_a",
                     "content": "out"}]},
            },
        )
        t = parse_claude_trajectory(stdout)
        self.assertEqual([s.kind for s in t.steps],
                         ["tool_call", "tool_result"])

    def test_missing_fields_yield_empty_sections(self) -> None:
        # No init frame, no capturable blocks, no result frame: every section
        # is empty / None, never an exception.
        stdout = _jsonl(
            {"type": "assistant",
             "message": {"id": "m", "content": []}},
        )
        t = parse_claude_trajectory(stdout)
        self.assertEqual(t.tools, ())
        self.assertEqual(t.steps, ())
        self.assertIsNone(t.final_output)
        self.assertIsNone(t.system_prompt)
        self.assertEqual(t.skills, SkillTriggers())

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps({
            "type": "assistant",
            "message": {"id": "m", "content": [
                {"type": "tool_use", "id": "toolu_a", "name": "Read",
                 "input": {"file_path": "x.py"}}]},
        })
        stdout = "\n".join([
            "starting claude...",
            '{"type":"assistant","message"',
            good,
            "not json either",
        ])
        t = parse_claude_trajectory(stdout)
        self.assertEqual(len(t.steps), 1)
        self.assertEqual(t.steps[0].name, "Read")

    def test_empty_stdout(self) -> None:
        self.assertEqual(parse_claude_trajectory(""),
                         AgentTrajectory(backend="claude"))


class CodexTrajectoryTest(unittest.TestCase):
    """``parse_codex_trajectory`` over synthetic ``codex exec --json`` runs.

    Codex's tool surface is the shell: each ``command_execution`` is one call
    (its ``command``) plus one result (its ``aggregated_output``), deduped by
    the shared ``item.id`` across the started/completed pair; each
    ``agent_message`` is one ``assistant_message`` text turn (its ``text``),
    captured in stream order. The last ``agent_message`` ``text`` is also the
    final output; ``tools`` / ``system_prompt`` stay empty (no confirmed codex
    frame exposes them).
    """

    def test_extracts_steps_skills_and_final_output(self) -> None:
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            _codex_cmd("item_1", "/bin/bash -lc 'cat skills/develop/SKILL.md'",
                       started=True, status="in_progress"),
            _codex_cmd("item_1", "/bin/bash -lc 'cat skills/develop/SKILL.md'",
                       status="completed", exit_code=0,
                       aggregated_output="# Developer skill\n"),
            _codex_cmd("item_2", "/bin/bash -lc 'git diff -- calc.py'",
                       status="completed", exit_code=0,
                       aggregated_output="diff --git ...\n"),
            {"type": "item.completed", "item": {
                "id": "item_3", "type": "agent_message", "text": "Approve."}},
        )
        t = parse_codex_trajectory(stdout)
        self.assertEqual(t.backend, "codex")
        self.assertIsNone(t.system_prompt)
        self.assertEqual(t.tools, ())
        self.assertEqual(t.final_output, "Approve.")
        # SKILL.md read surfaces in the names-only skills extractor.
        self.assertEqual(t.skills.triggered, ("develop",))
        # started + completed for item_1 collapse to one call + one result;
        # the trailing agent_message rides along as an assistant_message turn
        # (and is also the final output).
        self.assertEqual(
            t.steps,
            (
                TrajectoryStep(
                    kind="tool_call", name="command_execution",
                    tool_id="item_1",
                    content="/bin/bash -lc 'cat skills/develop/SKILL.md'"),
                TrajectoryStep(
                    kind="tool_result", tool_id="item_1",
                    content="# Developer skill\n"),
                TrajectoryStep(
                    kind="tool_call", name="command_execution",
                    tool_id="item_2",
                    content="/bin/bash -lc 'git diff -- calc.py'"),
                TrajectoryStep(
                    kind="tool_result", tool_id="item_2",
                    content="diff --git ...\n"),
                TrajectoryStep(
                    kind="assistant_message", content="Approve."),
            ),
        )

    def test_agent_messages_captured_as_assistant_turns_in_order(self) -> None:
        # Each agent_message item becomes an assistant_message turn, kept in
        # stream order relative to the command steps; the last one is still the
        # final output.
        stdout = _jsonl(
            {"type": "item.completed", "item": {
                "id": "a1", "type": "agent_message", "text": "starting"}},
            _codex_cmd("c1", "/bin/bash -lc 'ls'", status="completed",
                       exit_code=0, aggregated_output="out\n"),
            {"type": "item.completed", "item": {
                "id": "a2", "type": "agent_message", "text": "all done"}},
        )
        t = parse_codex_trajectory(stdout)
        self.assertEqual(
            [(s.kind, s.content) for s in t.steps],
            [
                ("assistant_message", "starting"),
                ("tool_call", "/bin/bash -lc 'ls'"),
                ("tool_result", "out\n"),
                ("assistant_message", "all done"),
            ],
        )
        self.assertEqual(t.final_output, "all done")

    def test_agent_message_started_and_completed_collapse(self) -> None:
        # A started + completed agent_message sharing an item.id is one turn
        # (last text wins), mirroring the command started/completed collapse.
        stdout = _jsonl(
            {"type": "item.started", "item": {
                "id": "a1", "type": "agent_message", "text": "partial"}},
            {"type": "item.completed", "item": {
                "id": "a1", "type": "agent_message", "text": "final text"}},
        )
        t = parse_codex_trajectory(stdout)
        self.assertEqual(
            t.steps,
            (TrajectoryStep(kind="assistant_message", content="final text"),),
        )
        self.assertEqual(t.final_output, "final text")

    def test_empty_or_nonstring_agent_message_is_skipped(self) -> None:
        # An empty / non-string agent_message text creates no turn.
        stdout = _jsonl(
            {"type": "item.completed", "item": {
                "id": "a1", "type": "agent_message", "text": ""}},
            {"type": "item.completed", "item": {
                "id": "a2", "type": "agent_message", "text": 7}},
        )
        self.assertEqual(parse_codex_trajectory(stdout).steps, ())

    def test_started_only_command_emits_call_without_result(self) -> None:
        # A command that never completes (no aggregated_output) is a call with
        # no result step rather than a fabricated empty result.
        stdout = _jsonl(
            _codex_cmd("item_1", "/bin/bash -lc 'sleep 99'",
                       started=True, status="in_progress"),
        )
        t = parse_codex_trajectory(stdout)
        self.assertEqual(len(t.steps), 1)
        self.assertEqual(t.steps[0].kind, "tool_call")
        self.assertEqual(t.steps[0].tool_id, "item_1")

    def test_missing_fields_yield_empty_sections(self) -> None:
        stdout = _jsonl(
            {"type": "thread.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        )
        t = parse_codex_trajectory(stdout)
        self.assertEqual(t.steps, ())
        self.assertIsNone(t.final_output)
        self.assertEqual(t.tools, ())
        self.assertEqual(t.skills, SkillTriggers())

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps(_codex_cmd(
            "item_1", "/bin/bash -lc 'ls'", status="completed",
            aggregated_output="out\n"))
        stdout = "\n".join([
            "codex starting...",
            '{"truncated":',
            good,
            "trailing-noise",
        ])
        t = parse_codex_trajectory(stdout)
        self.assertEqual([s.kind for s in t.steps],
                         ["tool_call", "tool_result"])

    def test_empty_stdout(self) -> None:
        self.assertEqual(parse_codex_trajectory(""),
                         AgentTrajectory(backend="codex"))


class TrajectoryDispatcherTest(unittest.TestCase):
    """``parse_agent_trajectory`` routes by backend, mirroring the siblings."""

    def test_routes_claude(self) -> None:
        self.assertEqual(parse_agent_trajectory("claude", "").backend, "claude")

    def test_routes_codex(self) -> None:
        self.assertEqual(parse_agent_trajectory("codex", "").backend, "codex")

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_agent_trajectory("gemini", "")


class AgentTrajectoryTest(unittest.TestCase):
    def test_to_dict_round_trips_via_json(self) -> None:
        t = AgentTrajectory(
            backend="claude",
            tools=("Bash", "Read"),
            skills=SkillTriggers(triggered=("develop",),
                                 trigger_counts={"develop": 1},
                                 available=("develop", "review")),
            steps=(
                TrajectoryStep(kind="tool_call", name="Bash", tool_id="t1",
                               content={"command": "ls"}),
                TrajectoryStep(kind="tool_result", tool_id="t1",
                               content="out"),
            ),
            final_output="done",
        )
        encoded = json.dumps(t.to_dict(), sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["backend"], "claude")
        self.assertEqual(decoded["tools"], ["Bash", "Read"])
        self.assertEqual(decoded["system_prompt"], None)
        self.assertEqual(decoded["final_output"], "done")
        self.assertEqual(decoded["skills"]["triggered"], ["develop"])
        self.assertEqual(decoded["skills"]["available"], ["develop", "review"])
        self.assertEqual(len(decoded["steps"]), 2)
        self.assertEqual(decoded["steps"][0]["name"], "Bash")
        self.assertEqual(decoded["steps"][1]["kind"], "tool_result")


if __name__ == "__main__":
    unittest.main()
