# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Small immutable views parsed from trajectory JSON records."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Optional

from orchestrator import _trajectory_constants as constants


KIND_FIELD = "kind"
NAME_FIELD = "name"
TOOL_ID_FIELD = "tool_id"
CONTENT_FIELD = "content"
TURN_FIELD = "turn"
ORIGIN_MODULE = "orchestrator._trajectory_records"
STEP_VIEW_SIGNATURE = inspect.Signature(
    parameters=(
        inspect.Parameter(KIND_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter(NAME_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=""),
        inspect.Parameter(TOOL_ID_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=""),
        inspect.Parameter(CONTENT_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=""),
        inspect.Parameter(TURN_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None),
    )
)
TIMELINE_ENTRY_SIGNATURE = inspect.Signature(
    parameters=(
        inspect.Parameter(KIND_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter(CONTENT_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=""),
        inspect.Parameter(NAME_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=""),
        inspect.Parameter(TOOL_ID_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=""),
        inspect.Parameter(TURN_FIELD, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None),
    )
)


def public_step_content(step: TrajectoryStepView) -> str:
    """Return a step body through its historical public name."""
    return step.step_content


def public_entry_content(entry: TimelineEntry) -> str:
    """Return a timeline body through its historical public name."""
    return entry.entry_content


@dataclass(frozen=True, init=False)
class TrajectoryStepView:
    """One normalized step from a trajectory record."""

    kind: str
    name: str = ""
    tool_id: str = ""
    step_content: str = ""
    turn: Optional[int] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        bound = STEP_VIEW_SIGNATURE.bind(*args, **kwargs)
        bound.apply_defaults()
        object.__setattr__(self, KIND_FIELD, bound.arguments[KIND_FIELD])
        object.__setattr__(self, NAME_FIELD, bound.arguments[NAME_FIELD])
        object.__setattr__(self, TOOL_ID_FIELD, bound.arguments[TOOL_ID_FIELD])
        object.__setattr__(self, "step_content", bound.arguments[CONTENT_FIELD])
        object.__setattr__(self, TURN_FIELD, bound.arguments[TURN_FIELD])

    @property
    def is_call(self) -> bool:
        return self.kind == "tool_call"

    @property
    def is_result(self) -> bool:
        return self.kind == "tool_result"


@dataclass(frozen=True, init=False)
class TimelineEntry:
    """One normalized prompt, step, or output timeline entry."""

    kind: str
    entry_content: str = ""
    name: str = ""
    tool_id: str = ""
    turn: Optional[int] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        bound = TIMELINE_ENTRY_SIGNATURE.bind(*args, **kwargs)
        bound.apply_defaults()
        object.__setattr__(self, KIND_FIELD, bound.arguments[KIND_FIELD])
        object.__setattr__(self, "entry_content", bound.arguments[CONTENT_FIELD])
        object.__setattr__(self, NAME_FIELD, bound.arguments[NAME_FIELD])
        object.__setattr__(self, TOOL_ID_FIELD, bound.arguments[TOOL_ID_FIELD])
        object.__setattr__(self, TURN_FIELD, bound.arguments[TURN_FIELD])

    @property
    def is_prompt(self) -> bool:
        return self.kind == constants.TIMELINE_PROMPT

    @property
    def is_output(self) -> bool:
        return self.kind == constants.TIMELINE_OUTPUT


@dataclass(frozen=True)
class TurnUsageView:
    """Per-turn token usage for one Claude assistant turn."""

    turn: Optional[int] = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class RunUsageView:
    """Run-level usage summary stored on a trajectory record."""

    models: tuple[str, ...] = ()
    turns: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


setattr(TrajectoryStepView, CONTENT_FIELD, property(public_step_content))
setattr(TimelineEntry, CONTENT_FIELD, property(public_entry_content))
TrajectoryStepView.__module__ = ORIGIN_MODULE
TrajectoryStepView.__signature__ = STEP_VIEW_SIGNATURE
TimelineEntry.__module__ = ORIGIN_MODULE
TimelineEntry.__signature__ = TIMELINE_ENTRY_SIGNATURE
TurnUsageView.__module__ = ORIGIN_MODULE
RunUsageView.__module__ = ORIGIN_MODULE
