# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed analytics recording requests and legacy signature binding."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from orchestrator.agents import AgentResult


ISSUE_FIELD = "issue"
RESULT_FIELD = "result"


@dataclass(frozen=True)
class _StageEvaluationRequest:
    repo: str
    issue: int
    stage: Optional[str]
    duration_s: float
    evaluation_result: str


@dataclass(frozen=True)
class _AgentExitContext:
    """Inputs that describe one completed tracked agent run."""

    repo: str
    issue: int
    stage: str
    agent_role: str
    backend: str
    agent_spec: Optional[str]
    resume_session_id: Optional[str]
    agent_result: AgentResult
    duration_s: float
    review_round: Optional[int]
    retry_count: Optional[int]
    fallback_model: Optional[str]
    prompt: Optional[str]
    cwd: Optional[Path]
    analytics_package: Any = None


@dataclass
class _CodexCatalog:
    """Out-of-band capabilities missing from Codex's JSON stream."""

    available_skills: Optional[list[str]] = None
    tools: Optional[list[str]] = None


@dataclass(frozen=True)
class _AgentExitSkillFields:
    """Normalized optional skill fields for an `agent_exit` event.

    `skills_evidence` maps each triggered name to why it counts as a load
    (`confirmed` / `inferred`); `skills_incidental` / `skills_incidental_count`
    carry the path-only references the run made without loading a skill. All
    are dropped (their key absent) when empty, so a run with nothing to report
    keeps today's record shape.
    """

    skills_triggered: Optional[list[str]] = None
    skills_triggered_count: Optional[int] = None
    skills_available: Optional[list[str]] = None
    skills_evidence: Optional[dict[str, str]] = None
    skills_incidental: Optional[list[str]] = None
    skills_incidental_count: Optional[int] = None


def _parameter(
    name: str,
    default: Any = inspect.Parameter.empty,
) -> inspect.Parameter:
    return inspect.Parameter(
        name,
        inspect.Parameter.KEYWORD_ONLY,
        default=default,
    )


STAGE_EVALUATION_SIGNATURE = inspect.Signature(
    (
        _parameter("repo"),
        _parameter(ISSUE_FIELD),
        _parameter("stage"),
        _parameter("duration_s"),
        _parameter(RESULT_FIELD),
    )
)
AGENT_EXIT_SIGNATURE = inspect.Signature(
    (
        _parameter("repo"),
        _parameter(ISSUE_FIELD),
        _parameter("stage"),
        _parameter("agent_role"),
        _parameter("backend"),
        _parameter("agent_spec"),
        _parameter("resume_session_id"),
        _parameter(RESULT_FIELD),
        _parameter("duration_s"),
        _parameter("review_round"),
        _parameter("retry_count"),
        _parameter("fallback_model", None),
        _parameter("prompt", None),
        _parameter("cwd", None),
    )
)


def bind_stage_evaluation(
    positional_fields: tuple[Any, ...],
    keyword_fields: dict[str, Any],
) -> _StageEvaluationRequest:
    bound_fields = STAGE_EVALUATION_SIGNATURE.bind(
        *positional_fields,
        **keyword_fields,
    )
    bound_values = dict(bound_fields.arguments)
    bound_values["evaluation_result"] = bound_values.pop(RESULT_FIELD)
    bound_values[ISSUE_FIELD] = int(bound_values[ISSUE_FIELD])
    return _StageEvaluationRequest(**bound_values)


def bind_agent_exit(
    positional_fields: tuple[Any, ...],
    keyword_fields: dict[str, Any],
    analytics_package: Any = None,
) -> _AgentExitContext:
    bound_fields = AGENT_EXIT_SIGNATURE.bind(
        *positional_fields,
        **keyword_fields,
    )
    bound_fields.apply_defaults()
    bound_values = dict(bound_fields.arguments)
    bound_values["agent_result"] = bound_values.pop(RESULT_FIELD)
    bound_values[ISSUE_FIELD] = int(bound_values[ISSUE_FIELD])
    bound_values["analytics_package"] = analytics_package
    return _AgentExitContext(**bound_values)
