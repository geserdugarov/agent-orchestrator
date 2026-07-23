# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused provider usage parsing tests."""

import unittest

from orchestrator import usage as _usage
from tests import usage_assertions as _assertions
from tests import usage_test_values as _usage_cases
from tests import usage_jsonl_helpers as _jsonl
from tests import usage_pricing_cases as _pricing
from tests import usage_claude_events as _claude


class ClaudeTurnUsageTest(unittest.TestCase):
    """Per-turn token usage from ``_usage.parse_claude_trajectory``.

    Tokens are billed per assistant turn (one ``message.id``), not per timeline
    step: a turn's ``text`` and ``tool_use`` blocks share one ``usage`` record,
    so usage rides on ``_usage.AgentTrajectory.turns`` -- one ``_usage.TurnUsage`` per turn --
    while every ``assistant_message`` / ``tool_call`` step carries the same
    ``turn`` index. Per-turn cost is always an estimate from the shared price
    path; ``tool_result`` / ``user_message`` steps are turn inputs (``turn``
    ``None``).
    """

    def test_indexes_span_tool_turns_and_models(
        self,
    ) -> None:
        # A single assistant message with a text block and two tool_use blocks
        # is one turn: all three steps share turn 0 and there is one _usage.TurnUsage
        # for it, with the cache read/write split and a per-turn estimated cost
        # priced from that turn's own model. A second message is turn 1, priced
        # from its own (different) model; the interleaved tool_result steps are
        # turn inputs and carry no index.
        stdout = _jsonl.jsonl(
            _claude.system_init(tools=[_usage_cases.BASH_TOOL, "Edit"]),
            _claude.assistant(
                id=_usage_cases.CLAUDE_TURN_ID,
                model=_usage_cases.SONNET,
                content_blocks=[
                    _jsonl.text("working"),
                    _jsonl.tool_use(
                        _usage_cases.BASH_TOOL,
                        {_usage_cases.COMMAND_FIELD: _usage_cases.LIST_COMMAND},
                        id=_usage_cases.TASK_ONE_ID,
                    ),
                    _jsonl.tool_use("Edit", {_usage_cases.FILE_PATH_FIELD: "a.py"}, id="t2"),
                ],
                usage=_claude.usage(
                    input=_usage_cases.CLAUDE_TURN_INPUT_TOKENS,
                    cache_write=_usage_cases.CLAUDE_TURN_CACHE_WRITE_TOKENS,
                    cache_read=_usage_cases.CLAUDE_TURN_CACHE_READ_TOKENS,
                    output=_usage_cases.CLAUDE_TURN_OUTPUT_TOKENS,
                ),
            ),
            _jsonl.user(
                [
                    _jsonl.tool_result(_usage_cases.TASK_ONE_ID, "o1"),
                    _jsonl.tool_result("t2", "o2"),
                ]
            ),
            _claude.assistant(
                id="msg_1",
                model=_usage_cases.HAIKU,
                content_blocks=[_jsonl.text(_usage_cases.FINAL_OUTPUT)],
                usage=_claude.usage(
                    input=_usage_cases.HAIKU_TURN_INPUT_TOKENS,
                    output=_usage_cases.HAIKU_TURN_OUTPUT_TOKENS,
                ),
            ),
            _claude.terminal_result(result="ok", num_turns=2),
        )
        trajectory = _usage.parse_claude_trajectory(stdout)
        self.assertEqual(
            [(step.kind, step.turn) for step in trajectory.steps],
            [
                (_usage_cases.ASSISTANT_MESSAGE_STEP, 0),
                (_usage_cases.TOOL_CALL_STEP, 0),
                (_usage_cases.TOOL_CALL_STEP, 0),
                (_usage_cases.TOOL_RESULT_STEP, None),
                (_usage_cases.TOOL_RESULT_STEP, None),
                (_usage_cases.ASSISTANT_MESSAGE_STEP, 1),
            ],
        )
        self.assertEqual(len(trajectory.turns), 2)
        turn0, turn1 = trajectory.turns
        # sonnet: input=3, cw5m=3.75, cr=0.30, output=15 (per 1M).
        self.assertEqual(
            turn0,
            _usage.TurnUsage(
                turn=0,
                model=_usage_cases.SONNET,
                input_tokens=_usage_cases.CLAUDE_TURN_INPUT_TOKENS,
                output_tokens=_usage_cases.CLAUDE_TURN_OUTPUT_TOKENS,
                cache_read_tokens=_usage_cases.CLAUDE_TURN_CACHE_READ_TOKENS,
                cache_write_tokens=_usage_cases.CLAUDE_TURN_CACHE_WRITE_TOKENS,
                cost_usd=turn0.cost_usd,
                cost_source=_usage_cases.ESTIMATED_COST_SOURCE,
            ),
        )
        _assertions.assert_cost(
            self,
            turn0,
            _pricing.sonnet_turn_cost(),
            places=_usage_cases.COST_ASSERT_PLACES,
        )
        # haiku-3-5: input=0.80, output=4 (per 1M).
        self.assertEqual(turn1.turn, 1)
        self.assertEqual(turn1.model, _usage_cases.HAIKU)
        self.assertEqual(turn1.cache_read_tokens, 0)
        self.assertEqual(turn1.cache_write_tokens, 0)
        _assertions.assert_cost(
            self,
            turn1,
            _pricing.haiku_turn_cost(),
            places=_usage_cases.COST_ASSERT_PLACES,
        )

    def test_cache_creation_adds_to_cache_write(self) -> None:
        # The structured 5m/1h cache-creation form sums into a single per-turn
        # cache_write_tokens while both still price at their own rate.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                id=_usage_cases.CLAUDE_TURN_ID,
                model=_usage_cases.OPUS_FOUR_SEVEN,
                content_blocks=[_jsonl.text(_usage_cases.GREETING_TEXT)],
                usage=_claude.usage(
                    input=0,
                    cache_five_minute=_usage_cases.CLAUDE_FIVE_MINUTE_CACHE_TOKENS,
                    cache_one_hour=_usage_cases.CLAUDE_ONE_HOUR_CACHE_TOKENS,
                    output=100,
                ),
            ),
        )
        turn0 = _usage.parse_claude_trajectory(stdout).turns[0]
        self.assertEqual(
            turn0.cache_write_tokens,
            _usage_cases.CLAUDE_COMBINED_CACHE_WRITE_TOKENS,
        )
        # opus-4-7: input=5, cw5m=6.25, cw1h=10, cr=0.50, output=25.
        expected = (
            _usage_cases.CLAUDE_FIVE_MINUTE_CACHE_TOKENS * _usage_cases.PRICE_RATE_SIX_AND_QUARTER
            + _usage_cases.CLAUDE_ONE_HOUR_CACHE_TOKENS * 10
            + 100 * _usage_cases.PRICE_RATE_TWENTY_FIVE
        ) / _usage_cases.TOKENS_PER_MILLION
        assert turn0.cost_usd is not None
        self.assertAlmostEqual(
            turn0.cost_usd,
            expected,
            places=_usage_cases.COST_ASSERT_PLACES,
        )

    def test_partial_frames_use_last_record_per_turn(self) -> None:
        # Two frames sharing a message.id are one turn; the last usage record is
        # authoritative (claude streams partial usage on intermediate frames).
        # Both frames' steps carry the single turn 0.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                id=_usage_cases.CLAUDE_TURN_ID,
                model=_usage_cases.OPUS_FOUR_SEVEN,
                content_blocks=[_jsonl.text("partial")],
                usage=_claude.usage(input=5, output=10, cache_read=100),
            ),
            _claude.assistant(
                id=_usage_cases.CLAUDE_TURN_ID,
                model=_usage_cases.OPUS_FOUR_SEVEN,
                content_blocks=[
                    _jsonl.tool_use(
                        _usage_cases.BASH_TOOL,
                        {_usage_cases.COMMAND_FIELD: _usage_cases.LIST_COMMAND},
                        id=_usage_cases.TASK_ONE_ID,
                    ),
                ],
                usage=_claude.usage(
                    input=8,
                    output=_usage_cases.PARTIAL_TURN_OUTPUT_TOKENS,
                    cache_read=_usage_cases.PARTIAL_TURN_CACHE_READ_TOKENS,
                ),
            ),
        )
        trajectory = _usage.parse_claude_trajectory(stdout)
        self.assertEqual([step.turn for step in trajectory.steps], [0, 0])
        self.assertEqual(len(trajectory.turns), 1)
        self.assertEqual(trajectory.turns[0].input_tokens, 8)
        self.assertEqual(
            trajectory.turns[0].output_tokens,
            _usage_cases.PARTIAL_TURN_OUTPUT_TOKENS,
        )
        self.assertEqual(
            trajectory.turns[0].cache_read_tokens,
            _usage_cases.PARTIAL_TURN_CACHE_READ_TOKENS,
        )

    def test_unpriced_model_turn_is_unknown_price(self) -> None:
        # A turn whose model has no first-party rate is unknown-price with a
        # None cost -- never a silent zero -- while its token counts still land.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                id=_usage_cases.CLAUDE_TURN_ID,
                model=_usage_cases.UNKNOWN_MODEL,
                content_blocks=[_jsonl.text(_usage_cases.GREETING_TEXT)],
                usage=_claude.usage(input=100, output=_usage_cases.TOKEN_COUNT_TWO_HUNDRED),
            ),
        )
        turn0 = _usage.parse_claude_trajectory(stdout).turns[0]
        self.assertEqual(turn0.cost_source, _usage_cases.UNKNOWN_COST_SOURCE)
        self.assertIsNone(turn0.cost_usd)
        self.assertEqual(turn0.model, _usage_cases.UNKNOWN_MODEL)
        self.assertEqual(turn0.input_tokens, 100)
        self.assertEqual(turn0.output_tokens, _usage_cases.TOKEN_COUNT_TWO_HUNDRED)

    def test_turn_cost_estimated_when_run_has_cost(
        self,
    ) -> None:
        # total_cost_usd is a run-level terminal figure; per-turn cost is always
        # an estimate and never inherits the reported source or value, even
        # though the run aggregate still surfaces the authoritative total.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                id=_usage_cases.CLAUDE_TURN_ID,
                model=_usage_cases.SONNET,
                content_blocks=[_jsonl.text(_usage_cases.GREETING_TEXT)],
                usage=_claude.usage(input=100, output=_usage_cases.TOKEN_COUNT_TWO_HUNDRED),
            ),
            _claude.terminal_result(
                total_cost_usd=_usage_cases.TRAJECTORY_REPORTED_COST_USD,
                num_turns=1,
            ),
        )
        turn0 = _usage.parse_claude_trajectory(stdout).turns[0]
        self.assertEqual(turn0.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        self.assertNotEqual(turn0.cost_usd, _usage_cases.TRAJECTORY_REPORTED_COST_USD)
        self.assertEqual(
            _usage.parse_claude_usage(stdout).cost_usd,
            _usage_cases.TRAJECTORY_REPORTED_COST_USD,
        )

    def test_per_turn_estimates_sum_to_run_estimate(self) -> None:
        # The shared _claude_estimate_cost feeds both the per-turn builder and
        # the run aggregate; because pricing is linear in token counts, the
        # per-turn estimates sum to the run-level estimated cost. Regression
        # guard that factoring the estimator out left run totals unchanged and
        # in lock-step with the per-turn numbers.
        stdout = _jsonl.jsonl(
            _claude.assistant(
                id=_usage_cases.CLAUDE_TURN_ID,
                model=_usage_cases.SONNET,
                content_blocks=[_jsonl.text("a")],
                usage=_claude.usage(
                    input=100,
                    cache_write=_usage_cases.TOKEN_COUNT_TWO_HUNDRED,
                    cache_read=_usage_cases.TOKEN_COUNT_THREE_HUNDRED,
                    output=_usage_cases.TOKEN_COUNT_FIFTY,
                ),
            ),
            _claude.assistant(
                id="msg_1",
                model=_usage_cases.HAIKU,
                content_blocks=[_jsonl.text("b")],
                usage=_claude.usage(
                    input=_usage_cases.TOKEN_COUNT_FOUR_HUNDRED, output=_usage_cases.TOKEN_COUNT_TWENTY
                ),
            ),
        )
        run = _usage.parse_claude_usage(stdout)
        turns = _usage.parse_claude_trajectory(stdout).turns
        self.assertEqual(run.cost_source, _usage_cases.ESTIMATED_COST_SOURCE)
        self.assertEqual(len(turns), 2)
        total = sum(turn.cost_usd for turn in turns)
        assert run.cost_usd is not None
        self.assertAlmostEqual(
            run.cost_usd,
            total,
            places=_usage_cases.COST_ASSERT_PLACES,
        )

    def test_no_usage_frames_yield_no_turns(self) -> None:
        # A stream whose assistant frames carry no usage block produces no
        # _usage.TurnUsage, and the steps it does emit fall back to turn 0 by
        # first-seen message.id -- no exception.
        stdout = _jsonl.jsonl(
            _claude.assistant(id=_usage_cases.CLAUDE_TURN_ID, content_blocks=[_jsonl.text(_usage_cases.GREETING_TEXT)]),
        )
        trajectory = _usage.parse_claude_trajectory(stdout)
        self.assertEqual(trajectory.turns, ())
        self.assertEqual([step.turn for step in trajectory.steps], [0])
