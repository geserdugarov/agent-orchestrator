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
  from `workflow._run_agent_tracked` with parsed usage / cost.

`ANALYTICS_LOG_PATH`, `ANALYTICS_RETENTION_DAYS`, and the libpq URL
for the analytics Postgres service (`ANALYTICS_DB_URL`) are parsed at
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .. import config, usage
from ..agents import AgentResult

__all__ = [
    "ANALYTICS_DB_URL",
    "ANALYTICS_LOG_PATH",
    "ANALYTICS_RETENTION_DAYS",
    "append_record",
    "build_record",
    "config",
    "prune_old_records",
    "prune_with_retention_logging",
    "record_agent_exit",
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


# Sink configuration. Parsed at import so a fresh process picks up the
# operator's env immediately; tests patch these module attributes
# directly (`patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`).
ANALYTICS_LOG_PATH: Optional[Path] = _parse_log_path()
ANALYTICS_RETENTION_DAYS: int = _parse_retention_days()
ANALYTICS_DB_URL: Optional[str] = _parse_db_url()


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


def append_record(record: dict) -> None:
    """Append one JSONL line to `ANALYTICS_LOG_PATH` if configured.

    No-op when the sink is disabled. OSError is logged and swallowed so
    a misconfigured path (read-only mount, disk full, permission
    failure) cannot stop the per-issue tick from making progress; the
    pinned state on GitHub remains correct regardless.
    """
    path = ANALYTICS_LOG_PATH
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as e:
        log.warning("could not write analytics record to %s: %s", path, e)


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
) -> None:
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
        return
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
        )
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
    """
    path = ANALYTICS_LOG_PATH
    if path is None:
        return 0
    days = ANALYTICS_RETENTION_DAYS
    if days <= 0:
        return 0
    if not path.exists():
        return 0

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    kept: list[str] = []
    removed = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                if not raw_line.strip():
                    continue
                line = raw_line if raw_line.endswith("\n") else raw_line + "\n"
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
        log.warning("could not read analytics file %s for prune: %s", path, e)
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
        log.warning(
            "could not rewrite analytics file %s after prune: %s", path, e
        )
        return 0

    return removed
