# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Resilient JSONL decoding and shared usage value helpers."""

from __future__ import annotations

import contextlib
import json
from typing import Any, Iterable, Optional

from orchestrator import _usage_metric_protocol as protocol


def iter_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            events.append(decoded)
    return events


def token_count(raw_count: Any) -> int:
    number = 0
    if isinstance(raw_count, bool):
        number = int(raw_count)
    elif isinstance(raw_count, (int, float)):
        number = int(raw_count)
    elif isinstance(raw_count, str):
        with contextlib.suppress(ValueError):
            number = int(float(raw_count))
    return number


def walk_objects(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk_objects(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk_objects(child)


def coerce_reported_cost(raw_cost: Any) -> Optional[float]:
    if isinstance(raw_cost, (int, float)):
        return float(raw_cost)
    if not isinstance(raw_cost, str):
        return None
    try:
        return float(raw_cost)
    except ValueError:
        return None


def find_last_reported_cost(events: list[dict[str, Any]]) -> Optional[float]:
    last_cost: Optional[float] = None
    for event in events:
        for payload in walk_objects(event):
            reported_cost = coerce_reported_cost(payload.get("total_cost_usd"))
            if reported_cost is not None:
                last_cost = reported_cost
    return last_cost


def dedup_models(models: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for model in models:
        if model and model != protocol.UNKNOWN and model not in seen:
            seen[model] = None
    return tuple(seen)


def select_cost(
    reported: Optional[float],
    estimated: Optional[float],
    has_usage: bool,
) -> tuple[Optional[float], str]:
    if reported is not None:
        return reported, "reported"
    if estimated is not None:
        return estimated, "estimated"
    if not has_usage:
        return None, "no-usage"
    return None, "unknown-price"
