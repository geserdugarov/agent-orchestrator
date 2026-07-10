# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import unittest

from orchestrator.usage import (
    AgentTrajectory,
    SkillTriggers,
    TrajectoryStep,
    TurnUsage,
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


# --- Backend + model identifiers -----------------------------------------
# Backends the dispatchers route on.
CLAUDE = "claude"
CODEX = "codex"

# Utility model SKUs the general fixtures name. Each maps to a known row in
# the price table; the per-1M rate a test applies stays inline beside its
# expected-cost assertion, so the pricing math is auditable there rather than
# here. The codex pricing-coverage tests keep their SKU strings inline for the
# same reason -- the exact string is what the prefix match is being audited on.
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-3-5"
OPUS_4_7 = "claude-opus-4-7"
OPUS_4_8 = "claude-opus-4-8"
GPT_5_CODEX = "gpt-5-codex"
GPT_5_MINI = "gpt-5-mini"
# A SKU with no first-party rate: usage lands but cost stays unknown-price.
UNKNOWN_MODEL = "third-party-model-x"

# Skill names the claude-side trigger fixtures reference. The codex skill /
# trajectory tests keep skill names inline: there the name is a path segment
# of the ``skills/<name>/SKILL.md`` command those tests exist to parse.
DEVELOP = "develop"
REVIEW = "review"
VERIFY = "verify"

TOKENS_PER_MILLION = 1_000_000

# Reused Codex pricing fixtures. Rate-table values stay inline in each cost
# assertion; these names identify the token shapes being priced.
LONG_CONTEXT_THRESHOLD_TOKENS = 272_000
LONG_CONTEXT_INPUT_TOKENS = 300_000
LONG_CONTEXT_CACHED_INPUT_TOKENS = 100_000
CODEX_PRICING_OUTPUT_TOKENS = 1_000
PRO_PRICING_INPUT_TOKENS = 100_000
PRO_PRICING_CACHED_INPUT_TOKENS = 50_000

# The representative Claude turn reused by parsing and serialization tests.
CLAUDE_TURN_INPUT_TOKENS = 12
CLAUDE_TURN_OUTPUT_TOKENS = 340
CLAUDE_TURN_CACHE_READ_TOKENS = 18_240
CLAUDE_TURN_CACHE_WRITE_TOKENS = 512


# --- Stream frame builders -----------------------------------------------
# Each returns one decoded stream event; ``_jsonl`` serializes a sequence of
# them. Token counts and models stay explicit at the call site so pricing and
# aggregation assertions read against visible inputs. Cache fields left at
# ``None`` stay absent from the emitted usage (the parser reads absent as 0).

def _claude_usage(*, input=0, output=0, cache_write=None, cache_read=None,
                  cache_5m=None, cache_1h=None):
    """A claude ``message.usage`` block.

    ``cache_write`` emits the flat ``cache_creation_input_tokens``;
    ``cache_5m`` / ``cache_1h`` emit the structured
    ``cache_creation.ephemeral_*`` form the 5m/1h split rides on instead.
    """
    usage: dict = {"input_tokens": input, "output_tokens": output}
    if cache_write is not None:
        usage["cache_creation_input_tokens"] = cache_write
    if cache_read is not None:
        usage["cache_read_input_tokens"] = cache_read
    if cache_5m is not None or cache_1h is not None:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": cache_5m or 0,
            "ephemeral_1h_input_tokens": cache_1h or 0,
        }
    return usage


def _assistant(*, id="msg_1", model=None, usage=None, content=None):
    """An ``assistant`` frame; the final frame per ``id`` wins for usage."""
    message: dict = {"id": id}
    if model is not None:
        message["model"] = model
    if content is not None:
        message["content"] = content
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "message": message}


def _system_init(**fields):
    """A ``system``/``init`` frame; ``tools`` / ``skills`` ride in ``fields``."""
    return {"type": "system", "subtype": "init", **fields}


def _result(**fields):
    """The terminal ``result`` frame (``num_turns`` / ``total_cost_usd`` / ``result``)."""
    return {"type": "result", **fields}


def _text(text):
    return {"type": "text", "text": text}


def _tool_use(name, tool_input, *, id=None):
    block: dict = {"type": "tool_use", "name": name}
    if id is not None:
        block["id"] = id
    block["input"] = tool_input
    return block


def _skill_use(skill, *, id=None, args=None):
    """A ``Skill`` ``tool_use`` block; ``args`` exercises the privacy path."""
    payload: dict = {"skill": skill}
    if args is not None:
        payload["args"] = args
    return _tool_use("Skill", payload, id=id)


def _tool_result(tool_use_id, content):
    return {"type": "tool_result", "tool_use_id": tool_use_id,
            "content": content}


def _user(content):
    return {"type": "user", "message": {"content": content}}


def _codex_usage(*, input=0, cached=0, output=0):
    """A codex ``usage`` block (cumulative; the final non-zero record wins)."""
    return {"input_tokens": input, "cached_input_tokens": cached,
            "output_tokens": output}


def _turn_complete(*, model=None, input=0, cached=0, output=0):
    """A codex ``turn_complete`` frame carrying a cumulative usage record."""
    frame: dict = {"type": "turn_complete"}
    if model is not None:
        frame["model"] = model
    frame["usage"] = _codex_usage(input=input, cached=cached, output=output)
    return frame


def _task_started(**fields):
    return {"type": "task_started", **fields}


def _task_complete(**fields):
    return {"type": "task_complete", **fields}


class ClaudeStreamJsonTest(unittest.TestCase):
    """Synthetic ``claude -p --output-format stream-json`` runs.

    Final assistant frame per ``message.id`` wins (claude streams partial
    usage on intermediate frames); per-model totals roll up into the
    flattened ``UsageMetrics`` shape.
    """

    def test_extracts_tokens_model_and_estimates_cost(self) -> None:
        stdout = _jsonl(
            _system_init(session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            _assistant(model=SONNET, usage=_claude_usage(
                input=100, cache_write=1000, cache_read=5000, output=200)),
            _assistant(model=SONNET, usage=_claude_usage(
                input=150, cache_write=1200, cache_read=6000, output=300)),
            _result(num_turns=3),
        )
        metrics = parse_claude_usage(stdout)
        self.assertEqual(metrics.backend, CLAUDE)
        self.assertEqual(metrics.models, (SONNET,))
        self.assertEqual(metrics.input_tokens, 150)
        self.assertEqual(metrics.output_tokens, 300)
        self.assertEqual(metrics.cache_read_tokens, 6000)
        self.assertEqual(metrics.cache_write_tokens, 1200)
        self.assertEqual(metrics.cached_tokens, 0)
        self.assertEqual(metrics.turns, 3)
        # sonnet rates: input=3, cw5m=3.75, cr=0.30, output=15 (per 1M)
        expected = (
            150 * 3 + 1200 * 3.75 + 6000 * 0.30 + 300 * 15
        ) / TOKENS_PER_MILLION
        self.assertEqual(metrics.cost_source, "estimated")
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_structured_cache_creation_splits_5m_and_1h(self) -> None:
        # The structured form (``cache_creation.ephemeral_*_input_tokens``)
        # bills 5m and 1h cache writes at different rates; the parser must
        # keep them separate rather than collapse both onto the 5m bucket.
        stdout = _jsonl(
            _assistant(model=OPUS_4_7, usage=_claude_usage(
                input=0, cache_5m=1000, cache_1h=500, output=100)),
        )
        metrics = parse_claude_usage(stdout)
        # opus-4-7 rates: input=5, cw5m=6.25, cw1h=10, cr=0.50, output=25
        expected = (
            0 * 5 + 1000 * 6.25 + 500 * 10 + 0 * 0.50 + 100 * 25
        ) / TOKENS_PER_MILLION
        self.assertEqual(metrics.cache_write_tokens, 1500)
        self.assertEqual(metrics.cost_source, "estimated")
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_reported_total_cost_overrides_estimate(self) -> None:
        # Even when we *could* compute an estimate, the agent's own
        # ``total_cost_usd`` on the result frame is authoritative -- it
        # already accounts for any pricing nuance we may have missed.
        stdout = _jsonl(
            _assistant(model=SONNET, usage=_claude_usage(input=100, output=200)),
            _result(total_cost_usd=0.42, num_turns=1),
        )
        metrics = parse_claude_usage(stdout)
        self.assertEqual(metrics.cost_source, "reported")
        self.assertEqual(metrics.cost_usd, 0.42)

    def test_unknown_model_yields_unknown_price(self) -> None:
        # Usage is present but no first-party rates match the SKU; we must
        # report unknown-price rather than guess at zero cost.
        stdout = _jsonl(
            _assistant(model=UNKNOWN_MODEL,
                       usage=_claude_usage(input=100, output=200)),
        )
        metrics = parse_claude_usage(stdout)
        self.assertEqual(metrics.cost_source, "unknown-price")
        self.assertIsNone(metrics.cost_usd)
        self.assertEqual(metrics.input_tokens, 100)
        self.assertEqual(metrics.output_tokens, 200)

    def test_no_usage_events_returns_no_usage(self) -> None:
        stdout = _jsonl(
            _system_init(),
            _result(num_turns=0),
        )
        metrics = parse_claude_usage(stdout)
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
            _assistant(model=SONNET, usage=_claude_usage(input=10, output=20)))
        stdout = "\n".join([
            "starting claude...",
            '{"type":"assistant","message"',
            good,
            "",
            "  ",
            "not json either",
        ])
        metrics = parse_claude_usage(stdout)
        self.assertEqual(metrics.input_tokens, 10)
        self.assertEqual(metrics.output_tokens, 20)
        self.assertEqual(metrics.cost_source, "estimated")

    def test_empty_stdout(self) -> None:
        metrics = parse_claude_usage("")
        self.assertEqual(metrics, UsageMetrics(backend=CLAUDE))

    def test_multiple_models_aggregate_when_all_priced(self) -> None:
        stdout = _jsonl(
            _assistant(id="msg_a", model=SONNET,
                       usage=_claude_usage(input=100, output=50)),
            _assistant(id="msg_b", model=HAIKU,
                       usage=_claude_usage(input=200, output=100)),
        )
        metrics = parse_claude_usage(stdout)
        self.assertEqual(set(metrics.models), {SONNET, HAIKU})
        self.assertEqual(metrics.input_tokens, 300)
        self.assertEqual(metrics.output_tokens, 150)
        self.assertEqual(metrics.cost_source, "estimated")
        # sonnet: input=3, output=15; haiku-3-5: input=0.80, output=4
        expected = (
            (100 * 3 + 50 * 15) + (200 * 0.80 + 100 * 4)
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)


class CodexJsonTest(unittest.TestCase):
    """Synthetic ``codex exec --json`` runs.

    Codex emits cumulative usage on each event; the parser takes the
    final non-zero record as the authoritative total rather than summing
    deltas.
    """

    def test_extracts_tokens_model_and_estimates_cost(self) -> None:
        stdout = _jsonl(
            _task_started(session_id="11111111-2222-3333-4444-555555555555"),
            _turn_complete(model=GPT_5_CODEX, input=500, cached=100, output=200),
            _turn_complete(model=GPT_5_CODEX, input=1000, cached=200, output=400),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.backend, CODEX)
        self.assertEqual(metrics.models, (GPT_5_CODEX,))
        # Cumulative: final usage record wins (NOT sum of two events).
        self.assertEqual(metrics.input_tokens, 1000)
        self.assertEqual(metrics.cached_tokens, 200)
        self.assertEqual(metrics.output_tokens, 400)
        self.assertEqual(metrics.cache_read_tokens, 0)
        self.assertEqual(metrics.cache_write_tokens, 0)
        # gpt-5-codex rates: input=1.25, cached=0.125, output=10
        uncached = 1000 - 200
        expected = (
            uncached * 1.25 + 200 * 0.125 + 400 * 10
        ) / TOKENS_PER_MILLION
        self.assertEqual(metrics.cost_source, "estimated")
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)
        self.assertEqual(metrics.turns, 2)

    def test_picks_up_nested_usage_and_num_turns(self) -> None:
        # Codex sometimes nests usage under ``info.total_token_usage`` and
        # publishes ``num_turns`` deep inside a payload object; both must
        # still be reachable via the recursive search.
        stdout = _jsonl(
            {
                "type": "session_summary",
                "payload": {
                    "info": {
                        "model": GPT_5_MINI,
                        "total_token_usage": _codex_usage(
                            input=800, cached=0, output=100),
                        "num_turns": 7,
                    },
                },
            },
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.models, (GPT_5_MINI,))
        self.assertEqual(metrics.input_tokens, 800)
        self.assertEqual(metrics.output_tokens, 100)
        self.assertEqual(metrics.turns, 7)
        # gpt-5-mini rates: input=0.25, cached=0.025, output=2
        expected = (800 * 0.25 + 0 + 100 * 2) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_reported_total_cost_overrides_estimate(self) -> None:
        stdout = _jsonl(
            _turn_complete(model=GPT_5_CODEX, input=1000, cached=0, output=100),
            _task_complete(total_cost_usd=0.07, num_turns=1),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "reported")
        self.assertEqual(metrics.cost_usd, 0.07)

    def test_unknown_model_yields_unknown_price(self) -> None:
        stdout = _jsonl(
            _turn_complete(model="made-up-vendor-mini",
                           input=100, cached=0, output=50),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "unknown-price")
        self.assertIsNone(metrics.cost_usd)
        self.assertEqual(metrics.input_tokens, 100)
        self.assertEqual(metrics.output_tokens, 50)

    def test_fallback_model_used_when_events_omit_one(self) -> None:
        # The CLI sometimes streams usage events without echoing the model
        # name; callers can pass the configured `-m` value as a fallback so
        # an estimate is still possible.
        stdout = _jsonl(
            _turn_complete(input=100, cached=0, output=50),
        )
        metrics = parse_codex_usage(stdout, fallback_model=GPT_5_CODEX)
        self.assertEqual(metrics.cost_source, "estimated")
        # Models list stays anchored on what the stream actually emitted;
        # the fallback only feeds the price lookup.
        self.assertEqual(metrics.models, (GPT_5_CODEX,))
        assert metrics.cost_usd is not None
        expected = (100 * 1.25 + 0 + 50 * 10) / TOKENS_PER_MILLION
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_cached_tokens_without_cached_rate_blocks_estimate(self) -> None:
        # A model whose published price table has no cached rate cannot be
        # estimated when the run actually used cache reads -- billing those
        # at the input rate would overcharge. Defer to unknown-price.
        stdout = _jsonl(
            _turn_complete(model="gpt-5.5-pro", input=500, cached=100, output=200),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "unknown-price")
        self.assertIsNone(metrics.cost_usd)

    def test_gpt_5_5_usage_yields_estimated_cost(self) -> None:
        # gpt-5.5 is in the priced family table; usage that names it
        # explicitly must produce an `estimated` cost rather than
        # falling through to `unknown-price`. Pricing-coverage guard:
        # if the row gets accidentally dropped from `_CODEX_RATES`
        # the test fails loudly and the dashboard's
        # `cost_source='unknown-price'` cohort gains a regression
        # before any operator notices.
        stdout = _jsonl(
            _turn_complete(model="gpt-5.5", input=1000, cached=200, output=400),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "estimated")
        self.assertEqual(metrics.models, ("gpt-5.5",))
        # gpt-5.5 rates: input=5, cached=0.50, output=30 (per 1M)
        uncached = 1000 - 200
        expected = (
            uncached * 5 + 200 * 0.50 + 400 * 30
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_5_5_reported_cost_wins_over_estimate(self) -> None:
        # Even when usage matches the priced gpt-5.5 family, a CLI-
        # reported `total_cost_usd` on the terminal frame is the
        # authoritative figure (it already accounts for any pricing
        # nuance our table may have missed). Precedence guard so a
        # future change to the priced-model path does not start
        # overriding reported values.
        stdout = _jsonl(
            _turn_complete(model="gpt-5.5", input=1000, cached=0, output=200),
            _task_complete(total_cost_usd=0.99, num_turns=1),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "reported")
        self.assertEqual(metrics.cost_usd, 0.99)

    def test_gpt_5_5_long_context_uses_tiered_pricing(self) -> None:
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
            _turn_complete(
                model="gpt-5.5",
                input=LONG_CONTEXT_INPUT_TOKENS,
                cached=0,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "estimated")
        # Long-context tier: input * 5 * 2 + output * 30 * 1.5, /1M.
        expected = (
            LONG_CONTEXT_INPUT_TOKENS * 5 * 2.0
            + CODEX_PRICING_OUTPUT_TOKENS * 30 * 1.5
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_5_5_at_or_under_threshold_uses_flat_rate(self) -> None:
        # The tier applies strictly when input > threshold; at or
        # under 272K the standard flat rates apply unchanged. This
        # is the boundary regression guard for the new long-context
        # branch.
        stdout = _jsonl(
            _turn_complete(
                model="gpt-5.5",
                input=LONG_CONTEXT_THRESHOLD_TOKENS,
                cached=0,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "estimated")
        # Flat rate: input * 5 + output * 30, /1M (no multipliers).
        expected = (
            LONG_CONTEXT_THRESHOLD_TOKENS * 5
            + CODEX_PRICING_OUTPUT_TOKENS * 30
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

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
            _turn_complete(
                model="gpt-5.5-pro",
                input=LONG_CONTEXT_INPUT_TOKENS,
                cached=0,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "estimated")
        # Flat pro rates: input=30, output=180; NO multipliers.
        expected = (
            LONG_CONTEXT_INPUT_TOKENS * 30
            + CODEX_PRICING_OUTPUT_TOKENS * 180
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_5_4_long_context_uses_tiered_pricing(self) -> None:
        # gpt-5.4 carries the same >272K input long-context tier as
        # gpt-5.5 per OpenAI's GPT-5.4 pricing docs: 2x input, 1.5x
        # output. Same regression-guard shape as the gpt-5.5 test --
        # a flat-rate fallback would silently undercount real runs.
        stdout = _jsonl(
            _turn_complete(
                model="gpt-5.4",
                input=LONG_CONTEXT_INPUT_TOKENS,
                cached=0,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "estimated")
        # gpt-5.4 rates: input=2.50, output=15; long-context 2x / 1.5x.
        expected = (
            LONG_CONTEXT_INPUT_TOKENS * 2.50 * 2.0
            + CODEX_PRICING_OUTPUT_TOKENS * 15 * 1.5
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_5_4_pro_long_context_uses_tiered_pricing(self) -> None:
        # gpt-5.4-pro mirrors gpt-5.5-pro: same threshold + multipliers.
        stdout = _jsonl(
            _turn_complete(
                model="gpt-5.4-pro",
                input=LONG_CONTEXT_INPUT_TOKENS,
                cached=0,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "estimated")
        expected = (
            LONG_CONTEXT_INPUT_TOKENS * 30 * 2.0
            + CODEX_PRICING_OUTPUT_TOKENS * 180 * 1.5
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

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
                    _turn_complete(
                        model=model,
                        input=LONG_CONTEXT_INPUT_TOKENS,
                        cached=0,
                        output=CODEX_PRICING_OUTPUT_TOKENS,
                    ),
                )
                metrics = parse_codex_usage(stdout)
                self.assertEqual(metrics.cost_source, "estimated")
                expected = (
                    LONG_CONTEXT_INPUT_TOKENS * rates["input"]
                    + CODEX_PRICING_OUTPUT_TOKENS * rates["output"]
                ) / TOKENS_PER_MILLION
                assert metrics.cost_usd is not None
                self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_5_2_pro_uses_its_own_rate_not_base(self) -> None:
        # `_codex_rates` is prefix-matched on insertion order, so a
        # missing explicit `gpt-5.2-pro` entry would silently fall
        # through to `gpt-5.2`'s $1.75 / $14 rates and undercount
        # by an order of magnitude. Pin the pro rate so an accidental
        # entry removal or reorder fails loudly here.
        stdout = _jsonl(
            _turn_complete(
                model="gpt-5.2-pro",
                input=PRO_PRICING_INPUT_TOKENS,
                cached=0,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "estimated")
        # Per OpenAI's gpt-5.2-pro page: $21 / $168, no cached rate.
        expected = (
            PRO_PRICING_INPUT_TOKENS * 21
            + CODEX_PRICING_OUTPUT_TOKENS * 168
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_5_2_pro_cached_tokens_block_estimate(self) -> None:
        # The pro tier publishes no cached-input discount; a run with
        # cached tokens must surface as `unknown-price` rather than
        # bill those tokens at the input rate (overcharge) or the
        # fallthrough sibling's cached rate (undercharge).
        stdout = _jsonl(
            _turn_complete(
                model="gpt-5.2-pro",
                input=PRO_PRICING_INPUT_TOKENS,
                cached=PRO_PRICING_CACHED_INPUT_TOKENS,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "unknown-price")
        self.assertIsNone(metrics.cost_usd)

    def test_gpt_5_pro_uses_its_own_rate_not_base(self) -> None:
        # Same prefix-fallthrough guard as gpt-5.2-pro: `gpt-5-pro`
        # would otherwise hit the `gpt-5` entry ($1.25 / $10) and
        # undercount by an order of magnitude.
        stdout = _jsonl(
            _turn_complete(
                model="gpt-5-pro",
                input=PRO_PRICING_INPUT_TOKENS,
                cached=0,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "estimated")
        # Per OpenAI's gpt-5-pro page: $15 / $120, no cached rate.
        expected = (
            PRO_PRICING_INPUT_TOKENS * 15
            + CODEX_PRICING_OUTPUT_TOKENS * 120
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_gpt_5_pro_cached_tokens_block_estimate(self) -> None:
        stdout = _jsonl(
            _turn_complete(
                model="gpt-5-pro",
                input=PRO_PRICING_INPUT_TOKENS,
                cached=PRO_PRICING_CACHED_INPUT_TOKENS,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "unknown-price")
        self.assertIsNone(metrics.cost_usd)

    def test_gpt_5_5_long_context_cached_tokens_also_tier_up(self) -> None:
        # Cached input tokens are still input billing -- the long-
        # context multiplier must apply to them too. Otherwise a
        # cache-heavy session over the threshold would silently
        # under-report against OpenAI's actual bill.
        stdout = _jsonl(
            _turn_complete(
                model="gpt-5.5",
                input=LONG_CONTEXT_INPUT_TOKENS,
                cached=LONG_CONTEXT_CACHED_INPUT_TOKENS,
                output=CODEX_PRICING_OUTPUT_TOKENS,
            ),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "estimated")
        uncached = LONG_CONTEXT_INPUT_TOKENS - LONG_CONTEXT_CACHED_INPUT_TOKENS
        expected = (
            uncached * 5 * 2.0
            + LONG_CONTEXT_CACHED_INPUT_TOKENS * 0.50 * 2.0
            + CODEX_PRICING_OUTPUT_TOKENS * 30 * 1.5
        ) / TOKENS_PER_MILLION
        assert metrics.cost_usd is not None
        self.assertAlmostEqual(metrics.cost_usd, expected, places=9)

    def test_truly_unknown_model_remains_unknown_price(self) -> None:
        # The unknown-price exposure contract: a SKU with no priced
        # family at all leaves cost_usd None and cost_source
        # `unknown-price` so the dashboard surfaces a pricing-table
        # gap rather than a silently-wrong zero.
        stdout = _jsonl(
            _turn_complete(model="third-party-unpriced-model",
                           input=100, cached=0, output=50),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "unknown-price")
        self.assertIsNone(metrics.cost_usd)
        self.assertEqual(metrics.input_tokens, 100)
        self.assertEqual(metrics.output_tokens, 50)

    def test_no_usage_events(self) -> None:
        stdout = _jsonl(
            _task_started(),
            {"type": "thought", "text": "thinking"},
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.cost_source, "no-usage")
        self.assertIsNone(metrics.cost_usd)
        self.assertEqual(metrics.input_tokens, 0)
        self.assertEqual(metrics.output_tokens, 0)
        self.assertEqual(metrics.models, ())
        self.assertIsNone(metrics.turns)

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps(
            _turn_complete(model=GPT_5_CODEX, input=10, cached=0, output=5))
        stdout = "\n".join([
            "codex starting...",
            '{"truncated":',
            "",
            good,
            "trailing-noise",
        ])
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.input_tokens, 10)
        self.assertEqual(metrics.output_tokens, 5)
        self.assertEqual(metrics.cost_source, "estimated")

    def test_turns_falls_back_to_turn_complete_count(self) -> None:
        # When ``num_turns`` is absent, the count of ``turn_complete``
        # events is the next-best signal of how many turns ran.
        stdout = _jsonl(
            _task_started(),
            _turn_complete(model=GPT_5_CODEX, input=10, cached=0, output=5),
            _turn_complete(model=GPT_5_CODEX, input=20, cached=0, output=10),
        )
        metrics = parse_codex_usage(stdout)
        self.assertEqual(metrics.turns, 2)


class DispatcherTest(unittest.TestCase):
    """``parse_agent_usage`` is a thin dispatcher over the per-backend parsers."""

    def test_routes_claude(self) -> None:
        metrics = parse_agent_usage(CLAUDE, "")
        self.assertEqual(metrics.backend, CLAUDE)

    def test_routes_codex(self) -> None:
        metrics = parse_agent_usage(CODEX, "")
        self.assertEqual(metrics.backend, CODEX)

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_agent_usage("gemini", "")


class UsageMetricsTest(unittest.TestCase):
    def test_to_dict_round_trips_via_json(self) -> None:
        metrics = UsageMetrics(
            backend=CODEX,
            models=(GPT_5_CODEX,),
            turns=3,
            input_tokens=100,
            output_tokens=50,
            cached_tokens=10,
            cost_usd=0.01,
            cost_source="estimated",
        )
        encoded = json.dumps(metrics.to_dict(), sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["backend"], CODEX)
        self.assertEqual(decoded["models"], [GPT_5_CODEX])
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
            _system_init(),
            _assistant(content=[
                _text("reading the guide"),
                _skill_use(DEVELOP),
            ]),
            _assistant(id="msg_2", content=[
                _tool_use("Read", {"file_path": "x.py"}),
                _skill_use(REVIEW),
                _skill_use(DEVELOP),
            ]),
            _result(num_turns=2),
        )
        skills = parse_claude_skills(stdout)
        # First-seen order, de-duplicated.
        self.assertEqual(skills.triggered, (DEVELOP, REVIEW))
        # `develop` fired twice (across two messages), `review` once.
        self.assertEqual(skills.trigger_counts, {DEVELOP: 2, REVIEW: 1})
        # This `init` frame carries no `skills` array, so the offered set is
        # empty (the `available` source is read from `system/init.skills`
        # when present -- see `test_available_from_init_skills`).
        self.assertEqual(skills.available, ())

    def test_partitioned_content_frames_keep_skill(self) -> None:
        # The real capture: `--include-partial-messages` emits one `assistant`
        # frame per completed content block, all sharing the message id. The
        # content array is partitioned across them -- a text block in its own
        # frame, then the `Skill` block in the next -- NOT a cumulative
        # snapshot. The old last-frame-wins logic would drop the trigger here
        # because the trailing frame's content has no skill; walking every
        # frame keeps it.
        stdout = _jsonl(
            _assistant(content=[_skill_use(DEVELOP, id="toolu_a")]),
            _assistant(content=[_text("now I'll start")]),
            _result(num_turns=1),
        )
        skills = parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, (DEVELOP,))
        self.assertEqual(skills.trigger_counts, {DEVELOP: 1})

    def test_repeated_tool_use_id_counted_once(self) -> None:
        # Defensive: should a future stream repeat one block across frames
        # (the way the `usage` sub-object repeats), the shared `tool_use` id
        # de-dups it so a single invocation still counts once.
        stdout = _jsonl(
            _assistant(content=[_skill_use(DEVELOP, id="toolu_a")]),
            _assistant(content=[
                _skill_use(DEVELOP, id="toolu_a"),
                _skill_use(REVIEW, id="toolu_b"),
            ]),
            _result(num_turns=1),
        )
        skills = parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, (DEVELOP, REVIEW))
        self.assertEqual(skills.trigger_counts, {DEVELOP: 1, REVIEW: 1})

    def test_distinct_ids_count_repeats(self) -> None:
        # Two genuine `develop` invocations carry distinct ids -> count 2.
        stdout = _jsonl(
            _assistant(content=[_skill_use(DEVELOP, id="toolu_a")]),
            _assistant(id="msg_2", content=[_skill_use(DEVELOP, id="toolu_b")]),
        )
        skills = parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, (DEVELOP,))
        self.assertEqual(skills.trigger_counts, {DEVELOP: 2})

    def test_available_from_init_skills(self) -> None:
        # The offered set is read from the `system`/`init` frame's dedicated
        # `skills` array (confirmed against a real claude 2.1.x capture), and
        # is independent of what the run triggered: here `review` is offered
        # but never fired, while `develop` is both offered and triggered.
        stdout = _jsonl(
            _system_init(skills=[DEVELOP, REVIEW, VERIFY]),
            _assistant(content=[_skill_use(DEVELOP, id="toolu_a")]),
            _result(num_turns=1),
        )
        skills = parse_claude_skills(stdout)
        self.assertEqual(skills.available, (DEVELOP, REVIEW, VERIFY))
        self.assertEqual(skills.triggered, (DEVELOP,))
        self.assertEqual(skills.trigger_counts, {DEVELOP: 1})

    def test_available_present_without_any_trigger(self) -> None:
        # Offered-but-not-triggered: `available` populated, `triggered` empty.
        stdout = _jsonl(
            _system_init(skills=[DEVELOP, REVIEW]),
            _assistant(content=[_text("no skill used")]),
            _result(num_turns=1),
        )
        skills = parse_claude_skills(stdout)
        self.assertEqual(skills.available, (DEVELOP, REVIEW))
        self.assertEqual(skills.triggered, ())
        self.assertEqual(skills.trigger_counts, {})

    def test_available_dedups_and_filters_non_strings(self) -> None:
        # Non-string entries filter out; duplicates collapse, first-seen order.
        stdout = _jsonl(
            _system_init(skills=[DEVELOP, REVIEW, DEVELOP, 42, None, "", VERIFY]),
        )
        skills = parse_claude_skills(stdout)
        self.assertEqual(skills.available, (DEVELOP, REVIEW, VERIFY))

    def test_available_empty_without_init_skills(self) -> None:
        # An init frame with no `skills` key, a non-list `skills`, and a
        # stream with no init frame at all all yield an empty offered set,
        # never an exception.
        for frame in (
            _system_init(),
            _system_init(skills=DEVELOP),
            {"type": "system", "subtype": "status"},
        ):
            with self.subTest(frame=frame):
                skills = parse_claude_skills(_jsonl(frame))
                self.assertEqual(skills.available, ())

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps(_assistant(content=[_skill_use(DEVELOP)]))
        stdout = "\n".join([
            "starting claude...",
            '{"type":"assistant","message"',
            good,
            "",
            "not json either",
        ])
        skills = parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, (DEVELOP,))
        self.assertEqual(skills.trigger_counts, {DEVELOP: 1})

    def test_skill_free_stream_is_empty(self) -> None:
        # Text and non-Skill tool_use blocks must not register as triggers.
        stdout = _jsonl(
            _system_init(),
            _assistant(content=[
                _text("no skills here"),
                _tool_use("Read", {"file_path": "x.py"}),
            ], usage=_claude_usage(input=5, output=3)),
            _result(num_turns=1),
        )
        self.assertEqual(parse_claude_skills(stdout), SkillTriggers())

    def test_malformed_skill_blocks_are_ignored(self) -> None:
        # Missing ``input``, missing/empty ``skill``, and non-dict content
        # entries all skip silently rather than raise.
        stdout = _jsonl(
            _assistant(content=[
                {"type": "tool_use", "name": "Skill"},
                {"type": "tool_use", "name": "Skill", "input": {}},
                {"type": "tool_use", "name": "Skill", "input": {"skill": ""}},
                "not-a-block",
                _skill_use(DEVELOP),
            ]),
        )
        skills = parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, (DEVELOP,))
        self.assertEqual(skills.trigger_counts, {DEVELOP: 1})

    def test_ignores_skill_args_for_privacy(self) -> None:
        # `input.args` can echo issue / user content; only the name is read.
        secret = "user secret: api_key=sk-deadbeef"
        stdout = _jsonl(
            _assistant(content=[_skill_use(DEVELOP, args=secret)]),
        )
        skills = parse_claude_skills(stdout)
        self.assertEqual(skills.triggered, (DEVELOP,))
        self.assertEqual(skills.trigger_counts, {DEVELOP: 1})
        self.assertNotIn(secret, repr(skills))

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


def _agent_message(item_id: str, text: object, *, started: bool = False) -> dict:
    """One ``codex exec --json`` ``agent_message`` item; the last text wins."""
    frame_type = "item.started" if started else "item.completed"
    return {"type": frame_type,
            "item": {"id": item_id, "type": "agent_message", "text": text}}


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
        skills = parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, ("review",))
        # started + completed echo the same command; the shared id counts once.
        self.assertEqual(skills.trigger_counts, {"review": 1})
        self.assertEqual(skills.available, ())

    def test_started_and_completed_not_double_counted(self) -> None:
        # Explicit dedup guard: a single SKILL.md read emits two frames sharing
        # one ``item.id`` -- they must collapse to one trigger.
        cmd = "/bin/bash -lc 'cat skills/develop/SKILL.md'"
        stdout = _jsonl(
            _codex_cmd("item_2", cmd, started=True, status="in_progress"),
            _codex_cmd("item_2", cmd, status="completed", exit_code=0),
        )
        skills = parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, ("develop",))
        self.assertEqual(skills.trigger_counts, {"develop": 1})

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
        skills = parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, ("develop", "review"))
        self.assertEqual(skills.trigger_counts, {"develop": 1, "review": 1})

    def test_order_dedup_and_counts_across_separate_reads(self) -> None:
        # Distinct ``item.id``s are separate reads: a skill opened in two
        # separate commands counts twice, mirroring the claude path, while the
        # ``triggered`` tuple keeps it once in first-seen order.
        stdout = _jsonl(
            _codex_cmd("item_1", "/bin/bash -lc 'cat skills/develop/SKILL.md'"),
            _codex_cmd("item_2", "/bin/bash -lc 'cat skills/review/SKILL.md'"),
            _codex_cmd("item_3", "/bin/bash -lc 'cat skills/develop/SKILL.md'"),
        )
        skills = parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, ("develop", "review"))
        self.assertEqual(skills.trigger_counts, {"develop": 2, "review": 1})

    def test_multiple_skills_in_one_command(self) -> None:
        # One command that opens two SKILL.md files records both, in order.
        stdout = _jsonl(
            _codex_cmd("item_1",
                       "/bin/bash -lc 'cat skills/review/SKILL.md "
                       "skills/develop/SKILL.md'"),
        )
        skills = parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, ("review", "develop"))
        self.assertEqual(skills.trigger_counts, {"review": 1, "develop": 1})

    def test_skill_free_usage_stream_is_empty(self) -> None:
        # A normal run (thread/turn frames, an agent message, a usage-bearing
        # turn.completed, and ordinary command_execution items that touch no
        # SKILL.md) carries no skill trigger; the parser must not false-positive.
        stdout = _jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            _codex_cmd("item_1", "/bin/bash -lc 'git diff -- calc.py'"),
            _agent_message("item_2", "Approve."),
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
        skills = parse_codex_skills(stdout)
        self.assertEqual(skills, SkillTriggers())
        self.assertNotIn(leaked, repr(skills))
        self.assertNotIn("leaked", repr(skills))

    def test_only_the_name_segment_is_captured_for_privacy(self) -> None:
        # The command around the SKILL.md read can carry issue / user content;
        # only the `<name>` path segment is ever extracted, never the rest.
        secret = "user secret: api_key=sk-deadbeef"
        stdout = _jsonl(
            _codex_cmd("item_1",
                       "/bin/bash -lc \"cat skills/review/SKILL.md; "
                       f"echo '{secret}'\""),
        )
        skills = parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, ("review",))
        self.assertNotIn(secret, repr(skills))
        self.assertNotIn("sk-deadbeef", repr(skills))

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps(
            _codex_cmd("item_1", "/bin/bash -lc 'cat skills/develop/SKILL.md'"))
        stdout = "\n".join([
            "codex starting...",
            '{"truncated":',
            good,
            "trailing-noise",
        ])
        skills = parse_codex_skills(stdout)
        self.assertEqual(skills.triggered, ("develop",))
        self.assertEqual(skills.trigger_counts, {"develop": 1})

    def test_empty_stdout(self) -> None:
        self.assertEqual(parse_codex_skills(""), SkillTriggers())


class SkillDispatcherTest(unittest.TestCase):
    """``parse_agent_skills`` routes by backend, mirroring ``parse_agent_usage``."""

    def test_routes_claude(self) -> None:
        # An assistant/tool_use stream is recognized only by the claude path.
        stdout = _jsonl(_assistant(id="m", content=[_skill_use(DEVELOP)]))
        self.assertEqual(parse_agent_skills(CLAUDE, stdout).triggered,
                         (DEVELOP,))

    def test_routes_codex(self) -> None:
        # A codex SKILL.md-read command_execution is recognized only by the
        # codex path; the claude parser returns empty on it, so a non-empty
        # result here proves the codex parser ran.
        stdout = _jsonl(_codex_cmd(
            "item_1", "/bin/bash -lc 'cat skills/review/SKILL.md'"))
        self.assertEqual(parse_agent_skills(CODEX, stdout).triggered,
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
            _system_init(tools=["Bash", "Read", "Skill"],
                         skills=[DEVELOP, REVIEW]),
            _assistant(content=[
                _text("let me look"),
                _tool_use("Bash", {"command": "ls"}, id="toolu_a"),
            ]),
            _user([_tool_result("toolu_a", "calc.py\n")]),
            _assistant(id="msg_2", content=[_skill_use(DEVELOP, id="toolu_b")]),
            _result(result="All done.", num_turns=2),
        )
        trajectory = parse_claude_trajectory(stdout)
        self.assertEqual(trajectory.backend, CLAUDE)
        self.assertIsNone(trajectory.system_prompt)
        self.assertEqual(trajectory.tools, ("Bash", "Read", "Skill"))
        self.assertEqual(trajectory.final_output, "All done.")
        # Skills reuse the names-only extractor (offered + triggered).
        self.assertEqual(trajectory.skills.available, (DEVELOP, REVIEW))
        self.assertEqual(trajectory.skills.triggered, (DEVELOP,))
        # Ordered timeline: assistant text -> call -> result -> call. The two
        # msg_1 steps share turn 0; the msg_2 call is turn 1; the tool_result
        # is a turn input and carries no turn index.
        self.assertEqual(len(trajectory.steps), 4)
        self.assertEqual(
            trajectory.steps[0],
            TrajectoryStep(kind="assistant_message", turn=0,
                           content="let me look"),
        )
        self.assertEqual(
            trajectory.steps[1],
            TrajectoryStep(kind="tool_call", name="Bash", tool_id="toolu_a",
                           turn=0, content={"command": "ls"}),
        )
        self.assertEqual(
            trajectory.steps[2],
            TrajectoryStep(kind="tool_result", tool_id="toolu_a",
                           content="calc.py\n"),
        )
        self.assertEqual(
            trajectory.steps[3],
            TrajectoryStep(kind="tool_call", name="Skill", tool_id="toolu_b",
                           turn=1, content={"skill": DEVELOP}),
        )

    def test_captures_assistant_and_user_text_turns_in_stream_order(
        self,
    ) -> None:
        # Full timeline: an assistant text turn, then a tool call + its
        # result and a user text turn in the same user message, then a closing
        # assistant text turn -- text turns are preserved inline with the tool
        # steps, in stream order, alongside the unchanged final output.
        stdout = _jsonl(
            _assistant(id="m1", content=[
                _text("let me check"),
                _tool_use("Read", {"file_path": "x.py"}, id="tu1"),
            ]),
            _user([
                _tool_result("tu1", "file body"),
                _text("now fix it"),
            ]),
            _assistant(id="m2", content=[_text("done")]),
            _result(result="all set"),
        )
        trajectory = parse_claude_trajectory(stdout)
        self.assertEqual(
            [(step.kind, step.content) for step in trajectory.steps],
            [
                ("assistant_message", "let me check"),
                ("tool_call", {"file_path": "x.py"}),
                ("tool_result", "file body"),
                ("user_message", "now fix it"),
                ("assistant_message", "done"),
            ],
        )
        # Text turns carry no tool name / id.
        first = trajectory.steps[0]
        self.assertEqual(first.name, "")
        self.assertEqual(first.tool_id, "")
        self.assertEqual(trajectory.final_output, "all set")

    def test_empty_or_nonstring_text_blocks_are_skipped(self) -> None:
        # An empty / missing / non-string text block does not create a
        # message step -- only non-empty string text turns are captured.
        stdout = _jsonl(
            _assistant(id="m", content=[
                _text(""),
                {"type": "text"},
                _text(7)]),
            _user([_text("")]),
        )
        self.assertEqual(parse_claude_trajectory(stdout).steps, ())

    def test_partial_frames_dedup_calls_and_results(self) -> None:
        # Defensive: a tool_use / tool_result block repeated across frames
        # (sharing its id) is one step, not two -- the same per-id de-dup
        # ``parse_claude_skills`` applies. Distinct ids stay distinct.
        stdout = _jsonl(
            _assistant(content=[
                _tool_use("Bash", {"command": "ls"}, id="toolu_a")]),
            _assistant(content=[
                _tool_use("Bash", {"command": "ls"}, id="toolu_a")]),
            _user([_tool_result("toolu_a", "out")]),
            _user([_tool_result("toolu_a", "out")]),
        )
        trajectory = parse_claude_trajectory(stdout)
        self.assertEqual([step.kind for step in trajectory.steps],
                         ["tool_call", "tool_result"])

    def test_missing_fields_yield_empty_sections(self) -> None:
        # No init frame, no capturable blocks, no result frame: every section
        # is empty / None, never an exception.
        stdout = _jsonl(
            _assistant(id="m", content=[]),
        )
        trajectory = parse_claude_trajectory(stdout)
        self.assertEqual(trajectory.tools, ())
        self.assertEqual(trajectory.steps, ())
        self.assertIsNone(trajectory.final_output)
        self.assertIsNone(trajectory.system_prompt)
        self.assertEqual(trajectory.skills, SkillTriggers())

    def test_malformed_lines_are_skipped(self) -> None:
        good = json.dumps(_assistant(id="m", content=[
            _tool_use("Read", {"file_path": "x.py"}, id="toolu_a")]))
        stdout = "\n".join([
            "starting claude...",
            '{"type":"assistant","message"',
            good,
            "not json either",
        ])
        trajectory = parse_claude_trajectory(stdout)
        self.assertEqual(len(trajectory.steps), 1)
        self.assertEqual(trajectory.steps[0].name, "Read")

    def test_empty_stdout(self) -> None:
        self.assertEqual(parse_claude_trajectory(""),
                         AgentTrajectory(backend=CLAUDE))


class ClaudeTurnUsageTest(unittest.TestCase):
    """Per-turn token usage from ``parse_claude_trajectory``.

    Tokens are billed per assistant turn (one ``message.id``), not per timeline
    step: a turn's ``text`` and ``tool_use`` blocks share one ``usage`` record,
    so usage rides on ``AgentTrajectory.turns`` -- one ``TurnUsage`` per turn --
    while every ``assistant_message`` / ``tool_call`` step carries the same
    ``turn`` index. Per-turn cost is always an estimate from the shared price
    path; ``tool_result`` / ``user_message`` steps are turn inputs (``turn``
    ``None``).
    """

    def test_turn_indexes_span_multi_tool_turns_and_per_turn_model(
        self,
    ) -> None:
        # A single assistant message with a text block and two tool_use blocks
        # is one turn: all three steps share turn 0 and there is one TurnUsage
        # for it, with the cache read/write split and a per-turn estimated cost
        # priced from that turn's own model. A second message is turn 1, priced
        # from its own (different) model; the interleaved tool_result steps are
        # turn inputs and carry no index.
        stdout = _jsonl(
            _system_init(tools=["Bash", "Edit"]),
            _assistant(id="msg_0", model=SONNET, content=[
                _text("working"),
                _tool_use("Bash", {"command": "ls"}, id="t1"),
                _tool_use("Edit", {"file_path": "a.py"}, id="t2"),
            ], usage=_claude_usage(
                input=CLAUDE_TURN_INPUT_TOKENS,
                cache_write=CLAUDE_TURN_CACHE_WRITE_TOKENS,
                cache_read=CLAUDE_TURN_CACHE_READ_TOKENS,
                output=CLAUDE_TURN_OUTPUT_TOKENS,
            )),
            _user([
                _tool_result("t1", "o1"),
                _tool_result("t2", "o2"),
            ]),
            _assistant(id="msg_1", model=HAIKU, content=[_text("done")],
                       usage=_claude_usage(input=20, output=50)),
            _result(result="ok", num_turns=2),
        )
        trajectory = parse_claude_trajectory(stdout)
        self.assertEqual(
            [(step.kind, step.turn) for step in trajectory.steps],
            [("assistant_message", 0), ("tool_call", 0), ("tool_call", 0),
             ("tool_result", None), ("tool_result", None),
             ("assistant_message", 1)],
        )
        self.assertEqual(len(trajectory.turns), 2)
        turn0, turn1 = trajectory.turns
        # sonnet: input=3, cw5m=3.75, cr=0.30, output=15 (per 1M).
        expected0 = (
            CLAUDE_TURN_INPUT_TOKENS * 3
            + CLAUDE_TURN_CACHE_WRITE_TOKENS * 3.75
            + CLAUDE_TURN_CACHE_READ_TOKENS * 0.30
            + CLAUDE_TURN_OUTPUT_TOKENS * 15
        ) / TOKENS_PER_MILLION
        self.assertEqual(
            turn0,
            TurnUsage(
                turn=0,
                model=SONNET,
                input_tokens=CLAUDE_TURN_INPUT_TOKENS,
                output_tokens=CLAUDE_TURN_OUTPUT_TOKENS,
                cache_read_tokens=CLAUDE_TURN_CACHE_READ_TOKENS,
                cache_write_tokens=CLAUDE_TURN_CACHE_WRITE_TOKENS,
                cost_usd=turn0.cost_usd,
                cost_source="estimated",
            ),
        )
        assert turn0.cost_usd is not None
        self.assertAlmostEqual(turn0.cost_usd, expected0, places=12)
        # haiku-3-5: input=0.80, output=4 (per 1M).
        self.assertEqual(turn1.turn, 1)
        self.assertEqual(turn1.model, HAIKU)
        self.assertEqual(turn1.cache_read_tokens, 0)
        self.assertEqual(turn1.cache_write_tokens, 0)
        expected1 = (20 * 0.80 + 50 * 4) / TOKENS_PER_MILLION
        assert turn1.cost_usd is not None
        self.assertAlmostEqual(turn1.cost_usd, expected1, places=12)

    def test_structured_cache_creation_sums_into_cache_write(self) -> None:
        # The structured 5m/1h cache-creation form sums into a single per-turn
        # cache_write_tokens while both still price at their own rate.
        stdout = _jsonl(
            _assistant(id="msg_0", model=OPUS_4_7, content=[_text("hi")],
                       usage=_claude_usage(input=0, cache_5m=1000, cache_1h=500,
                                           output=100)),
        )
        turn0 = parse_claude_trajectory(stdout).turns[0]
        self.assertEqual(turn0.cache_write_tokens, 1500)
        # opus-4-7: input=5, cw5m=6.25, cw1h=10, cr=0.50, output=25.
        expected = (1000 * 6.25 + 500 * 10 + 100 * 25) / TOKENS_PER_MILLION
        assert turn0.cost_usd is not None
        self.assertAlmostEqual(turn0.cost_usd, expected, places=12)

    def test_partial_message_frames_are_one_turn_last_record_wins(self) -> None:
        # Two frames sharing a message.id are one turn; the last usage record is
        # authoritative (claude streams partial usage on intermediate frames).
        # Both frames' steps carry the single turn 0.
        stdout = _jsonl(
            _assistant(id="msg_0", model=OPUS_4_7, content=[_text("partial")],
                       usage=_claude_usage(input=5, output=10, cache_read=100)),
            _assistant(id="msg_0", model=OPUS_4_7, content=[
                _tool_use("Bash", {"command": "ls"}, id="t1")],
                       usage=_claude_usage(input=8, output=40, cache_read=200)),
        )
        trajectory = parse_claude_trajectory(stdout)
        self.assertEqual([step.turn for step in trajectory.steps], [0, 0])
        self.assertEqual(len(trajectory.turns), 1)
        self.assertEqual(trajectory.turns[0].input_tokens, 8)
        self.assertEqual(trajectory.turns[0].output_tokens, 40)
        self.assertEqual(trajectory.turns[0].cache_read_tokens, 200)

    def test_unpriced_model_turn_is_unknown_price(self) -> None:
        # A turn whose model has no first-party rate is unknown-price with a
        # None cost -- never a silent zero -- while its token counts still land.
        stdout = _jsonl(
            _assistant(id="msg_0", model=UNKNOWN_MODEL, content=[_text("hi")],
                       usage=_claude_usage(input=100, output=200)),
        )
        turn0 = parse_claude_trajectory(stdout).turns[0]
        self.assertEqual(turn0.cost_source, "unknown-price")
        self.assertIsNone(turn0.cost_usd)
        self.assertEqual(turn0.model, UNKNOWN_MODEL)
        self.assertEqual(turn0.input_tokens, 100)
        self.assertEqual(turn0.output_tokens, 200)

    def test_per_turn_cost_is_estimated_even_when_run_reports_cost(
        self,
    ) -> None:
        # total_cost_usd is a run-level terminal figure; per-turn cost is always
        # an estimate and never inherits the reported source or value, even
        # though the run aggregate still surfaces the authoritative total.
        stdout = _jsonl(
            _assistant(id="msg_0", model=SONNET, content=[_text("hi")],
                       usage=_claude_usage(input=100, output=200)),
            _result(total_cost_usd=9.99, num_turns=1),
        )
        turn0 = parse_claude_trajectory(stdout).turns[0]
        self.assertEqual(turn0.cost_source, "estimated")
        self.assertNotEqual(turn0.cost_usd, 9.99)
        self.assertEqual(parse_claude_usage(stdout).cost_usd, 9.99)

    def test_per_turn_estimates_sum_to_run_estimate(self) -> None:
        # The shared _claude_estimate_cost feeds both the per-turn builder and
        # the run aggregate; because pricing is linear in token counts, the
        # per-turn estimates sum to the run-level estimated cost. Regression
        # guard that factoring the estimator out left run totals unchanged and
        # in lock-step with the per-turn numbers.
        stdout = _jsonl(
            _assistant(id="msg_0", model=SONNET, content=[_text("a")],
                       usage=_claude_usage(input=100, cache_write=200,
                                           cache_read=300, output=50)),
            _assistant(id="msg_1", model=HAIKU, content=[_text("b")],
                       usage=_claude_usage(input=400, output=20)),
        )
        run = parse_claude_usage(stdout)
        turns = parse_claude_trajectory(stdout).turns
        self.assertEqual(run.cost_source, "estimated")
        self.assertEqual(len(turns), 2)
        total = sum(turn.cost_usd for turn in turns)
        assert run.cost_usd is not None
        self.assertAlmostEqual(run.cost_usd, total, places=12)

    def test_no_usage_frames_yield_no_turns(self) -> None:
        # A stream whose assistant frames carry no usage block produces no
        # TurnUsage, and the steps it does emit fall back to turn 0 by
        # first-seen message.id -- no exception.
        stdout = _jsonl(
            _assistant(id="msg_0", content=[_text("hi")]),
        )
        trajectory = parse_claude_trajectory(stdout)
        self.assertEqual(trajectory.turns, ())
        self.assertEqual([step.turn for step in trajectory.steps], [0])


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
            _agent_message("item_3", "Approve."),
        )
        trajectory = parse_codex_trajectory(stdout)
        self.assertEqual(trajectory.backend, CODEX)
        self.assertIsNone(trajectory.system_prompt)
        self.assertEqual(trajectory.tools, ())
        self.assertEqual(trajectory.final_output, "Approve.")
        # SKILL.md read surfaces in the names-only skills extractor.
        self.assertEqual(trajectory.skills.triggered, ("develop",))
        # started + completed for item_1 collapse to one call + one result;
        # the trailing agent_message rides along as an assistant_message turn
        # (and is also the final output).
        self.assertEqual(
            trajectory.steps,
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
            _agent_message("a1", "starting"),
            _codex_cmd("c1", "/bin/bash -lc 'ls'", status="completed",
                       exit_code=0, aggregated_output="out\n"),
            _agent_message("a2", "all done"),
        )
        trajectory = parse_codex_trajectory(stdout)
        self.assertEqual(
            [(step.kind, step.content) for step in trajectory.steps],
            [
                ("assistant_message", "starting"),
                ("tool_call", "/bin/bash -lc 'ls'"),
                ("tool_result", "out\n"),
                ("assistant_message", "all done"),
            ],
        )
        self.assertEqual(trajectory.final_output, "all done")

    def test_agent_message_started_and_completed_collapse(self) -> None:
        # A started + completed agent_message sharing an item.id is one turn
        # (last text wins), mirroring the command started/completed collapse.
        stdout = _jsonl(
            _agent_message("a1", "partial", started=True),
            _agent_message("a1", "final text"),
        )
        trajectory = parse_codex_trajectory(stdout)
        self.assertEqual(
            trajectory.steps,
            (TrajectoryStep(kind="assistant_message", content="final text"),),
        )
        self.assertEqual(trajectory.final_output, "final text")

    def test_empty_or_nonstring_agent_message_is_skipped(self) -> None:
        # An empty / non-string agent_message text creates no turn.
        stdout = _jsonl(
            _agent_message("a1", ""),
            _agent_message("a2", 7),
        )
        self.assertEqual(parse_codex_trajectory(stdout).steps, ())

    def test_started_only_command_emits_call_without_result(self) -> None:
        # A command that never completes (no aggregated_output) is a call with
        # no result step rather than a fabricated empty result.
        stdout = _jsonl(
            _codex_cmd("item_1", "/bin/bash -lc 'sleep 99'",
                       started=True, status="in_progress"),
        )
        trajectory = parse_codex_trajectory(stdout)
        self.assertEqual(len(trajectory.steps), 1)
        self.assertEqual(trajectory.steps[0].kind, "tool_call")
        self.assertEqual(trajectory.steps[0].tool_id, "item_1")

    def test_missing_fields_yield_empty_sections(self) -> None:
        stdout = _jsonl(
            {"type": "thread.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        )
        trajectory = parse_codex_trajectory(stdout)
        self.assertEqual(trajectory.steps, ())
        self.assertIsNone(trajectory.final_output)
        self.assertEqual(trajectory.tools, ())
        self.assertEqual(trajectory.skills, SkillTriggers())

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
        trajectory = parse_codex_trajectory(stdout)
        self.assertEqual([step.kind for step in trajectory.steps],
                         ["tool_call", "tool_result"])

    def test_has_no_per_turn_usage(self) -> None:
        # Codex usage frames are cumulative, not per-turn, so the per-turn
        # section stays empty and no step is stamped with a turn index -- the
        # run-level summary is codex's only usage surface (mirrors how tools /
        # skills_available stay best-effort-empty for codex).
        stdout = _jsonl(
            _codex_cmd("item_1", "/bin/bash -lc 'ls'", status="completed",
                       aggregated_output="out\n"),
            _agent_message("a1", "done"),
            {"type": "turn.completed", "usage": {"input_tokens": 10,
                                                 "output_tokens": 5}},
        )
        trajectory = parse_codex_trajectory(stdout)
        self.assertEqual(trajectory.turns, ())
        self.assertTrue(trajectory.steps)
        self.assertTrue(all(step.turn is None for step in trajectory.steps))

    def test_empty_stdout(self) -> None:
        self.assertEqual(parse_codex_trajectory(""),
                         AgentTrajectory(backend=CODEX))


class TrajectoryDispatcherTest(unittest.TestCase):
    """``parse_agent_trajectory`` routes by backend, mirroring the siblings."""

    def test_routes_claude(self) -> None:
        self.assertEqual(parse_agent_trajectory(CLAUDE, "").backend, CLAUDE)

    def test_routes_codex(self) -> None:
        self.assertEqual(parse_agent_trajectory(CODEX, "").backend, CODEX)

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_agent_trajectory("gemini", "")


class AgentTrajectoryTest(unittest.TestCase):
    def test_to_dict_round_trips_via_json(self) -> None:
        trajectory = AgentTrajectory(
            backend=CLAUDE,
            tools=("Bash", "Read"),
            skills=SkillTriggers(triggered=(DEVELOP,),
                                 trigger_counts={DEVELOP: 1},
                                 available=(DEVELOP, REVIEW)),
            steps=(
                TrajectoryStep(kind="tool_call", name="Bash", tool_id="t1",
                               turn=0, content={"command": "ls"}),
                TrajectoryStep(kind="tool_result", tool_id="t1",
                               content="out"),
            ),
            final_output="done",
            turns=(
                TurnUsage(
                    turn=0,
                    model=OPUS_4_8,
                    input_tokens=CLAUDE_TURN_INPUT_TOKENS,
                    output_tokens=CLAUDE_TURN_OUTPUT_TOKENS,
                    cache_read_tokens=CLAUDE_TURN_CACHE_READ_TOKENS,
                    cache_write_tokens=CLAUDE_TURN_CACHE_WRITE_TOKENS,
                    cost_usd=0.0123,
                    cost_source="estimated",
                ),
            ),
        )
        encoded = json.dumps(trajectory.to_dict(), sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["backend"], CLAUDE)
        self.assertEqual(decoded["tools"], ["Bash", "Read"])
        self.assertEqual(decoded["system_prompt"], None)
        self.assertEqual(decoded["final_output"], "done")
        self.assertEqual(decoded["skills"]["triggered"], [DEVELOP])
        self.assertEqual(decoded["skills"]["available"], [DEVELOP, REVIEW])
        self.assertEqual(len(decoded["steps"]), 2)
        self.assertEqual(decoded["steps"][0]["name"], "Bash")
        self.assertEqual(decoded["steps"][0]["turn"], 0)
        self.assertEqual(decoded["steps"][1]["kind"], "tool_result")
        self.assertIsNone(decoded["steps"][1]["turn"])
        self.assertEqual(len(decoded["turns"]), 1)
        self.assertEqual(decoded["turns"][0]["model"], OPUS_4_8)
        self.assertEqual(
            decoded["turns"][0]["cache_read_tokens"],
            CLAUDE_TURN_CACHE_READ_TOKENS,
        )
        self.assertEqual(decoded["turns"][0]["cost_source"], "estimated")


if __name__ == "__main__":
    unittest.main()
