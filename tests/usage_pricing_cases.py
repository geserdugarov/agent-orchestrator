# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Expected pricing cases for Claude per-turn usage tests."""

from tests import usage_test_values as _usage_cases


def sonnet_turn_cost() -> float:
    return (
        _usage_cases.CLAUDE_TURN_INPUT_TOKENS * 3
        + _usage_cases.CLAUDE_TURN_CACHE_WRITE_TOKENS * _usage_cases.PRICE_RATE_THREE_AND_THREE_QUARTERS
        + _usage_cases.CLAUDE_TURN_CACHE_READ_TOKENS * _usage_cases.PRICE_RATE_THREE_TENTHS
        + _usage_cases.CLAUDE_TURN_OUTPUT_TOKENS * _usage_cases.PRICE_RATE_FIFTEEN
    ) / _usage_cases.TOKENS_PER_MILLION


def haiku_turn_cost() -> float:
    return (
        _usage_cases.HAIKU_TURN_INPUT_TOKENS * _usage_cases.PRICE_RATE_FOUR_FIFTHS
        + _usage_cases.HAIKU_TURN_OUTPUT_TOKENS * 4
    ) / _usage_cases.TOKENS_PER_MILLION
