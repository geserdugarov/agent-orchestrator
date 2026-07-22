# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable usage-metric models and provider parser entry points."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from orchestrator import _usage_claude_rows as claude_rows
from orchestrator import _usage_claude_summary as claude_summary
from orchestrator import _usage_codex_summary as codex_summary
from orchestrator import _usage_event_stream as event_stream
from orchestrator import _usage_metric_prices as prices
from orchestrator import _usage_metric_protocol as protocol
from orchestrator import _usage_model_paths as model_paths


@dataclass
class UsageMetrics:
    """Structured usage extracted from one agent run's JSONL stdout."""

    backend: str
    models: tuple[str, ...] = ()
    turns: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = "no-usage"

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "models": list(self.models),
            "turns": self.turns,
            protocol.INPUT_TOKENS: self.input_tokens,
            protocol.OUTPUT_TOKENS: self.output_tokens,
            protocol.CACHED_TOKENS: self.cached_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
        }


def parse_claude_usage(stdout: str) -> UsageMetrics:
    """Extract usage and cost from a Claude stream-json run."""
    events = event_stream.iter_events(stdout)
    metrics = UsageMetrics(backend=protocol.CLAUDE)
    records = claude_rows.claude_usage_records(events)
    aggregate = claude_summary.aggregate_by_model(records)
    aggregate.apply_tokens(metrics)
    selected_cost = event_stream.select_cost(
        event_stream.find_last_reported_cost(events),
        claude_summary.estimate_total(aggregate.per_model),
        bool(records),
    )
    metrics.cost_usd = selected_cost[0]
    metrics.cost_source = selected_cost[1]
    metrics.turns = claude_summary.turn_count(events, records)
    return metrics


def parse_codex_usage(
    stdout: str,
    fallback_model: Optional[str] = None,
) -> UsageMetrics:
    """Extract usage and cost from a Codex JSON run."""
    events = event_stream.iter_events(stdout)
    metrics = UsageMetrics(backend=protocol.CODEX)
    codex_summary.CodexUsageSummary.build(events, fallback_model).apply(metrics)
    return metrics


def parse_agent_usage(
    backend: str,
    stdout: str,
    *,
    fallback_model: Optional[str] = None,
) -> UsageMetrics:
    """Dispatch usage parsing by agent backend."""
    if backend == protocol.CLAUDE:
        return parse_claude_usage(stdout)
    if backend == protocol.CODEX:
        return parse_codex_usage(stdout, fallback_model=fallback_model)
    raise ValueError(
        f"unknown agent backend {backend!r}; expected 'claude' or 'codex'",
    )


# Private compatibility names used by the sibling skill and trajectory parsers.
_TokenBucket = protocol.TokenBucket
_ASSISTANT = protocol.ASSISTANT
_CACHE_READ = protocol.CACHE_READ
_CACHE_WRITE_FIVE_MIN = protocol.CACHE_WRITE_FIVE_MIN
_CACHE_WRITE_ONE_HOUR = protocol.CACHE_WRITE_ONE_HOUR
_CLAUDE = protocol.CLAUDE
_CODEX = protocol.CODEX
_ID = protocol.ID
_INPUT = protocol.INPUT
_INPUT_TOKENS = protocol.INPUT_TOKENS
_ITEM_KEY = protocol.ITEM_KEY
_MESSAGE = protocol.MESSAGE
_MODEL = protocol.MODEL
_OUTPUT = protocol.OUTPUT
_OUTPUT_TOKENS = protocol.OUTPUT_TOKENS
_RESULT_KEY = protocol.RESULT_KEY
_TYPE = protocol.TYPE
_USAGE = protocol.USAGE
_iter_events = event_stream.iter_events
_claude_estimate_cost = prices.claude_estimate_cost
_claude_model_name = model_paths.claude_model_name
_claude_usage_record = claude_rows.claude_usage_record
