# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics event recording -- sink configuration, JSONL primitives, and
recorders.

Backs the `orchestrator.analytics` package facade. Append-only JSONL
records keyed by `ts`, `repo`, `issue`, `event`, and optional `stage`.
Distinct from the audit event log at `config.EVENT_LOG_PATH`: the audit
log is wired through `GitHubClient.emit_event` for stage transitions /
agent lifecycle events, while this analytics sink is a foundation layer
for future aggregation that can be opted in or out independently. The raw
JSONL is intended to be ingested later into a structured database
(SQLite / DuckDB / Postgres) for aggregation and reporting; one record
per line keeps the ingestion path streaming.

Event kinds written today:

- `stage_enter` -- one record per workflow label transition, emitted
  by `GitHubClient._emit_stage_enter` alongside the audit event of
  the same name.
- `stage_evaluation` -- one record per `workflow._process_issue`
  dispatch, carrying `stage` (the current workflow label, omitted
  when the issue has none), `duration_s`, and `result` (`"ok"` on a
  clean return, `"error"` when the handler raised). Backlog-skips
  short-circuit before the timing wrapper and are NOT recorded.
- `agent_exit` -- one record per tracked agent invocation, written
  from `workflow._run_agent_tracked` with parsed usage / cost. When the
  opt-in `TRACK_SKILL_TRIGGERS` switch is on it additionally carries the
  agent's triggered skills (`skills_triggered` / `skills_triggered_count`
  / `skills_available`); with the switch off (the default) those keys are
  absent and the record shape is unchanged.
- `repo_skill_catalog` -- one repo-level record per tick per spec,
  written from `orchestrator.skill_catalog._emit_repo_skill_catalog`
  (driven by `workflow.tick`). Enumerates the `SKILL.md` definitions the
  target repo carries on its base ref and carries `base_branch`,
  `remote_name`, `skills_available` (the deduped skill names), and the
  optional `skill_paths` (name -> source paths). Not issue-scoped, so its
  `issue` is the sentinel `0`.

`ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`, the libpq URL
for the analytics Postgres service (`ANALYTICS_DB_URL`), and the
skill-trigger opt-in (`TRACK_SKILL_TRIGGERS`, default off) are parsed
here from the environment, not in `orchestrator.config`, so the sink
owns its own configuration surface and `config` does not pull analytics
defaults in transitively. The `orchestrator.analytics` facade binds the
parsed values as its own module attributes at import (see the package
`__init__`); every recorder here reads them back off that facade at call
time via `_live_settings`, so a test that patches `analytics.*` or
reloads the package sees the fresh value. `append_record` is a no-op
when `ANALYTICS_LOG_PATH` is None. `prune_old_records` removes records
older than `ANALYTICS_RETENTION_DAYS`; it is a no-op when the sink is
disabled or retention is non-positive (keep forever). `main._run_tick`
calls `prune_with_retention_logging` once per polling tick after every
configured repo drains, so retention is applied without operator
intervention; that wrapper delegates to `prune_old_records`, swallowing
exceptions and logging the removed-record count.

A separate, opt-in trajectory sink lives beside the analytics sink.
`TRAJECTORY_LOG_PATH` is parsed here too but defaults *off*: unset /
empty / `off` / `disabled` / `none` all disable it (unlike
`ANALYTICS_LOG_PATH`, which defaults to a path under `config.LOG_DIR`).
When enabled it gates an independent JSONL file for per-run reasoning
trajectories, pruned by `TRAJECTORY_RETENTION_DAYS` with the same
semantics as `ANALYTICS_RETENTION_DAYS` (default 90; non-positive keeps
forever). Its producer is `record_agent_exit` (via
`_maybe_record_trajectory`): when the sink is on it parses the run's
trajectory from the same stdout, redacts and head/tail truncates every
free-text field, denormalizes the `UsageMetrics` it already parsed for
the baseline record into a `run_usage` summary (plus claude's per-turn
`turns[]`), and appends one `agent_trajectory` record -- all behind
its own fail-open guard so it never disturbs the baseline `agent_exit`
usage record. `append_trajectory_record` / `prune_trajectory_records` share
the append/prune discipline of their analytics counterparts (reopen
append per record, `mkdir -p` parents, `OSError` downgraded to a
warning, malformed lines preserved on prune) but hold a dedicated file
lock and never touch `ANALYTICS_LOG_PATH`, the analytics Postgres sync,
or the dashboard rollups -- the two sinks are fully independent files.
The pinned GitHub state on each issue is the authoritative durable
state -- this sink is local-filesystem observability and may be
truncated or deleted at any time without affecting workflow
correctness.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator import usage
from orchestrator.agents import AgentResult

# The `orchestrator.analytics` package instance that imported this module.
# `tests/test_analytics.py::_reload` pops + re-imports `orchestrator.analytics`
# to land a patched env, leaving a divergent package in `sys.modules` while
# every other module (workflow, the conftest sink-disable, the recorder tests)
# still holds the original; the package `__init__` evicts any cached
# `_recording` before importing so each package instance gets its own copy of
# this module. Binding the facade to *that* instance here -- not resolving it
# off `sys.modules` at call time -- is what keeps the recorders reading the
# same sink knobs the caller patched, on either side of a reload. Captured at
# import while the package is still initializing (it is a stable reference; its
# attributes are read later, once bound).
_facade = sys.modules[__package__]

# Serializes filesystem ops on `ANALYTICS_LOG_PATH` so a concurrent
# `prune_old_records` (read + rewrite via `os.replace`) cannot drop an
# `append_record` that landed between the prune's read and replace.
# Both operations are short and IO-bound; a single process-local lock
# is sufficient because the sink path is single-writer per orchestrator
# process by design (operators run one orchestrator per host). The
# scheduler workers that drove the race fan out across threads inside
# the SAME process, so this lock closes the window without needing a
# filesystem-level fcntl.
_FILE_LOCK = threading.Lock()

# A dedicated lock for the trajectory sink so its append / prune
# serialize against each other (the same read-vs-replace race the
# analytics lock closes) but NOT against the analytics file -- the two
# sinks are independent paths and must not block one another.
_TRAJECTORY_FILE_LOCK = threading.Lock()

# Log under the package's public logger name (not `__name__`) so the
# facade's re-exported `analytics.log` is this exact logger: existing log
# output and the `assertLogs(analytics.log)` targets in the tests stay
# unchanged after the recorders moved out of the package `__init__`.
log = logging.getLogger(__package__)

# Case-insensitive values that switch an analytics path setting off.
_DISABLED_SENTINELS = ("off", "disabled", "none")

# Skill name -> the `SKILL.md` source paths that define it, carried by a
# `repo_skill_catalog` record.
_SkillPaths = dict[str, list[str]]

# One prune scan's outcome: the JSONL lines retained and the count removed.
_KeptRemoved = tuple[list[str], int]


def _parse_log_path() -> Optional[Path]:
    """Resolve `ANALYTICS_LOG_PATH` from the environment.

    Unset -> default under `config.LOG_DIR` (already covered by the
    `logs/` .gitignore rule). Empty value and the sentinels `off` /
    `disabled` / `none` (case-insensitive) disable the sink entirely;
    `append_record` and `prune_old_records` become silent no-ops in
    that mode and no file is ever opened.

    `config` is imported at call time -- not bound at module import -- so
    the `orchestrator.analytics` facade re-parses against the current
    `orchestrator.config` when a test pops + reloads both packages in
    lockstep to land a patched `LOG_DIR`.
    """
    from orchestrator import config
    raw = os.environ.get("ANALYTICS_LOG_PATH")
    if raw is None:
        return config.LOG_DIR / "analytics.jsonl"
    stripped = raw.strip()
    if not stripped or stripped.lower() in _DISABLED_SENTINELS:
        return None
    return Path(stripped)


def _parse_retention_days() -> int:
    """Resolve `ANALYTICS_RETENTION_DAYS` from the environment.

    Default 90 days. 0 (or any non-positive value) keeps raw data
    indefinitely -- `prune_old_records` becomes a no-op so operators
    can opt out of cleanup without disabling the sink itself.
    """
    return int(os.environ.get("ANALYTICS_RETENTION_DAYS", "90"))


def _parse_db_url() -> Optional[str]:
    """Resolve `ANALYTICS_DB_URL` from the environment.

    Unset / empty value and the sentinels `off` / `disabled` / `none`
    (case-insensitive) disable the Postgres surfaces (sync + read
    model) entirely; a real URL passes through verbatim so a libpq
    connection string is the single-knob endpoint contract. The
    orchestrator's polling tick does not read this var, so an unset
    value has no effect on workflow correctness. Matches
    `ANALYTICS_LOG_PATH`'s disable knob so the two can be turned off
    together with parallel spellings.
    """
    raw = os.environ.get("ANALYTICS_DB_URL", "").strip()
    if not raw or raw.lower() in _DISABLED_SENTINELS:
        return None
    return raw


def _parse_track_skill_triggers() -> bool:
    """Resolve `TRACK_SKILL_TRIGGERS` from the environment.

    Default off. When on, `record_agent_exit` runs the skill-trigger
    extractor (`usage.parse_agent_skills`) and folds `skills_triggered` /
    `skills_triggered_count` / `skills_available` into the `agent_exit`
    record. The switch defaults off *because* the sink itself is default-on
    (`ANALYTICS_LOG_PATH` -> `LOG_DIR/analytics.jsonl`): an on-by-default
    switch would silently add skill fields to every default install's
    records, breaking the "absent opt-in -> today's record shape"
    guarantee. Truthy spellings match `orchestrator.config`'s other boolean
    knobs: `1` / `true` / `on` / `yes` (case-insensitive).
    """
    return os.environ.get("TRACK_SKILL_TRIGGERS", "off").strip().lower() in (
        "1", "true", "on", "yes",
    )


def _parse_trajectory_log_path() -> Optional[Path]:
    """Resolve `TRAJECTORY_LOG_PATH` from the environment.

    Opt-in / default off: unlike `ANALYTICS_LOG_PATH` (which defaults to
    a path under `config.LOG_DIR`), an *unset* `TRAJECTORY_LOG_PATH`
    disables the trajectory sink. Empty value and the sentinels `off` /
    `disabled` / `none` (case-insensitive) also disable it; any other
    value is the explicit opt-in path. When disabled,
    `append_trajectory_record` and `prune_trajectory_records` are silent
    no-ops and no file is ever opened.
    """
    raw = os.environ.get("TRAJECTORY_LOG_PATH")
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped or stripped.lower() in _DISABLED_SENTINELS:
        return None
    return Path(stripped)


def _parse_trajectory_retention_days() -> int:
    """Resolve `TRAJECTORY_RETENTION_DAYS` from the environment.

    Default 90 days, matching `ANALYTICS_RETENTION_DAYS`. 0 (or any
    non-positive value) keeps trajectories indefinitely --
    `prune_trajectory_records` becomes a no-op so operators can opt out
    of cleanup without disabling the sink itself.
    """
    return int(os.environ.get("TRAJECTORY_RETENTION_DAYS", "90"))


def _live_settings():
    """Return the `orchestrator.analytics` facade to read live sink settings.

    The sink knobs -- `ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS` /
    `TRAJECTORY_LOG_PATH` / `TRAJECTORY_RETENTION_DAYS` / `TRACK_SKILL_TRIGGERS`,
    the `_TRAJECTORY_RECORD_BUDGET` / `_TRAJECTORY_FIELD_HEAD` /
    `_TRAJECTORY_FIELD_TAIL` caps, and the re-exported recorders /
    append primitives -- are patched on the facade
    (`patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`, the autouse conftest
    sink-disable). Reading them off `_facade` -- the package instance that
    imported this module, not a value captured at import -- at call time is
    what lets a patched value take effect and keeps a recorder that internally
    calls another recorder (`record_stage_enter` -> `append_record`,
    `prune_with_retention_logging` -> `prune_old_records`) interceptable
    through the facade, the same late-binding `workflow.py`'s stage modules use.
    """
    return _facade


def build_record(
    *,
    repo: str,
    issue: int,
    event: str,
    stage: Optional[str] = None,
    **extras: Any,
) -> dict:
    """Build a single analytics record.

    `ts` is the current UTC time at second precision in ISO-8601 form.
    `stage` and any extra whose value is None are dropped so callers can
    pass optional context unconditionally without polluting records that
    don't carry them.
    """
    rec: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": repo,
        "issue": int(issue),
        "event": event,
    }
    if stage is not None:
        rec["stage"] = stage
    for key, field_value in extras.items():
        if field_value is not None:
            rec[key] = field_value
    return rec


def _append_jsonl_record(
    path: Optional[Path], lock: threading.Lock, record: dict
) -> None:
    """Append one JSONL line to `path` under `lock`; no-op when `path` is
    None.

    Shared core for the analytics and trajectory sinks: each passes its
    own path and dedicated lock so the two files never serialize against
    one another. OSError is logged and swallowed so a misconfigured path
    (read-only mount, disk full, permission failure) cannot stop the
    per-issue tick from making progress.

    Holds `lock` around the actual filesystem ops so a concurrent prune
    cannot rewrite the file (via `os.replace`) between this append's open
    and write; otherwise the appended record would be written to the
    soon-unlinked inode and silently lost. Scheduler workers fan out
    across threads in the same process, so the race is real on the
    multi-issue path. JSON serialization is done outside the lock to keep
    the critical section short.
    """
    if path is None:
        return
    serialized = f"{json.dumps(record, sort_keys=True)}\n"
    try:
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(serialized)
    except OSError as error:
        log.warning("could not write record to %s: %s", path, error)


def append_record(record: dict) -> None:
    """Append one JSONL line to `ANALYTICS_LOG_PATH` if configured.

    No-op when the sink is disabled. OSError is logged and swallowed so
    a misconfigured path (read-only mount, disk full, permission
    failure) cannot stop the per-issue tick from making progress; the
    pinned state on GitHub remains correct regardless.

    Holds `_FILE_LOCK` around the actual filesystem ops so a concurrent
    `prune_old_records` cannot rewrite the file (via `os.replace`)
    between this append's open and write; otherwise the appended record
    would be written to the soon-unlinked inode and silently lost.
    Scheduler workers fan out across threads in the same process, so the
    race is real on the multi-issue path. JSON serialization is done
    outside the lock to keep the critical section short.
    """
    _append_jsonl_record(_live_settings().ANALYTICS_LOG_PATH, _FILE_LOCK, record)


def record_stage_enter(*, repo: str, issue: int, stage: str) -> None:
    """Append the `stage_enter` analytics record emitted alongside the audit
    event of the same name.

    Centralized so `GitHubClient._emit_stage_enter` and the in-memory fake
    in `tests/fakes.py` agree on the record shape without re-inlining the
    `build_record`/`append_record` pair. Disabled-sink behavior is
    inherited from `append_record` (no-op when the sink is off).
    """
    _live_settings().append_record(
        build_record(
            repo=repo,
            issue=int(issue),
            event="stage_enter",
            stage=stage,
        )
    )


def record_stage_evaluation(
    *,
    repo: str,
    issue: int,
    stage: Optional[str],
    duration_s: float,
    result: str,
) -> None:
    """Append one `stage_evaluation` analytics record for a dispatch.

    Centralized so `workflow._process_issue` does not re-inline the
    `build_record`/`append_record` pair. `stage` is `None` when the
    issue has no workflow label (the `_handle_pickup` arc) -- `build_record`
    drops the field rather than encoding "no stage" as a sentinel string.
    Disabled-sink behavior is inherited from `append_record`.
    """
    _live_settings().append_record(
        build_record(
            repo=repo,
            issue=int(issue),
            event="stage_evaluation",
            stage=stage,
            duration_s=duration_s,
            result=result,
        )
    )


def record_repo_skill_catalog(
    *,
    repo: str,
    base_branch: str,
    remote_name: str,
    skills_available: list[str],
    skill_paths: Optional[_SkillPaths] = None,
) -> None:
    """Append one `repo_skill_catalog` analytics record for a spec.

    Repo-level, not issue-scoped: `issue` is the sentinel `0` so the
    record still satisfies the `ts` / `repo` / `issue` / `event` envelope
    that both the JSONL sink and the Postgres `analytics_events` schema
    require, with no DDL change -- `base_branch`, `remote_name`,
    `skills_available`, and `skill_paths` all land in the `extras` JSONB
    column. `skill_paths` is dropped when None (`build_record` drops None
    extras), so an empty catalog records `skills_available: []` -- the
    "scanned, found none" signal -- without an empty `skill_paths`.
    Disabled-sink behavior is inherited from `append_record` (no-op when
    the sink is off). Centralized here so the producer in
    `orchestrator.skill_catalog` does not re-inline the record shape.
    """
    _live_settings().append_record(
        build_record(
            repo=repo,
            issue=0,
            event="repo_skill_catalog",
            base_branch=base_branch,
            remote_name=remote_name,
            skills_available=skills_available,
            skill_paths=skill_paths,
        )
    )


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


@dataclass
class _CodexCatalog:
    """Out-of-band capabilities missing from Codex's JSON stream."""

    available_skills: Optional[list[str]] = None
    tools: Optional[list[str]] = None


@dataclass(frozen=True)
class _AgentExitSkillFields:
    """Normalized optional skill fields for an `agent_exit` event."""

    skills_triggered: Optional[list[str]] = None
    skills_triggered_count: Optional[int] = None
    skills_available: Optional[list[str]] = None


def _parse_agent_exit_usage(
    context: _AgentExitContext,
) -> Optional[usage.UsageMetrics]:
    """Parse usage and attach it to the result, failing open on bad streams."""
    try:
        metrics = usage.parse_agent_usage(
            context.backend,
            context.agent_result.stdout,
            fallback_model=context.fallback_model,
        )
    except Exception:
        log.exception(
            "issue=#%d analytics: parse_agent_usage(%s) failed; "
            "skipping record",
            context.issue,
            context.backend,
        )
        return None
    context.agent_result.usage = metrics
    return metrics


def _discover_codex_skills(
    context: _AgentExitContext,
    skill_catalog: Any,
) -> Optional[list[str]]:
    """Read Codex's offered skills when either sink needs them."""
    settings = _live_settings()
    if context.cwd is None or not (
        settings.TRACK_SKILL_TRIGGERS or settings.TRAJECTORY_LOG_PATH is not None
    ):
        return None
    return list(skill_catalog.discover_local_skills(context.cwd)) or None


def _discover_codex_tools(skill_catalog: Any) -> Optional[list[str]]:
    """Read Codex's baseline tools only for trajectory records."""
    if _live_settings().TRAJECTORY_LOG_PATH is None:
        return None
    return list(skill_catalog.discover_codex_tools()) or None


def _populate_codex_catalog(
    context: _AgentExitContext,
    catalog: _CodexCatalog,
) -> None:
    """Fill Codex capabilities in discovery order."""
    from orchestrator import skill_catalog
    catalog.available_skills = _discover_codex_skills(context, skill_catalog)
    catalog.tools = _discover_codex_tools(skill_catalog)


def _discover_codex_catalog(context: _AgentExitContext) -> _CodexCatalog:
    """Discover Codex capabilities needed by enabled analytics sinks."""
    catalog = _CodexCatalog()
    if context.backend != "codex":
        return catalog
    try:
        _populate_codex_catalog(context, catalog)
    except Exception:
        log.exception(
            "issue=#%d analytics: codex out-of-band discovery failed; "
            "leaving skills_available / tools empty",
            context.issue,
        )
    return catalog


def _normalize_agent_exit_skills(
    parsed_skills: usage.SkillTriggers,
    codex_catalog: _CodexCatalog,
) -> _AgentExitSkillFields:
    """Convert parser output into optional event fields."""
    skills_triggered = list(parsed_skills.triggered) or None
    skills_triggered_count = (
        sum(parsed_skills.trigger_counts.values())
        if skills_triggered
        else None
    )
    skills_available = (
        list(parsed_skills.available) or codex_catalog.available_skills
    )
    return _AgentExitSkillFields(
        skills_triggered=skills_triggered,
        skills_triggered_count=skills_triggered_count,
        skills_available=skills_available,
    )


def _read_agent_exit_skills(
    context: _AgentExitContext,
    codex_catalog: _CodexCatalog,
) -> _AgentExitSkillFields:
    """Parse and normalize skill fields for an enabled run."""
    parsed_skills = usage.parse_agent_skills(
        context.backend,
        context.agent_result.stdout,
    )
    return _normalize_agent_exit_skills(parsed_skills, codex_catalog)


def _parse_agent_exit_skills(
    context: _AgentExitContext,
    codex_catalog: _CodexCatalog,
) -> _AgentExitSkillFields:
    """Parse opt-in skill fields without risking the baseline event."""
    if not _live_settings().TRACK_SKILL_TRIGGERS:
        return _AgentExitSkillFields()
    try:
        return _read_agent_exit_skills(context, codex_catalog)
    except Exception:
        log.exception(
            "issue=#%d analytics: parse_agent_skills(%s) failed; "
            "emitting record without skill fields",
            context.issue,
            context.backend,
        )
        return _AgentExitSkillFields()


def _build_agent_exit_record(
    context: _AgentExitContext,
    metrics: usage.UsageMetrics,
    skill_fields: _AgentExitSkillFields,
) -> dict:
    """Build the allowlisted baseline event without raw run content."""
    return build_record(
        repo=context.repo,
        issue=context.issue,
        event="agent_exit",
        stage=context.stage,
        agent_role=context.agent_role,
        backend=context.backend,
        agent_spec=context.agent_spec,
        resume_session_id=context.resume_session_id,
        session_id=context.agent_result.session_id,
        review_round=context.review_round,
        retry_count=context.retry_count,
        duration_s=context.duration_s,
        exit_code=context.agent_result.exit_code,
        timed_out=context.agent_result.timed_out,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        cached_tokens=metrics.cached_tokens,
        cache_read_tokens=metrics.cache_read_tokens,
        cache_write_tokens=metrics.cache_write_tokens,
        models=list(metrics.models),
        turns=metrics.turns,
        cost_usd=metrics.cost_usd,
        cost_source=metrics.cost_source,
        skills_triggered=skill_fields.skills_triggered,
        skills_triggered_count=skill_fields.skills_triggered_count,
        skills_available=skill_fields.skills_available,
    )


def _persist_agent_exit(
    context: _AgentExitContext,
    metrics: usage.UsageMetrics,
    skill_fields: _AgentExitSkillFields,
    codex_catalog: _CodexCatalog,
) -> None:
    """Write the baseline event, then the independently guarded trajectory."""
    _live_settings().append_record(
        _build_agent_exit_record(context, metrics, skill_fields)
    )
    _maybe_record_trajectory(context, metrics, codex_catalog)


def record_agent_exit(
    *,
    repo: str,
    issue: int,
    stage: str,
    agent_role: str,
    backend: str,
    agent_spec: Optional[str],
    resume_session_id: Optional[str],
    result: AgentResult,
    duration_s: float,
    review_round: Optional[int],
    retry_count: Optional[int],
    fallback_model: Optional[str] = None,
    prompt: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Optional[list[str]]:
    """Parse usage from agent stdout and append a single `agent_exit` record.

    Usage parsing is the only failure that suppresses the baseline event. A
    successful parse is attached to `result.usage` even when the analytics sink
    is disabled, allowing workflow callers to reuse the structured metrics.
    `fallback_model` supplies Codex's configured model when its stream omits one.

    Skill parsing and Codex capability discovery have independent fail-open
    guards, so either can fail without dropping usage and cost. The baseline
    builder allowlists context, session, usage, and normalized skill fields; it
    never stores raw stdout, stderr, prompts, or worktree contents.

    The prompt is passed only to the opt-in trajectory sink, where
    `_maybe_record_trajectory` redacts and truncates every free-text field under
    its own fail-open guard. Baseline persistence always happens before that
    optional work.

    Returns the distinct triggered skill names (first-seen order) so the
    caller can emit per-skill audit events without reparsing stdout, or
    `None` when nothing fired, the switch is off, the skill parse failed,
    or the usage parse failed (no record was written).
    """
    context = _AgentExitContext(
        repo=repo,
        issue=int(issue),
        stage=stage,
        agent_role=agent_role,
        backend=backend,
        agent_spec=agent_spec,
        resume_session_id=resume_session_id,
        agent_result=result,
        duration_s=duration_s,
        review_round=review_round,
        retry_count=retry_count,
        fallback_model=fallback_model,
        prompt=prompt,
        cwd=cwd,
    )
    metrics = _parse_agent_exit_usage(context)
    if metrics is None:
        return None
    codex_catalog = _discover_codex_catalog(context)
    skill_fields = _parse_agent_exit_skills(context, codex_catalog)
    _persist_agent_exit(context, metrics, skill_fields, codex_catalog)
    return skill_fields.skills_triggered


# --- trajectory recording ---------------------------------------------------

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


def _redact_tree(node: Any, redact) -> Any:
    """Recursively redact every string leaf of a tool payload.

    Applied before JSON serialization so a multiline / control-character
    secret in a tool input or result is masked on the raw leaf:
    `json.dumps` would otherwise escape its newlines (a real `\\n` becomes
    the two-character `\\n` escape), leaving `_redact_secrets`' literal
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


def _redact_and_truncate(field_value: Any, redact) -> Optional[str]:
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
    redact,
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


def _trajectory_step(step: usage.TrajectoryStep, redact) -> dict[str, Any]:
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
    redact,
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
    redact,
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


def prune_with_retention_logging() -> None:
    """Drop analytics records past `ANALYTICS_RETENTION_DAYS` and log the
    outcome. Intended for the per-tick caller in `main._run_tick`.

    A no-op when the sink is disabled or retention is non-positive (the
    documented "keep raw data indefinitely" knob); `prune_old_records`
    itself handles the absent-file / unparseable-line / IO-failure cases.
    A runaway programming error here must not abort the polling loop --
    analytics is observability, never authoritative workflow state -- so
    any escape is logged and swallowed. Per-tick cadence is cheap: the
    helper reads the file at most once and only rewrites it when at
    least one record is older than the retention window.
    """
    try:
        removed = _live_settings().prune_old_records()
    except Exception:
        log.exception("analytics retention prune raised; continuing")
        return
    if removed:
        log.info("analytics retention prune removed %d record(s)", removed)


def prune_old_records(*, now: Optional[datetime] = None) -> int:
    """Remove records whose `ts` is older than `ANALYTICS_RETENTION_DAYS`.

    Reads the `ANALYTICS_LOG_PATH` / `ANALYTICS_RETENTION_DAYS` bound on the
    `orchestrator.analytics` facade (parsed from the env at import).

    Returns the number of records removed. No-op (returns 0) when the
    sink is disabled, retention is non-positive (keep forever), or the
    file does not exist yet. `now` defaults to the current UTC time and
    is parameter-overridable so tests can pin the comparison point.

    Records whose `ts` is missing, not a string, or unparseable are
    preserved verbatim -- the prune step does not silently drop malformed
    data; an operator can clean it up. Likewise lines that are not valid
    JSON survive the rewrite.

    The rewrite goes through a temp file in the same directory followed
    by `os.replace` so a crash mid-prune cannot truncate the analytics
    file.

    Holds `_FILE_LOCK` across the read + rewrite so a concurrent
    `append_record` cannot land between the read and the `os.replace`
    -- without this, an append that observed the old inode after we
    read but before `os.replace` would write to the soon-unlinked inode
    and be silently lost. Scheduler workers may still be running when
    the polling loop calls this between ticks, so serializing with
    `append_record` is what keeps that prune-window invisible.
    """
    settings = _live_settings()
    return _prune_jsonl_records(
        settings.ANALYTICS_LOG_PATH, settings.ANALYTICS_RETENTION_DAYS,
        _FILE_LOCK, now,
    )


def _probe_exists(path: Path) -> bool:
    """True if `path` exists; False when it is absent or the probe raised.

    `Path.exists()` re-raises OSErrors that do not mean "absent" -- e.g.
    ENAMETOOLONG on a misconfigured path -- so the probe itself must be
    guarded, otherwise it escapes the per-tick caller. A probe failure is
    logged and treated as "absent" (a no-op prune), same as a read/rewrite
    OSError.
    """
    try:
        return path.exists()
    except OSError as error:
        log.warning("could not probe %s for prune: %s", path, error)
        return False


def _prune_timestamp(raw_line: str) -> Optional[datetime]:
    """Parse a JSONL record timestamp, returning None for kept malformed data."""
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    raw_timestamp = record.get("ts") if isinstance(record, dict) else None
    if not isinstance(raw_timestamp, str):
        return None
    try:
        timestamp = datetime.fromisoformat(raw_timestamp)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _normalized_jsonl_line(raw_line: str) -> str:
    if raw_line.endswith("\n"):
        return raw_line
    return f"{raw_line}\n"


@dataclass
class _PruneScan:
    """Mutable partition of retained and expired JSONL records."""

    kept: list[str] = field(default_factory=list)
    removed: int = 0

    def add(self, raw_line: str, cutoff: datetime) -> None:
        if not raw_line.strip():
            return
        timestamp = _prune_timestamp(raw_line)
        if timestamp is not None and timestamp < cutoff:
            self.removed += 1
            return
        self.kept.append(_normalized_jsonl_line(raw_line))


def _read_kept_records(
    path: Path, cutoff: datetime,
) -> Optional[_KeptRemoved]:
    """Split `path`'s lines into (kept, removed_count) by the `cutoff` time.

    A record is removed only when its `ts` parses to a time strictly before
    `cutoff`. Records whose `ts` is missing / non-string / unparseable, and
    lines that are not valid JSON, are kept verbatim so the prune never
    silently drops data an operator can clean up; a naive `ts` is read as UTC
    to match the writer's forward-compat behavior. Returns None when the read
    itself raises OSError, which the caller turns into a logged no-op.
    """
    scan = _PruneScan()
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                scan.add(raw_line, cutoff)
    except OSError as error:
        log.warning("could not read file %s for prune: %s", path, error)
        return None
    return scan.kept, scan.removed


def _unlink_quietly(path: str) -> None:
    """Remove `path`, ignoring a missing or unremovable file.

    Best-effort cleanup of the prune's temp file when the rewrite fails; an
    unlink failure leaves an orphaned `.prune.*.tmp` but never masks the write
    error that triggered the cleanup.
    """
    try:
        os.unlink(path)
    except OSError:
        pass


def _flush_fd_and_replace(
    tmp_fd: int, tmp_path: str, path: Path, lines: list[str],
) -> None:
    """Write `lines` through `tmp_fd`, then atomically replace `path`."""
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.replace(tmp_path, str(path))


def _atomic_rewrite(path: Path, lines: list[str]) -> None:
    """Replace `path`'s contents with `lines` via a temp file + `os.replace`.

    The temp file lands in `path.parent` so `os.replace` is a same-filesystem
    atomic rename: a crash mid-write cannot truncate the original. On any
    write / replace OSError the partial temp file is unlinked (best-effort)
    before the error propagates, so a failed prune leaves neither a truncated
    original nor an orphaned temp file.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f"{path.name}.prune.",
        suffix=".tmp",
    )
    try:
        _flush_fd_and_replace(tmp_fd, tmp_path, path, lines)
    except OSError:
        _unlink_quietly(tmp_path)
        raise


def _rewrite_pruned_file(
    path: Path, cutoff: datetime, lock: threading.Lock,
) -> int:
    """Under `lock`, drop records older than `cutoff` and return the count.

    The lock is held across the read + rewrite so a concurrent append cannot
    land on the soon-unlinked inode; every filesystem touch downgrades OSError
    to a logged no-op.
    """
    with lock:
        # Re-check existence under the lock: a concurrent operator `rm`
        # between the pre-lock probe and acquiring the lock would
        # otherwise let `path.open` raise an unhandled FileNotFoundError.
        if not _probe_exists(path):
            return 0
        kept_removed = _read_kept_records(path, cutoff)
        if kept_removed is None:
            return 0
        kept, removed = kept_removed
        if removed == 0:
            return 0
        try:
            _atomic_rewrite(path, kept)
        except OSError as error:
            log.warning(
                "could not rewrite file %s after prune: %s", path, error
            )
            return 0
        return removed


def _prune_jsonl_records(
    path: Optional[Path],
    days: int,
    lock: threading.Lock,
    now: Optional[datetime],
) -> int:
    """Remove records whose `ts` is older than `days` from `path` under
    `lock`.

    Shared core for the analytics and trajectory prune wrappers. Returns
    the number of records removed; a no-op (returns 0) when `path` is
    None (sink disabled), `days` is non-positive (keep forever), or the
    file does not exist. Malformed lines -- not valid JSON, or a record
    whose `ts` is missing / non-string / unparseable -- are preserved
    verbatim so the prune never silently drops data an operator can
    clean up. The rewrite goes through a temp file plus `os.replace` so
    a crash mid-prune cannot truncate the file, and `lock` is held
    across the read + rewrite so a concurrent append cannot land on the
    soon-unlinked inode.

    Every filesystem touch -- the existence probes (`_probe_exists`), the
    read (`_read_kept_records`), and the rewrite (`_atomic_rewrite`) --
    downgrades OSError to a logged no-op, so a misconfigured path (e.g.
    ENAMETOOLONG) never escapes to the per-tick caller.
    """
    if path is None or days <= 0:
        return 0
    # Pre-lock probe for the fast zero-cost no-op path on a disabled sink.
    if not _probe_exists(path):
        return 0

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return _rewrite_pruned_file(path, cutoff, lock)


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
