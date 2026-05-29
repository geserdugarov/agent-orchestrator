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

`append_record` is a no-op when `config.ANALYTICS_LOG_PATH` is None.
`prune_old_records` removes records older than
`config.ANALYTICS_RETENTION_DAYS`; it is a no-op when the sink is
disabled or retention is non-positive (keep forever). `main._run_tick`
calls `prune_old_records` once per polling tick after every configured
repo drains, so retention is applied without operator intervention.
The pinned GitHub state on each issue is the authoritative durable
state -- this sink is local-filesystem observability and may be
truncated or deleted at any time without affecting workflow
correctness.

The sink API lives in this `__init__.py` rather than a submodule so
the package's `config` binding matches the flat-module behavior it
replaced: `tests/test_analytics.py` pops both `orchestrator.config`
and `orchestrator.analytics` from `sys.modules` between cases and
re-imports them in lockstep, and callers elsewhere patch their
already-imported `orchestrator.config` reference. A submodule would
re-bind `config` only when its own module entry was popped, which
would diverge from both patterns. Future analytics surfaces that do
NOT need that lockstep can land in sibling submodules.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .. import config

__all__ = [
    "append_record",
    "build_record",
    "config",
    "prune_old_records",
]

log = logging.getLogger(__name__)


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
    """Append one JSONL line to `config.ANALYTICS_LOG_PATH` if configured.

    No-op when the sink is disabled. OSError is logged and swallowed so
    a misconfigured path (read-only mount, disk full, permission
    failure) cannot stop the per-issue tick from making progress; the
    pinned state on GitHub remains correct regardless.
    """
    path = config.ANALYTICS_LOG_PATH
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as e:
        log.warning("could not write analytics record to %s: %s", path, e)


def prune_old_records(*, now: Optional[datetime] = None) -> int:
    """Remove records whose `ts` is older than `ANALYTICS_RETENTION_DAYS`.

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
    path = config.ANALYTICS_LOG_PATH
    if path is None:
        return 0
    days = config.ANALYTICS_RETENTION_DAYS
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
