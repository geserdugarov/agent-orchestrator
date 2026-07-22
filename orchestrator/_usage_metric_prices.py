# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""First-party model prices and usage-cost calculations."""

from __future__ import annotations

import re
from typing import Optional

from orchestrator import _usage_metric_protocol as protocol


CLAUDE_RATES: tuple[protocol.ClaudeRateRow, ...] = (
    (
        re.compile(r"opus.*4([._-]?[567]|\.[567])"),
        {
            protocol.INPUT: 5,
            protocol.CACHE_WRITE_FIVE_MIN: 6.25,
            protocol.CACHE_WRITE_ONE_HOUR: 10,
            protocol.CACHE_READ: 0.5,
            protocol.OUTPUT: 25,
        },
    ),
    (
        re.compile(r"opus.*4"),
        {
            protocol.INPUT: 15,
            protocol.CACHE_WRITE_FIVE_MIN: 18.75,
            protocol.CACHE_WRITE_ONE_HOUR: 30,
            protocol.CACHE_READ: 1.5,
            protocol.OUTPUT: 75,
        },
    ),
    (
        re.compile(r"sonnet"),
        {
            protocol.INPUT: 3,
            protocol.CACHE_WRITE_FIVE_MIN: 3.75,
            protocol.CACHE_WRITE_ONE_HOUR: 6,
            protocol.CACHE_READ: 0.3,
            protocol.OUTPUT: 15,
        },
    ),
    (
        re.compile(r"haiku.*3([._-]?5|\.5)"),
        {
            protocol.INPUT: 0.8,
            protocol.CACHE_WRITE_FIVE_MIN: 1,
            protocol.CACHE_WRITE_ONE_HOUR: 1.6,
            protocol.CACHE_READ: 0.08,
            protocol.OUTPUT: 4,
        },
    ),
    (
        re.compile(r"haiku"),
        {
            protocol.INPUT: 1,
            protocol.CACHE_WRITE_FIVE_MIN: 1.25,
            protocol.CACHE_WRITE_ONE_HOUR: 2,
            protocol.CACHE_READ: 0.1,
            protocol.OUTPUT: 5,
        },
    ),
)

CODEX_RATES: tuple[protocol.CodexRateRow, ...] = (
    ("gpt-5.5-pro", {protocol.INPUT: 30, protocol.CACHED: None, protocol.OUTPUT: 180}),
    (
        "gpt-5.5",
        {
            protocol.INPUT: 5,
            protocol.CACHED: 0.5,
            protocol.OUTPUT: 30,
            protocol.LONG_CONTEXT_THRESHOLD: 272_000,
            protocol.LONG_CONTEXT_INPUT_MULT: 2.0,
            protocol.LONG_CONTEXT_OUTPUT_MULT: 1.5,
        },
    ),
    (
        "gpt-5.4-pro",
        {
            protocol.INPUT: 30,
            protocol.CACHED: None,
            protocol.OUTPUT: 180,
            protocol.LONG_CONTEXT_THRESHOLD: 272_000,
            protocol.LONG_CONTEXT_INPUT_MULT: 2.0,
            protocol.LONG_CONTEXT_OUTPUT_MULT: 1.5,
        },
    ),
    ("gpt-5.4-mini", {protocol.INPUT: 0.75, protocol.CACHED: 0.075, protocol.OUTPUT: 4.5}),
    ("gpt-5.4-nano", {protocol.INPUT: 0.2, protocol.CACHED: 0.02, protocol.OUTPUT: 1.25}),
    (
        "gpt-5.4",
        {
            protocol.INPUT: 2.5,
            protocol.CACHED: 0.25,
            protocol.OUTPUT: 15,
            protocol.LONG_CONTEXT_THRESHOLD: 272_000,
            protocol.LONG_CONTEXT_INPUT_MULT: 2.0,
            protocol.LONG_CONTEXT_OUTPUT_MULT: 1.5,
        },
    ),
    ("gpt-5.3-codex", {protocol.INPUT: 1.75, protocol.CACHED: 0.175, protocol.OUTPUT: 14}),
    ("gpt-5.3", {protocol.INPUT: 1.75, protocol.CACHED: 0.175, protocol.OUTPUT: 14}),
    ("gpt-5.2-pro", {protocol.INPUT: 21, protocol.CACHED: None, protocol.OUTPUT: 168}),
    ("gpt-5.2", {protocol.INPUT: 1.75, protocol.CACHED: 0.175, protocol.OUTPUT: 14}),
    ("gpt-5.1-codex-mini", {protocol.INPUT: 0.25, protocol.CACHED: 0.025, protocol.OUTPUT: 2}),
    ("gpt-5.1-codex", {protocol.INPUT: 1.25, protocol.CACHED: 0.125, protocol.OUTPUT: 10}),
    ("gpt-5.1", {protocol.INPUT: 1.25, protocol.CACHED: 0.125, protocol.OUTPUT: 10}),
    ("gpt-5-pro", {protocol.INPUT: 15, protocol.CACHED: None, protocol.OUTPUT: 120}),
    ("gpt-5-mini", {protocol.INPUT: 0.25, protocol.CACHED: 0.025, protocol.OUTPUT: 2}),
    ("gpt-5-nano", {protocol.INPUT: 0.05, protocol.CACHED: 0.005, protocol.OUTPUT: 0.4}),
    ("gpt-5-codex", {protocol.INPUT: 1.25, protocol.CACHED: 0.125, protocol.OUTPUT: 10}),
    ("gpt-5", {protocol.INPUT: 1.25, protocol.CACHED: 0.125, protocol.OUTPUT: 10}),
    ("codex-mini-latest", {protocol.INPUT: 1.5, protocol.CACHED: 0.375, protocol.OUTPUT: 6}),
)


def claude_rates(model: str) -> Optional[protocol.ClaudeRateMap]:
    if not model or model == protocol.UNKNOWN:
        return None
    lowered = model.lower()
    for pattern, rates in CLAUDE_RATES:
        if pattern.search(lowered):
            return rates
    return None


def claude_estimate_cost(
    model: str,
    bucket: protocol.TokenBucket,
) -> Optional[float]:
    rates = claude_rates(model)
    if rates is None:
        return None
    return (
        bucket[protocol.INPUT] * rates[protocol.INPUT]
        + bucket[protocol.CACHE_WRITE_FIVE_MIN] * rates[protocol.CACHE_WRITE_FIVE_MIN]
        + bucket[protocol.CACHE_WRITE_ONE_HOUR] * rates[protocol.CACHE_WRITE_ONE_HOUR]
        + bucket[protocol.CACHE_READ] * rates[protocol.CACHE_READ]
        + bucket[protocol.OUTPUT] * rates[protocol.OUTPUT]
    ) / protocol.TOKENS_PER_MILLION


def codex_rates(model: str) -> Optional[protocol.CodexRateMap]:
    if not model or model == protocol.UNKNOWN:
        return None
    lowered = model.lower()
    for prefix, rates in CODEX_RATES:
        if lowered.startswith(prefix):
            return rates
    return None
