# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Opt-in trajectory sink -- serialization, budgeting, and persistence.

A separate, opt-in trajectory sink lives beside the analytics event
sink in `orchestrator.analytics._recording`. `TRAJECTORY_LOG_PATH` is
parsed there but defaults *off*: unset / empty / `off` / `disabled` /
`none` all disable it (unlike `ANALYTICS_LOG_PATH`, which defaults to a
path under `config.LOG_DIR`). When enabled it gates an independent JSONL
file for per-run reasoning trajectories, pruned by
`TRAJECTORY_RETENTION_DAYS` with the same semantics as
`ANALYTICS_RETENTION_DAYS` (default 90; non-positive keeps forever).

Its producer is `record_agent_exit` (in `_recording`, via
`_maybe_record_trajectory`): when the sink is on it parses the run's
trajectory from the same stdout, redacts and head/tail truncates every
free-text field, denormalizes the `UsageMetrics` `record_agent_exit`
already parsed for the baseline record into a `run_usage` summary (plus
claude's per-turn `turns[]`), and appends one `agent_trajectory` record
-- all behind its own fail-open guard so it never disturbs the baseline
`agent_exit` usage record. `append_trajectory_record` /
`prune_trajectory_records` share the append / prune discipline of their
analytics counterparts in `_recording` (reopen append per record,
`mkdir -p` parents, `OSError` downgraded to a warning, malformed lines
preserved on prune) -- they reuse `_recording`'s `_append_jsonl_record`
/ `_prune_jsonl_records` cores -- but hold a dedicated file lock and
never touch `ANALYTICS_LOG_PATH`, the analytics Postgres sync, or the
dashboard rollups: the two sinks are fully independent files. The pinned
GitHub state on each issue is the authoritative durable state -- this
sink is local-filesystem observability and may be truncated or deleted
at any time without affecting workflow correctness.

Settings ownership. `TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS`
and the `_TRAJECTORY_RECORD_BUDGET` / `_TRAJECTORY_FIELD_HEAD` /
`_TRAJECTORY_FIELD_TAIL` caps below are re-exported on the
`orchestrator.analytics` facade; every helper here reads them back off
that facade at call time via `_recording._live_settings`, so a test that
patches `analytics.*` or reloads the package sees the fresh value.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Optional

from orchestrator import usage
from orchestrator.analytics._recording import (
    _AgentExitContext as _AgentExitContext,
    _CodexCatalog as _CodexCatalog,
    _append_jsonl_record as _append_jsonl_record,
    _live_settings as _live_settings,
    _prune_jsonl_records as _prune_jsonl_records,
    build_record as build_record,
    log as log,
)

# A redactor masks the secret-shaped substrings of one free-text field.
# Bound to `workflow_messages._redact_secrets` at call time in
# `_persist_trajectory_record` (a call-time import avoids a
# `github` -> `analytics` -> `workflow_messages` -> `github` cycle).
_Redactor = Callable[[str], str]

# Head/tail truncation caps for the opt-in trajectory record. Each free-text
# field -- `user_input` (the orchestrator-built prompt), `system_prompt`,
# every per-step `content` (a `tool_call` input, a `tool_result` output, or an
# `assistant_message` / `user_message` text turn), and the final
# `output` -- is redacted with `workflow_messages._redact_secrets` and then
# truncated to its first `_TRAJECTORY_FIELD_HEAD` and last
# `_TRAJECTORY_FIELD_TAIL` characters (the head carries the request, the tail
# the result) with an elision marker in between. Redaction runs BEFORE
# truncation so a secret straddling the elided middle cannot survive as two
# halves. The whole record is additionally bounded: each step is charged its
# full *serialized* size (the JSON metadata -- `kind` / `name` / `tool_id` --
# plus its truncated content, not just `len(content)`), so even thousands of
# empty- or metadata-only steps still consume the budget; once the running
# total crosses `_TRAJECTORY_RECORD_BUDGET` bytes the remaining steps are
# dropped and a `truncated` flag is set, so a single pathological run cannot
# write an unbounded line. All three caps are re-exported on the `analytics`
# facade and read live via `_live_settings` so a test can shrink them with
# `patch.object(analytics, "_TRAJECTORY_RECORD_BUDGET", ...)` (and the head /
# tail equivalents).
_TRAJECTORY_FIELD_HEAD = 2000
_TRAJECTORY_FIELD_TAIL = 2000
_TRAJECTORY_RECORD_BUDGET = 200_000

# A dedicated lock for the trajectory sink so its append / prune
# serialize against each other (the same read-vs-replace race the
# analytics lock closes) but NOT against the analytics file -- the two
# sinks are independent paths and must not block one another.
_TRAJECTORY_FILE_LOCK = threading.Lock()


@dataclass(frozen=True)
class _TrajectoryHeadline:
    """Always-retained trajectory fields charged before variable arrays."""

    user_input: Optional[str]
    system_prompt: Optional[str]
    output: Optional[str]
    run_usage: dict[str, Any]

    @property
    def serialized_size(self) -> int:
        text_size = sum(
            len(text_field or "")
            for text_field in (self.user_input, self.system_prompt, self.output)
        )
        return text_size + len(json.dumps(self.run_usage, default=str))


@dataclass
class _TrajectoryBudget:
    """Track serialized variable-field bytes retained in one record."""

    used: int
    limit: int
    truncated: bool = False

    def include(self, field_value: Any) -> bool:
        self.used += len(json.dumps(field_value, default=str))
        if self.used <= self.limit:
            return True
        self.truncated = True
        return False


def _truncate_head_tail(text: str, head: int, tail: int) -> str:
    """Keep the first `head` + last `tail` chars of `text`, eliding the
    middle with a marker recording how many chars were dropped. Returns
    `text` unchanged when it already fits within `head + tail`."""
    if len(text) <= head + tail:
        return text
    elided = len(text) - head - tail
    head_text = text[:head]
    tail_text = text[-tail:]
    return f"{head_text}\n...[{elided} chars elided]...\n{tail_text}"


def _redact_tree(node: Any, redact: _Redactor) -> Any:
    r"""Recursively redact every string leaf of a tool payload.

    Applied before JSON serialization so a multiline / control-character
    secret in a tool input or result is masked on the raw leaf:
    `json.dumps` would otherwise escape its newlines (a real `\n` becomes
    the two-character `\n` escape), leaving `_redact_secrets`' literal
    `str.replace` unable to match the raw env value -- and the secret would
    survive into `steps[].content`. Dict keys are structural field names and
    pass through unredacted; only values and list elements carry
    agent-sourced content. Non-string scalars (numbers, bools, `None`) are
    returned as-is.
    """
    if isinstance(node, str):
        return redact(node)
    if isinstance(node, dict):
        return {key: _redact_tree(child, redact) for key, child in node.items()}
    if isinstance(node, list):
        return [_redact_tree(child, redact) for child in node]
    return node


def _redact_and_truncate(field_value: Any, redact: _Redactor) -> Optional[str]:
    """Redact then per-field head/tail truncate one trajectory value.

    String leaves are redacted with `_redact_secrets` BEFORE any JSON
    serialization. A plain string is redacted directly; dict / list content
    (claude tool inputs are dicts; `tool_result` content a list) is redacted
    leaf-by-leaf via `_redact_tree` first, then serialized -- serializing
    first would escape a multiline secret's newlines so the redactor's
    literal `str.replace` could no longer match it. A final redact pass over
    the serialized text is a cheap safety net for any leaf the walk could
    not reach (e.g. a value stringified by `default=str`). Redaction precedes
    truncation so a secret spanning the elided middle cannot leak as two
    halves. Empty / `None` content yields `None` so `build_record` drops the
    field rather than storing an empty string.
    """
    if field_value is None:
        return None
    if isinstance(field_value, str):
        text = redact(field_value)
    else:
        try:
            text = json.dumps(
                _redact_tree(field_value, redact), sort_keys=True, default=str,
            )
        except (TypeError, ValueError):
            text = str(_redact_tree(field_value, redact))
        text = redact(text)
    if not text:
        return None
    settings = _live_settings()
    return _truncate_head_tail(
        text, settings._TRAJECTORY_FIELD_HEAD, settings._TRAJECTORY_FIELD_TAIL,
    )


def _trajectory_usage(metrics: usage.UsageMetrics) -> dict[str, Any]:
    run_usage = metrics.to_dict()
    run_usage.pop("backend", None)
    return run_usage


def _trajectory_headline(
    context: _AgentExitContext,
    trajectory: usage.AgentTrajectory,
    metrics: usage.UsageMetrics,
    redact: _Redactor,
) -> _TrajectoryHeadline:
    return _TrajectoryHeadline(
        user_input=_redact_and_truncate(context.prompt, redact),
        system_prompt=_redact_and_truncate(trajectory.system_prompt, redact),
        output=_redact_and_truncate(trajectory.final_output, redact),
        run_usage=_trajectory_usage(metrics),
    )


def _bounded_trajectory_turns(
    trajectory: usage.AgentTrajectory,
    budget: _TrajectoryBudget,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for turn in trajectory.turns:
        turn_dict = turn.to_dict()
        if not budget.include(turn_dict):
            break
        turns.append(turn_dict)
    return turns


def _trajectory_step(step: usage.TrajectoryStep, redact: _Redactor) -> dict[str, Any]:
    step_dict: dict[str, Any] = {
        "kind": step.kind,
        "name": step.name or None,
        "tool_id": step.tool_id or None,
        "content": _redact_and_truncate(step.content, redact),
    }
    if step.turn is not None:
        step_dict["turn"] = step.turn
    return step_dict


def _bounded_trajectory_steps(
    trajectory: usage.AgentTrajectory,
    budget: _TrajectoryBudget,
    redact: _Redactor,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if budget.truncated:
        return steps
    for step in trajectory.steps:
        step_dict = _trajectory_step(step, redact)
        if not budget.include(step_dict):
            break
        steps.append(step_dict)
    return steps


def _build_trajectory_record(
    context: _AgentExitContext,
    trajectory: usage.AgentTrajectory,
    metrics: usage.UsageMetrics,
    redact: _Redactor,
) -> dict:
    """Assemble one redacted, truncated `agent_trajectory` record.

    `prompt` becomes the redacted `user_input`; `system_prompt`, each
    step's content, and the final `output` are redacted the same way.
    `metrics` (the same `UsageMetrics` the baseline `agent_exit` record
    already carries) is denormalized into a `run_usage` summary so the
    file-only viewer needs no re-parse; it is `UsageMetrics.to_dict()` minus
    `backend` (already a record field). Its token counts / cost / model name
    are not secret-shaped, so they skip redaction. The claude per-turn
    breakdown rides along as `turns` (empty on codex, whose usage frames are
    cumulative -- `build_record` then drops the key).

    Each step is charged its full *serialized* size -- the JSON metadata
    (`kind` / `name` / `tool_id` / `turn`) plus its truncated content, not
    merely `len(content)` -- so steps with empty or tiny content still consume
    the budget. The per-turn `turns` array is charged and truncated the same
    way (a run with thousands of turns and no steps would otherwise write the
    whole array in full and blow the budget); it is drawn down before the
    steps, so once the running total crosses `_TRAJECTORY_RECORD_BUDGET` the
    remaining turns -- then steps -- are dropped and `truncated` is set. Only
    the small fixed `run_usage` summary is always kept whole. `build_record`
    drops every `None`-valued field, so an absent prompt, empty system prompt,
    no-trigger skill set, or codex's empty per-turn array leaves its key off
    rather than storing a null.
    """
    headline = _trajectory_headline(context, trajectory, metrics, redact)
    budget = _TrajectoryBudget(
        headline.serialized_size, _live_settings()._TRAJECTORY_RECORD_BUDGET,
    )
    turns = _bounded_trajectory_turns(trajectory, budget)
    steps = _bounded_trajectory_steps(trajectory, budget, redact)
    return build_record(
        repo=context.repo,
        issue=context.issue,
        event="agent_trajectory",
        stage=context.stage,
        agent_role=context.agent_role,
        backend=context.backend,
        session_id=context.agent_result.session_id,
        review_round=context.review_round,
        retry_count=context.retry_count,
        user_input=headline.user_input,
        system_prompt=headline.system_prompt,
        tools=list(trajectory.tools) or None,
        skills_triggered=list(trajectory.skills.triggered) or None,
        skills_available=list(trajectory.skills.available) or None,
        run_usage=headline.run_usage,
        turns=turns or None,
        steps=steps,
        output=headline.output,
        truncated=budget.truncated or None,
    )


def _codex_trajectory_changes(
    trajectory: usage.AgentTrajectory,
    catalog: _CodexCatalog,
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if catalog.available_skills and not trajectory.skills.available:
        changes["skills"] = replace(
            trajectory.skills,
            available=tuple(catalog.available_skills),
        )
    if catalog.tools and not trajectory.tools:
        changes["tools"] = tuple(catalog.tools)
    return changes


def _agent_trajectory(
    context: _AgentExitContext,
    catalog: _CodexCatalog,
) -> usage.AgentTrajectory:
    trajectory = usage.parse_agent_trajectory(
        context.backend,
        context.agent_result.stdout,
    )
    if context.backend != "codex":
        return trajectory
    changes = _codex_trajectory_changes(trajectory, catalog)
    if not changes:
        return trajectory
    return replace(trajectory, **changes)


def _persist_trajectory_record(
    context: _AgentExitContext,
    metrics: usage.UsageMetrics,
    codex_catalog: _CodexCatalog,
) -> None:
    """Build and append the denormalized trajectory record.

    `_redact_secrets` is imported at call time to avoid a
    `github` -> `analytics` -> `workflow_messages` -> `github` import cycle.
    """
    from orchestrator.workflow_messages import _redact_secrets
    trajectory = _agent_trajectory(context, codex_catalog)
    _live_settings().append_trajectory_record(
        _build_trajectory_record(context, trajectory, metrics, _redact_secrets)
    )


def _maybe_record_trajectory(
    context: _AgentExitContext,
    metrics: usage.UsageMetrics,
    codex_catalog: _CodexCatalog,
) -> None:
    """Parse, redact, truncate, and append one trajectory record -- gated on
    the opt-in `TRAJECTORY_LOG_PATH` and wrapped in its own fail-open guard.

    A no-op when the trajectory sink is disabled (the default), so the
    orchestrator-built prompt (`user_input`) -- and the parse/redact work
    itself -- happens ONLY when an operator turned the sink on. `metrics` is
    the `UsageMetrics` `record_agent_exit` already parsed for the baseline
    `agent_exit` record; it is threaded through (never re-parsed) so the
    trajectory record can carry a denormalized `run_usage` summary. The whole
    block rides a dedicated try/except: a parser bug, an unredactable
    payload, or a sink IO failure logs and is swallowed so it can never drop
    the baseline `agent_exit` usage / cost record or the `skill_triggered`
    audit events, all of which were already produced before this runs.
    `_redact_secrets` is imported at call time to avoid a
    `github` -> `analytics` -> `workflow_messages` -> `github` import cycle.

    `codex_catalog` carries the out-of-band offered-skills and offered-tools
    sets `record_agent_exit` discovered for a codex run (empty for claude,
    whose offered sets already ride its stream). When present they backfill
    the codex trajectory's otherwise-empty
    `skills.available` / `tools` so the trajectory viewer's "Skills available"
    and "Tools offered" chips match a claude run's; a non-empty stream-parsed
    set is never overridden.
    """
    if _live_settings().TRAJECTORY_LOG_PATH is None:
        return
    try:
        _persist_trajectory_record(context, metrics, codex_catalog)
    except Exception:
        log.exception(
            "issue=#%d analytics: trajectory record(%s) failed; "
            "baseline agent_exit record is unaffected",
            context.issue,
            context.backend,
        )


def append_trajectory_record(record: dict) -> None:
    """Append one JSONL line to `TRAJECTORY_LOG_PATH` if configured.

    No-op when the trajectory sink is disabled (the opt-in default).
    Shares `append_record`'s discipline -- reopen append per record,
    `mkdir -p` parents, OSError downgraded to a warning -- but writes to
    the trajectory file under `_TRAJECTORY_FILE_LOCK`, so it never opens,
    serializes against, or otherwise interacts with `ANALYTICS_LOG_PATH`,
    the analytics Postgres sync, or the dashboard rollups.
    """
    _append_jsonl_record(
        _live_settings().TRAJECTORY_LOG_PATH, _TRAJECTORY_FILE_LOCK, record,
    )


def prune_trajectory_records(*, now: Optional[datetime] = None) -> int:
    """Remove trajectory records older than `TRAJECTORY_RETENTION_DAYS`.

    Reads the `TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS` bound on
    the `orchestrator.analytics` facade. Mirrors `prune_old_records` exactly
    (no-op when the sink is disabled, retention is non-positive, or the
    file is absent; malformed / unparseable lines preserved; atomic
    temp-file + `os.replace` rewrite) but operates solely on the
    trajectory file under `_TRAJECTORY_FILE_LOCK` -- it never touches
    `ANALYTICS_LOG_PATH`, the analytics Postgres sync, or the dashboard
    rollups. `now` is parameter-overridable so tests can pin the
    comparison point.
    """
    settings = _live_settings()
    return _prune_jsonl_records(
        settings.TRAJECTORY_LOG_PATH, settings.TRAJECTORY_RETENTION_DAYS,
        _TRAJECTORY_FILE_LOCK, now,
    )
