# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Project-local analytics sink.

Append-only JSONL records keyed by `ts`, `repo`, `issue`, `event`, and
optional `stage`. Distinct from the audit event log at
`config.EVENT_LOG_PATH`: the audit log is wired through
`GitHubClient.emit_event` for stage transitions / agent lifecycle
events, while this analytics sink is a foundation layer for future
aggregation that can be opted in or out independently. The raw JSONL
is intended to be ingested later into a structured database
(SQLite / DuckDB / Postgres) for aggregation and reporting; one
record per line keeps the ingestion path streaming.

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
skill-trigger opt-in (`TRACK_SKILL_TRIGGERS`, default off) are parsed at
import here -- not in `orchestrator.config` -- so the sink owns its own
configuration surface and `config` does not pull analytics defaults in
transitively. `append_record` is a no-op when
`ANALYTICS_LOG_PATH` is None. `prune_old_records` removes records older
than `ANALYTICS_RETENTION_DAYS`; it is a no-op when the sink is
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
free-text field, and appends one `agent_trajectory` record -- all behind
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

The sink API lives in this `__init__.py` rather than a submodule so
the package's `config` binding and the `ANALYTICS_LOG_PATH` /
`ANALYTICS_RETENTION_DAYS` module attributes match the flat-module
behavior the package replaced: `tests/test_analytics.py` pops both
`orchestrator.config` and `orchestrator.analytics` from `sys.modules`
between cases and re-imports them in lockstep, and callers elsewhere
patch their already-imported `orchestrator.analytics` reference
(`patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`). A submodule
would re-bind `config` (and the sink settings) only when its own
module entry was popped, which would diverge from both patterns.
Future analytics surfaces that do NOT need that lockstep can land in
sibling submodules.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .. import config, usage
from ..agents import AgentResult

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

__all__ = [
    "ANALYTICS_DB_URL",
    "ANALYTICS_LOG_PATH",
    "ANALYTICS_RETENTION_DAYS",
    "TRACK_SKILL_TRIGGERS",
    "TRAJECTORY_LOG_PATH",
    "TRAJECTORY_RETENTION_DAYS",
    "append_record",
    "append_trajectory_record",
    "build_record",
    "config",
    "prune_old_records",
    "prune_trajectory_records",
    "prune_with_retention_logging",
    "record_agent_exit",
    "record_repo_skill_catalog",
    "record_stage_enter",
    "record_stage_evaluation",
]

log = logging.getLogger(__name__)


def _parse_log_path() -> Optional[Path]:
    """Resolve `ANALYTICS_LOG_PATH` from the environment.

    Unset -> default under `config.LOG_DIR` (already covered by the
    `logs/` .gitignore rule). Empty value and the sentinels `off` /
    `disabled` / `none` (case-insensitive) disable the sink entirely;
    `append_record` and `prune_old_records` become silent no-ops in
    that mode and no file is ever opened.
    """
    raw = os.environ.get("ANALYTICS_LOG_PATH")
    if raw is None:
        return config.LOG_DIR / "analytics.jsonl"
    stripped = raw.strip()
    if not stripped or stripped.lower() in ("off", "disabled", "none"):
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
    if not raw or raw.lower() in ("off", "disabled", "none"):
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
    if not stripped or stripped.lower() in ("off", "disabled", "none"):
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


# Sink configuration. Parsed at import so a fresh process picks up the
# operator's env immediately; tests patch these module attributes
# directly (`patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`).
ANALYTICS_LOG_PATH: Optional[Path] = _parse_log_path()
ANALYTICS_RETENTION_DAYS: int = _parse_retention_days()
ANALYTICS_DB_URL: Optional[str] = _parse_db_url()
TRACK_SKILL_TRIGGERS: bool = _parse_track_skill_triggers()
TRAJECTORY_LOG_PATH: Optional[Path] = _parse_trajectory_log_path()
TRAJECTORY_RETENTION_DAYS: int = _parse_trajectory_retention_days()


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
    for k, v in extras.items():
        if v is not None:
            rec[k] = v
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
    serialized = json.dumps(record, sort_keys=True) + "\n"
    try:
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(serialized)
    except OSError as e:
        log.warning("could not write record to %s: %s", path, e)


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
    _append_jsonl_record(ANALYTICS_LOG_PATH, _FILE_LOCK, record)


def record_stage_enter(*, repo: str, issue: int, stage: str) -> None:
    """Append the `stage_enter` analytics record emitted alongside the audit
    event of the same name.

    Centralized so `GitHubClient._emit_stage_enter` and the in-memory fake
    in `tests/fakes.py` agree on the record shape without re-inlining the
    `build_record`/`append_record` pair. Disabled-sink behavior is
    inherited from `append_record` (no-op when the sink is off).
    """
    append_record(
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
    append_record(
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
    skill_paths: Optional[dict[str, list[str]]] = None,
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
    append_record(
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
) -> Optional[list[str]]:
    """Parse usage from agent stdout and append a single `agent_exit` record.

    Pulled out of `workflow._run_agent_tracked` so the parse + append step
    has a single try/except boundary: a malformed JSONL stream from a
    flaky backend, an unknown-price model rev, or a transient IO failure
    on the sink path must NEVER propagate out of the wrapper -- the audit
    `agent_exit` is already emitted and the agent itself has exited.
    `append_record` is internally hardened against OSError; the helper
    here additionally guards the parse step.

    `fallback_model` is the configured-spec model name (from
    `workflow._configured_model`) the codex parser uses when no usage
    frame carries one; the claude parser ignores it (claude streams always
    include `message.model`).

    When `TRACK_SKILL_TRIGGERS` is on, the agent's triggered skills are
    parsed from the same stdout and folded into the record as
    `skills_triggered` / `skills_triggered_count` / `skills_available`.
    That parse rides its OWN inner try/except -- it must NOT share the
    usage-parse guard above, which `return`s and drops the whole record on
    failure: an opt-in skill-parser bug must never cost the baseline
    usage / cost record that ships today. On any skill-parse failure we log
    and fall through with the three fields left `None`, so `build_record`
    drops them and the record degrades to "agent_exit without skill
    fields," never a missing record. With the switch off the extractor
    never runs and the record stays byte-for-byte shape-compatible with
    today's.

    `prompt` is the orchestrator-built agent prompt. It is stored only as
    the redacted `user_input` of the opt-in trajectory record, and ONLY
    when `TRAJECTORY_LOG_PATH` is enabled -- the baseline `agent_exit`
    usage / cost record never carries it, so the default-install record
    shape is unchanged. When the trajectory sink is on, the run's
    trajectory is parsed from the same stdout, every free-text field is
    redacted and head/tail truncated, and a single `agent_trajectory`
    record is appended to the trajectory file. That work rides its OWN
    inner fail-open guard (`_maybe_record_trajectory`): a parser, redactor,
    or sink failure logs and is swallowed so it can never drop the baseline
    record above or the caller's `skill_triggered` audit events.

    Returns the distinct triggered skill names (first-seen order) so the
    caller can emit per-skill audit events without reparsing stdout, or
    `None` when nothing fired, the switch is off, the skill parse failed,
    or the usage parse failed (no record was written).
    """
    try:
        metrics = usage.parse_agent_usage(
            backend, result.stdout, fallback_model=fallback_model,
        )
    except Exception:
        log.exception(
            "issue=#%d analytics: parse_agent_usage(%s) failed; "
            "skipping record",
            issue, backend,
        )
        return None
    skills_triggered: Optional[list[str]] = None
    skills_triggered_count: Optional[int] = None
    skills_available: Optional[list[str]] = None
    if TRACK_SKILL_TRIGGERS:
        try:
            skills = usage.parse_agent_skills(backend, result.stdout)
            if skills.triggered:
                skills_triggered = list(skills.triggered)
                skills_triggered_count = sum(skills.trigger_counts.values())
            if skills.available:
                skills_available = list(skills.available)
        except Exception:
            log.exception(
                "issue=#%d analytics: parse_agent_skills(%s) failed; "
                "emitting record without skill fields",
                issue, backend,
            )
    append_record(
        build_record(
            repo=repo,
            issue=int(issue),
            event="agent_exit",
            stage=stage,
            agent_role=agent_role,
            backend=backend,
            agent_spec=agent_spec,
            resume_session_id=resume_session_id,
            session_id=result.session_id,
            review_round=review_round,
            retry_count=retry_count,
            duration_s=duration_s,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            cached_tokens=metrics.cached_tokens,
            cache_read_tokens=metrics.cache_read_tokens,
            cache_write_tokens=metrics.cache_write_tokens,
            models=list(metrics.models),
            turns=metrics.turns,
            cost_usd=metrics.cost_usd,
            cost_source=metrics.cost_source,
            skills_triggered=skills_triggered,
            skills_triggered_count=skills_triggered_count,
            skills_available=skills_available,
        )
    )
    _maybe_record_trajectory(
        repo=repo,
        issue=int(issue),
        stage=stage,
        agent_role=agent_role,
        backend=backend,
        result=result,
        prompt=prompt,
        review_round=review_round,
        retry_count=retry_count,
    )
    return skills_triggered


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
# write an unbounded line.
_TRAJECTORY_FIELD_HEAD = 2000
_TRAJECTORY_FIELD_TAIL = 2000
_TRAJECTORY_RECORD_BUDGET = 200_000


def _truncate_head_tail(text: str, head: int, tail: int) -> str:
    """Keep the first `head` + last `tail` chars of `text`, eliding the
    middle with a marker recording how many chars were dropped. Returns
    `text` unchanged when it already fits within `head + tail`."""
    if len(text) <= head + tail:
        return text
    elided = len(text) - head - tail
    return f"{text[:head]}\n...[{elided} chars elided]...\n{text[-tail:]}"


def _redact_tree(value: Any, redact) -> Any:
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
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_tree(v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_tree(v, redact) for v in value]
    return value


def _redact_and_truncate(value: Any, redact) -> Optional[str]:
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
    if value is None:
        return None
    if isinstance(value, str):
        text = redact(value)
    else:
        try:
            text = json.dumps(
                _redact_tree(value, redact), sort_keys=True, default=str,
            )
        except (TypeError, ValueError):
            text = str(_redact_tree(value, redact))
        text = redact(text)
    if not text:
        return None
    return _truncate_head_tail(
        text, _TRAJECTORY_FIELD_HEAD, _TRAJECTORY_FIELD_TAIL,
    )


def _build_trajectory_record(
    *,
    repo: str,
    issue: int,
    stage: str,
    agent_role: str,
    backend: str,
    session_id: Optional[str],
    prompt: Optional[str],
    trajectory: usage.AgentTrajectory,
    review_round: Optional[int],
    retry_count: Optional[int],
    redact,
) -> dict:
    """Assemble one redacted, truncated `agent_trajectory` record.

    `prompt` becomes the redacted `user_input`; `system_prompt`, each
    step's content, and the final `output` are redacted the same way.
    Each step is charged its full *serialized* size -- the JSON metadata
    (`kind` / `name` / `tool_id`) plus its truncated content, not merely
    `len(content)` -- so steps with empty or tiny content still consume the
    budget; once the running total crosses `_TRAJECTORY_RECORD_BUDGET` the
    remainder is dropped and `truncated` is set. `build_record` drops every
    `None`-valued field, so an absent prompt, empty system prompt, or
    no-trigger skill set leaves its key off rather than storing a null.
    """
    user_input = _redact_and_truncate(prompt, redact)
    system_prompt = _redact_and_truncate(trajectory.system_prompt, redact)
    output = _redact_and_truncate(trajectory.final_output, redact)

    used = len(user_input or "") + len(system_prompt or "") + len(output or "")
    steps: list[dict[str, Any]] = []
    truncated = False
    for step in trajectory.steps:
        step_dict = {
            "kind": step.kind,
            "name": step.name or None,
            "tool_id": step.tool_id or None,
            "content": _redact_and_truncate(step.content, redact),
        }
        # Charge the whole serialized step, not just its content: a run with
        # thousands of empty- / metadata-only steps would otherwise evade a
        # content-length-only budget and write an unbounded record.
        used += len(json.dumps(step_dict, default=str))
        if used > _TRAJECTORY_RECORD_BUDGET:
            truncated = True
            break
        steps.append(step_dict)

    skills = trajectory.skills
    return build_record(
        repo=repo,
        issue=int(issue),
        event="agent_trajectory",
        stage=stage,
        agent_role=agent_role,
        backend=backend,
        session_id=session_id,
        review_round=review_round,
        retry_count=retry_count,
        user_input=user_input,
        system_prompt=system_prompt,
        tools=list(trajectory.tools) or None,
        skills_triggered=list(skills.triggered) or None,
        skills_available=list(skills.available) or None,
        steps=steps,
        output=output,
        truncated=truncated or None,
    )


def _maybe_record_trajectory(
    *,
    repo: str,
    issue: int,
    stage: str,
    agent_role: str,
    backend: str,
    result: AgentResult,
    prompt: Optional[str],
    review_round: Optional[int],
    retry_count: Optional[int],
) -> None:
    """Parse, redact, truncate, and append one trajectory record -- gated on
    the opt-in `TRAJECTORY_LOG_PATH` and wrapped in its own fail-open guard.

    A no-op when the trajectory sink is disabled (the default), so the
    orchestrator-built prompt (`user_input`) -- and the parse/redact work
    itself -- happens ONLY when an operator turned the sink on. The whole
    block rides a dedicated try/except: a parser bug, an unredactable
    payload, or a sink IO failure logs and is swallowed so it can never drop
    the baseline `agent_exit` usage / cost record or the `skill_triggered`
    audit events, all of which were already produced before this runs.
    `_redact_secrets` is imported at call time to avoid a
    `github` -> `analytics` -> `workflow_messages` -> `github` import cycle.
    """
    if TRAJECTORY_LOG_PATH is None:
        return
    try:
        from ..workflow_messages import _redact_secrets
        trajectory = usage.parse_agent_trajectory(backend, result.stdout)
        append_trajectory_record(
            _build_trajectory_record(
                repo=repo,
                issue=int(issue),
                stage=stage,
                agent_role=agent_role,
                backend=backend,
                session_id=result.session_id,
                prompt=prompt,
                trajectory=trajectory,
                review_round=review_round,
                retry_count=retry_count,
                redact=_redact_secrets,
            )
        )
    except Exception:
        log.exception(
            "issue=#%d analytics: trajectory record(%s) failed; "
            "baseline agent_exit record is unaffected",
            issue, backend,
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
        removed = prune_old_records()
    except Exception:
        log.exception("analytics retention prune raised; continuing")
        return
    if removed:
        log.info("analytics retention prune removed %d record(s)", removed)


def prune_old_records(*, now: Optional[datetime] = None) -> int:
    """Remove records whose `ts` is older than `ANALYTICS_RETENTION_DAYS`.

    Reads the module-level `ANALYTICS_LOG_PATH` /
    `ANALYTICS_RETENTION_DAYS` parsed from the env at import.

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
    return _prune_jsonl_records(
        ANALYTICS_LOG_PATH, ANALYTICS_RETENTION_DAYS, _FILE_LOCK, now,
    )


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

    Every filesystem touch -- the existence probes as well as the read
    and rewrite -- downgrades OSError to a logged no-op, so a
    misconfigured path (e.g. ENAMETOOLONG) never escapes to the
    per-tick caller.
    """
    if path is None:
        return 0
    if days <= 0:
        return 0
    # `Path.exists()` re-raises OSErrors that do not mean "absent" --
    # e.g. ENAMETOOLONG on a misconfigured path -- so the probe itself
    # must be guarded, otherwise it escapes the per-tick caller. Treat
    # any such failure as a logged no-op, same as a read/rewrite OSError.
    try:
        if not path.exists():
            return 0
    except OSError as e:
        log.warning("could not probe %s for prune: %s", path, e)
        return 0

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    with lock:
        # Re-check existence under the lock: a concurrent operator
        # `rm` between the pre-lock probe above and acquiring the
        # lock would otherwise let `path.open` raise an unhandled
        # FileNotFoundError. The pre-lock probe stays for the fast
        # zero-cost no-op path on a disabled sink. Guarded for the same
        # reason as the pre-lock probe.
        try:
            if not path.exists():
                return 0
        except OSError as e:
            log.warning("could not probe %s for prune: %s", path, e)
            return 0
        kept: list[str] = []
        removed = 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    if not raw_line.strip():
                        continue
                    line = (
                        raw_line if raw_line.endswith("\n") else raw_line + "\n"
                    )
                    try:
                        rec = json.loads(raw_line)
                    except json.JSONDecodeError:
                        kept.append(line)
                        continue
                    ts_raw = rec.get("ts") if isinstance(rec, dict) else None
                    if not isinstance(ts_raw, str):
                        kept.append(line)
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw)
                    except ValueError:
                        kept.append(line)
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        removed += 1
                        continue
                    kept.append(line)
        except OSError as e:
            log.warning("could not read file %s for prune: %s", path, e)
            return 0

        if removed == 0:
            return 0

        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=path.name + ".prune.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    fh.writelines(kept)
                os.replace(tmp_path, str(path))
            except OSError:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            log.warning("could not rewrite file %s after prune: %s", path, e)
            return 0

        return removed


def append_trajectory_record(record: dict) -> None:
    """Append one JSONL line to `TRAJECTORY_LOG_PATH` if configured.

    No-op when the trajectory sink is disabled (the opt-in default).
    Shares `append_record`'s discipline -- reopen append per record,
    `mkdir -p` parents, OSError downgraded to a warning -- but writes to
    the trajectory file under `_TRAJECTORY_FILE_LOCK`, so it never opens,
    serializes against, or otherwise interacts with `ANALYTICS_LOG_PATH`,
    the analytics Postgres sync, or the dashboard rollups.
    """
    _append_jsonl_record(TRAJECTORY_LOG_PATH, _TRAJECTORY_FILE_LOCK, record)


def prune_trajectory_records(*, now: Optional[datetime] = None) -> int:
    """Remove trajectory records older than `TRAJECTORY_RETENTION_DAYS`.

    Reads the module-level `TRAJECTORY_LOG_PATH` /
    `TRAJECTORY_RETENTION_DAYS`. Mirrors `prune_old_records` exactly
    (no-op when the sink is disabled, retention is non-positive, or the
    file is absent; malformed / unparseable lines preserved; atomic
    temp-file + `os.replace` rewrite) but operates solely on the
    trajectory file under `_TRAJECTORY_FILE_LOCK` -- it never touches
    `ANALYTICS_LOG_PATH`, the analytics Postgres sync, or the dashboard
    rollups. `now` is parameter-overridable so tests can pin the
    comparison point.
    """
    return _prune_jsonl_records(
        TRAJECTORY_LOG_PATH, TRAJECTORY_RETENTION_DAYS,
        _TRAJECTORY_FILE_LOCK, now,
    )
