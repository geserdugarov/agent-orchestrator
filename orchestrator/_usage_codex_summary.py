# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Codex cumulative usage pricing and run summary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from orchestrator import _usage_codex_rows as codex_rows
from orchestrator import _usage_event_stream as event_stream
from orchestrator import _usage_metric_prices as prices
from orchestrator import _usage_metric_protocol as protocol

if TYPE_CHECKING:
    from orchestrator._usage_metrics import UsageMetrics


TURN_COMPLETE_RE = re.compile(r"turn[_ -]?complete|turncomplete", re.IGNORECASE)


@dataclass(frozen=True)
class CodexPrice:
    rates: protocol.CodexRateMap
    usage: protocol.TokenBucket

    def estimate(self) -> Optional[float]:
        input_multiplier, output_multiplier = self._multipliers()
        input_cost = self._input_cost(input_multiplier)
        if input_cost is None:
            return None
        output_cost = self.usage[protocol.OUTPUT] * self.rates[protocol.OUTPUT]
        return (input_cost + output_cost * output_multiplier) / protocol.TOKENS_PER_MILLION

    def _multipliers(self) -> tuple[float, float]:
        threshold = self.rates.get(protocol.LONG_CONTEXT_THRESHOLD)
        if threshold is None or self.usage[protocol.INPUT] <= threshold:
            return 1.0, 1.0
        return (
            self.rates.get(protocol.LONG_CONTEXT_INPUT_MULT) or 1.0,
            self.rates.get(protocol.LONG_CONTEXT_OUTPUT_MULT) or 1.0,
        )

    def _input_cost(self, multiplier: float) -> Optional[float]:
        cached_tokens = self.usage[protocol.CACHED]
        cached_rate = self.rates[protocol.CACHED]
        if cached_tokens > 0 and cached_rate is None:
            return None
        uncached_tokens = max(self.usage[protocol.INPUT] - cached_tokens, 0)
        effective_cached_rate = self.rates[protocol.INPUT] if cached_rate is None else cached_rate
        return (uncached_tokens * self.rates[protocol.INPUT] + cached_tokens * effective_cached_rate) * multiplier


def estimate_cost(
    model: str,
    usage: protocol.TokenBucket,
) -> Optional[float]:
    rates = prices.codex_rates(model)
    billable_tokens = usage[protocol.INPUT] + usage[protocol.OUTPUT]
    if rates is None or billable_tokens <= 0:
        return None
    return CodexPrice(rates, usage).estimate()


def reported_turn_count(events: list[dict[str, Any]]) -> Optional[int]:
    reported: Optional[int] = None
    for event in events:
        for payload in event_stream.walk_objects(event):
            turns_value = payload.get("num_turns")
            if isinstance(turns_value, (int, float)):
                reported = int(turns_value)
    return reported


def completed_turn_count(events: list[dict[str, Any]]) -> Optional[int]:
    count = 0
    for event in events:
        event_type = event.get(protocol.TYPE)
        if isinstance(event_type, str) and TURN_COMPLETE_RE.search(event_type):
            count += 1
    return count or None


def turn_count(events: list[dict[str, Any]]) -> Optional[int]:
    reported = reported_turn_count(events)
    if reported is not None:
        return reported
    return completed_turn_count(events)


@dataclass(frozen=True)
class CodexUsageSummary:
    events: list[dict[str, Any]]
    usage_events: list[protocol.CodexUsageEvent]
    usage: protocol.TokenBucket
    model: Optional[str]

    @classmethod
    def build(
        cls,
        events: list[dict[str, Any]],
        fallback_model: Optional[str],
    ) -> CodexUsageSummary:
        usage_events = codex_rows.codex_usage_events(events)
        last_model, usage = codex_rows.last_codex_usage(usage_events)
        return cls(
            events=events,
            usage_events=usage_events,
            usage=usage,
            model=codex_rows.codex_select_model(events, last_model, fallback_model),
        )

    def apply(self, metrics: UsageMetrics) -> None:
        metrics.input_tokens = self.usage[protocol.INPUT]
        metrics.cached_tokens = self.usage[protocol.CACHED]
        metrics.output_tokens = self.usage[protocol.OUTPUT]
        if self.model is not None:
            metrics.models = (self.model,)
        selected_cost = event_stream.select_cost(
            event_stream.find_last_reported_cost(self.events),
            estimate_cost(self.model or protocol.UNKNOWN, self.usage),
            bool(self.usage_events),
        )
        metrics.cost_usd = selected_cost[0]
        metrics.cost_source = selected_cost[1]
        metrics.turns = turn_count(self.events)
