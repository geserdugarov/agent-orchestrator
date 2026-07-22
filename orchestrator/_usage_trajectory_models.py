# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable trajectory records and their serialized views."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Optional

from orchestrator._usage_skills import SkillTriggers


KIND_FIELD = "kind"
NAME_FIELD = "name"
TOOL_ID_FIELD = "tool_id"
TURN_FIELD = "turn"
CONTENT_FIELD = "content"
ORIGIN_MODULE = "orchestrator._usage_trajectory"
STEP_SIGNATURE = inspect.Signature(
    parameters=(
        inspect.Parameter(KIND_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter(NAME_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=""),
        inspect.Parameter(TOOL_ID_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=""),
        inspect.Parameter(TURN_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None),
        inspect.Parameter(CONTENT_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None),
    )
)


def public_step_content(step: TrajectoryStep) -> Any:
    """Return a step payload through its historical public name."""
    return step.step_payload


@dataclass(frozen=True, init=False)
class TrajectoryStep:
    """One ordered message, tool call, or tool result in an agent run."""

    kind: str
    name: str = ""
    tool_id: str = ""
    turn: Optional[int] = None
    step_payload: Any = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        bound = STEP_SIGNATURE.bind(*args, **kwargs)
        bound.apply_defaults()
        object.__setattr__(self, KIND_FIELD, bound.arguments[KIND_FIELD])
        object.__setattr__(self, NAME_FIELD, bound.arguments[NAME_FIELD])
        object.__setattr__(self, TOOL_ID_FIELD, bound.arguments[TOOL_ID_FIELD])
        object.__setattr__(self, TURN_FIELD, bound.arguments[TURN_FIELD])
        object.__setattr__(self, "step_payload", bound.arguments[CONTENT_FIELD])

    def to_dict(self) -> dict[str, Any]:
        return {
            KIND_FIELD: self.kind,
            NAME_FIELD: self.name,
            TOOL_ID_FIELD: self.tool_id,
            TURN_FIELD: self.turn,
            CONTENT_FIELD: self.step_payload,
        }


@dataclass(frozen=True)
class TurnUsage:
    """Per-turn token usage for one Claude assistant turn."""

    turn: int
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = "estimated"

    def to_dict(self) -> dict[str, Any]:
        return {
            TURN_FIELD: self.turn,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
        }


@dataclass(frozen=True)
class AgentTrajectory:
    """Structured trajectory reconstructed from one agent JSONL stream."""

    backend: str
    system_prompt: Optional[str] = None
    tools: tuple[str, ...] = ()
    skills: SkillTriggers = field(default_factory=SkillTriggers)
    steps: tuple[TrajectoryStep, ...] = ()
    final_output: Optional[str] = None
    turns: tuple[TurnUsage, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "system_prompt": self.system_prompt,
            "tools": list(self.tools),
            "skills": {
                "triggered": list(self.skills.triggered),
                "trigger_counts": dict(self.skills.trigger_counts),
                "available": list(self.skills.available),
            },
            "steps": [step.to_dict() for step in self.steps],
            "final_output": self.final_output,
            "turns": [turn_usage.to_dict() for turn_usage in self.turns],
        }


setattr(TrajectoryStep, CONTENT_FIELD, property(public_step_content))
TrajectoryStep.__module__ = ORIGIN_MODULE
TrajectoryStep.__signature__ = STEP_SIGNATURE
TurnUsage.__module__ = ORIGIN_MODULE
AgentTrajectory.__module__ = ORIGIN_MODULE
