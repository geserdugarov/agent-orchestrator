# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable skill-trigger models and provider parser entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from orchestrator import _usage_event_stream as event_stream
from orchestrator import _usage_metric_protocol as protocol
from orchestrator import _usage_skill_claude as claude_skills
from orchestrator import _usage_skill_codex as codex_skills


FoldedCounts = tuple[list[str], dict[str, int]]
EVIDENCE_CONFIRMED = "confirmed"
EVIDENCE_INFERRED = "inferred"


@dataclass(frozen=True)
class SkillTriggers:
    """Skills loaded, offered, or incidentally referenced during one run."""

    triggered: tuple[str, ...] = ()
    trigger_counts: dict[str, int] = field(default_factory=dict)
    available: tuple[str, ...] = ()
    evidence: dict[str, str] = field(default_factory=dict)
    incidental: tuple[str, ...] = ()
    incidental_counts: dict[str, int] = field(default_factory=dict)


def _fold_counts(names: Iterable[str]) -> FoldedCounts:
    order: list[str] = []
    counts: dict[str, int] = {}
    for name in names:
        if name not in counts:
            order.append(name)
            counts[name] = 0
        counts[name] += 1
    return order, counts


def _collect(
    names: Iterable[str],
    *,
    evidence_tier: str,
    available: Iterable[str] = (),
    incidental_names: Iterable[str] = (),
) -> SkillTriggers:
    order, counts = _fold_counts(names)
    incidental_order, incidental_counts = _fold_counts(incidental_names)
    return SkillTriggers(
        triggered=tuple(order),
        trigger_counts=counts,
        available=tuple(available),
        evidence={name: evidence_tier for name in order},
        incidental=tuple(incidental_order),
        incidental_counts=incidental_counts,
    )


def parse_claude_skills(stdout: str) -> SkillTriggers:
    """Extract confirmed and offered skills from a Claude stream-json run."""
    events = event_stream.iter_events(stdout)
    collector = claude_skills.ClaudeSkillCollector()
    for event in events:
        collector.add_event(event)
    return _collect(
        collector.names,
        evidence_tier=EVIDENCE_CONFIRMED,
        available=claude_skills.claude_offered_skills(events),
    )


def parse_codex_skills(stdout: str) -> SkillTriggers:
    """Extract inferred and incidental skills from a Codex JSON run."""
    collector = codex_skills.CodexSkillCollector()
    for event in event_stream.iter_events(stdout):
        collector.add_event(event)
    return _collect(
        collector.inferred_names(),
        evidence_tier=EVIDENCE_INFERRED,
        incidental_names=collector.incidental_names(),
    )


def parse_agent_skills(backend: str, stdout: str) -> SkillTriggers:
    """Dispatch skill parsing by agent backend."""
    if backend == protocol.CLAUDE:
        return parse_claude_skills(stdout)
    if backend == protocol.CODEX:
        return parse_codex_skills(stdout)
    raise ValueError(
        f"unknown agent backend {backend!r}; expected 'claude' or 'codex'",
    )


# Private compatibility names shared with trajectory reconstruction.
_CONTENT_KEY = claude_skills.CONTENT_KEY
_COMMAND_EXECUTION = codex_skills.COMMAND_EXECUTION
_claude_init_field = claude_skills.claude_init_field
_ordered_unique_names = claude_skills.ordered_unique_names
