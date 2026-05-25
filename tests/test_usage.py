# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import unittest

from orchestrator.usage import (
    UsageMetrics,
    parse_agent_usage,
    parse_claude_usage,
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


if __name__ == "__main__":
    unittest.main()
