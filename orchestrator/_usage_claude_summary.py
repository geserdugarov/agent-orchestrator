# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Claude run aggregation, pricing, and turn counting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from orchestrator import _usage_claude_rows as claude_rows
from orchestrator import _usage_event_stream as event_stream
from orchestrator import _usage_metric_prices as prices
from orchestrator import _usage_metric_protocol as protocol

if TYPE_CHECKING:
    from orchestrator._usage_metrics import UsageMetrics


@dataclass
class ClaudeUsageAggregate:
    """Per-model token buckets with stable first-seen model order."""

    per_model: dict[str, protocol.TokenBucket] = field(default_factory=dict)
    model_order: list[str] = field(default_factory=list)

    def add(self, model: str, record: protocol.TokenBucket) -> None:
        bucket = self.per_model.setdefault(
            model,
            {
                protocol.INPUT: 0,
                protocol.CACHE_WRITE_FIVE_MIN: 0,
                protocol.CACHE_WRITE_ONE_HOUR: 0,
                protocol.CACHE_READ: 0,
                protocol.OUTPUT: 0,
            },
        )
        if model not in self.model_order:
            self.model_order.append(model)
        for key, count in record.items():
            bucket[key] += count

    def apply_tokens(self, metrics: UsageMetrics) -> None:
        for bucket in self.per_model.values():
            metrics.input_tokens += bucket[protocol.INPUT]
            metrics.output_tokens += bucket[protocol.OUTPUT]
            metrics.cache_read_tokens += bucket[protocol.CACHE_READ]
            metrics.cache_write_tokens += bucket[protocol.CACHE_WRITE_FIVE_MIN] + bucket[protocol.CACHE_WRITE_ONE_HOUR]
        metrics.models = event_stream.dedup_models(self.model_order)


def aggregate_by_model(
    records: list[claude_rows.ClaudeUsageRow],
) -> ClaudeUsageAggregate:
    aggregate = ClaudeUsageAggregate()
    for _, model, record in records:
        aggregate.add(model, record)
    return aggregate


def estimate_total(
    per_model: dict[str, protocol.TokenBucket],
) -> Optional[float]:
    if not per_model:
        return None
    estimates: list[float] = []
    for model, bucket in per_model.items():
        estimate = prices.claude_estimate_cost(model, bucket)
        if estimate is None:
            return None
        estimates.append(estimate)
    return sum(estimates)


def turn_count(
    events: list[dict[str, Any]],
    records: list[claude_rows.ClaudeUsageRow],
) -> Optional[int]:
    reported_turns: Optional[int] = None
    for event in events:
        if event.get(protocol.TYPE) == protocol.RESULT_KEY:
            candidate = event.get("num_turns")
            if isinstance(candidate, (int, float)):
                reported_turns = int(candidate)
    if reported_turns is None and records:
        reported_turns = len(records)
    return reported_turns
