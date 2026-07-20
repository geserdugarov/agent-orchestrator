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
  / `skills_available`), the per-load evidence tier (`skills_evidence`,
  `confirmed` / `inferred`), and any path-only references the run made
  without loading a skill (`skills_incidental` / `skills_incidental_count`);
  with the switch off (the default) those keys are absent and the record
  shape is unchanged.
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
when `ANALYTICS_LOG_PATH` is None. By-age retention pruning for both
sinks lives in the sibling `orchestrator.analytics._retention`
(`prune_old_records` / `prune_trajectory_records` and the per-tick
`prune_with_retention_logging` wrapper `main._run_tick` calls); it reuses
this module's `_FILE_LOCK` and `_live_settings` so a prune's read +
rewrite cannot race an `append_record` onto the soon-unlinked inode.

A separate, opt-in trajectory sink lives beside the analytics sink in
`orchestrator.analytics._trajectories`. `TRAJECTORY_LOG_PATH` /
`TRAJECTORY_RETENTION_DAYS` are parsed here too (unset / empty / `off` /
`disabled` / `none` disable the sink, which defaults *off* -- unlike
`ANALYTICS_LOG_PATH`), but the serialization, redaction / truncation,
budgeting, and file append machinery live in `_trajectories` (its prune
wrapper, with the analytics one, lives in `_retention`).
Its producer is `record_agent_exit`, which hands off to
`_trajectories._maybe_record_trajectory` once the baseline `agent_exit`
record is persisted. The pinned GitHub state on each issue is the
authoritative durable state -- both sinks are local-filesystem
observability and may be truncated or deleted at any time without
affecting workflow correctness.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
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
    `skills_triggered_count` / `skills_available` / `skills_evidence` /
    `skills_incidental` / `skills_incidental_count` into the `agent_exit`
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
    """Convert parser output into optional event fields.

    Incidental references stay out of `skills_triggered` / the count (and thus
    the `skill_triggered` audit events) -- they ride the separate
    `skills_incidental` / `skills_incidental_count` keys. `skills_evidence`
    persists the per-load tier the parser assigned.
    """
    skills_triggered = list(parsed_skills.triggered) or None
    skills_triggered_count = (
        sum(parsed_skills.trigger_counts.values())
        if skills_triggered
        else None
    )
    skills_available = (
        list(parsed_skills.available) or codex_catalog.available_skills
    )
    skills_incidental = list(parsed_skills.incidental) or None
    skills_incidental_count = (
        sum(parsed_skills.incidental_counts.values())
        if skills_incidental
        else None
    )
    return _AgentExitSkillFields(
        skills_triggered=skills_triggered,
        skills_triggered_count=skills_triggered_count,
        skills_available=skills_available,
        skills_evidence=dict(parsed_skills.evidence) or None,
        skills_incidental=skills_incidental,
        skills_incidental_count=skills_incidental_count,
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
        skills_evidence=skill_fields.skills_evidence,
        skills_incidental=skill_fields.skills_incidental,
        skills_incidental_count=skill_fields.skills_incidental_count,
    )


def _persist_agent_exit(
    context: _AgentExitContext,
    metrics: usage.UsageMetrics,
    skill_fields: _AgentExitSkillFields,
    codex_catalog: _CodexCatalog,
) -> None:
    """Write the baseline event, then the independently guarded trajectory.

    The trajectory sink is reached through the facade's `_trajectories`
    submodule rather than a direct import so a `_recording` instance always
    dispatches to the same package instance's trajectory recorder -- keeping
    the `_reload` A/B isolation `_live_settings` documents.
    """
    facade = _live_settings()
    facade.append_record(_build_agent_exit_record(context, metrics, skill_fields))
    facade._trajectories._maybe_record_trajectory(context, metrics, codex_catalog)


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
    `_trajectories._maybe_record_trajectory` redacts and truncates every
    free-text field under its own fail-open guard. Baseline persistence always
    happens before that optional work.

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
