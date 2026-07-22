# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Build Claude per-turn usage aligned with trajectory steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from orchestrator import _usage_claude_rows as claude_rows
from orchestrator import _usage_metric_prices as prices
from orchestrator import _usage_metric_protocol as protocol
from orchestrator import _usage_model_paths as model_paths
from orchestrator import _usage_trajectory_claude_stream as claude_stream
from orchestrator._usage_trajectory_models import TurnUsage


TurnUsageRow = tuple[int, str, protocol.TokenBucket]


@dataclass
class ClaudeTurnUsageBuilder:
    turn_index: dict[str, int] = field(default_factory=dict)
    by_key: dict[str, TurnUsageRow] = field(default_factory=dict)

    def add_event(self, index: int, event: dict[str, Any]) -> None:
        if event.get(protocol.TYPE) != protocol.ASSISTANT:
            return
        message = event.get(protocol.MESSAGE)
        if not isinstance(message, dict):
            return
        key = claude_stream.turn_key(index, event)
        turn = self.turn_index.setdefault(key, len(self.turn_index))
        usage = message.get(protocol.USAGE)
        if isinstance(usage, dict):
            self.by_key[key] = (
                turn,
                model_paths.claude_model_name(event),
                claude_rows.claude_usage_record(usage),
            )

    def build(self) -> tuple[TurnUsage, ...]:
        ordered_rows = sorted(self.by_key.values(), key=turn_usage_row_index)
        return tuple(turn_usage_from_row(row) for row in ordered_rows)


def turn_usage_row_index(usage_row: TurnUsageRow) -> int:
    return usage_row[0]


def turn_usage_from_row(usage_row: TurnUsageRow) -> TurnUsage:
    turn, model, record = usage_row
    estimated_cost = prices.claude_estimate_cost(model, record)
    return TurnUsage(
        turn=turn,
        model=model,
        input_tokens=record[protocol.INPUT],
        output_tokens=record[protocol.OUTPUT],
        cache_read_tokens=record[protocol.CACHE_READ],
        cache_write_tokens=(record[protocol.CACHE_WRITE_FIVE_MIN] + record[protocol.CACHE_WRITE_ONE_HOUR]),
        cost_usd=estimated_cost,
        cost_source="unknown-price" if estimated_cost is None else "estimated",
    )


def claude_turn_usage(
    events: Iterable[dict[str, Any]],
) -> tuple[TurnUsage, ...]:
    builder = ClaudeTurnUsageBuilder()
    for index, event in enumerate(events):
        builder.add_event(index, event)
    return builder.build()
