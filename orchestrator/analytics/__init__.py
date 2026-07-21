# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Project-local analytics sink -- package facade.

The recording implementation (sink configuration, JSONL append
primitives, and the stage / repo-skill / agent-exit recorders) lives in
`orchestrator.analytics._recording`; the opt-in trajectory sink's
serialization, budgeting, redaction / truncation, and append helpers live
in the sibling `orchestrator.analytics._trajectories`; the by-age
retention pruning for both sinks (JSONL prune, scan models, atomic
rewrite, retention logging, and the public prune entry points) lives in
the sibling `orchestrator.analytics._retention`. This
`__init__` re-exports all three surfaces so callers keep importing from
`orchestrator.analytics` unchanged. The sibling read / sync submodules
(`read`, `read_*`, `sync`, `connection`, `query`, `predicates`,
`db_url`) are the Postgres-facing surfaces and are imported directly as
`analytics.<submodule>`.

Settings ownership. The six sink knobs -- `ANALYTICS_LOG_PATH`,
`ANALYTICS_RETENTION_DAYS`, `ANALYTICS_DB_URL`, `TRACK_SKILL_TRIGGERS`,
`TRAJECTORY_LOG_PATH`, `TRAJECTORY_RETENTION_DAYS` -- are parsed here at
import (via `_recording`'s `_parse_*` helpers) and bound as attributes
of *this* package, not of the `_recording` submodule. That placement is
load-bearing for the tests: `tests/test_analytics.py` pops both
`orchestrator.config` and `orchestrator.analytics` from `sys.modules`
between cases and re-imports them in lockstep to pick up a patched env,
and callers elsewhere patch the already-imported package reference
(`patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`, the autouse
conftest sink-disable, `patch.object(analytics, "_TRAJECTORY_RECORD_BUDGET",
...)`). Binding the knobs here re-parses them on every package (re)import;
the recorders in `_recording` (and `_trajectories` / `_retention`, which
import its `_live_settings`) read the values back off this package at call
time (see `_recording._live_settings`), so a patched or reloaded value
takes effect. Each package instance imports its own `_recording` /
`_trajectories` / `_retention` (the eviction below), bound to that
instance, so a reference held across a reload keeps reading the knobs its
own callers patched. `ANALYTICS_DB_URL` is resolved the same way by
`analytics.db_url._resolve_db_url`.

`ANALYTICS_LOG_PATH` defaults to `<config.LOG_DIR>/analytics.jsonl` and
disables on empty / `off` / `disabled` / `none`; `TRAJECTORY_LOG_PATH`
is opt-in and defaults *off*. Full analytics-sink semantics -- event
kinds, record shapes, locks, fail-open persistence -- are documented in
`_recording`; the trajectory sink's redaction / truncation, budgeting,
and append discipline are documented in `_trajectories`; retention
pruning for both sinks is documented in `_retention`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from orchestrator import config as config, usage as usage
from orchestrator.agents import AgentResult as AgentResult

# Evict any cached `_recording` / `_trajectories` / `_retention` so this package
# instance imports its own copies. `tests/test_analytics.py::_reload` pops +
# re-imports `orchestrator.analytics` to land a patched env; a shared submodule
# would bind its `_facade` to whichever package instance imported it last, so a
# stale holder's recorders would read the reloaded instance's sink knobs.
# Per-instance submodules keep each side of the reload reading the knobs its own
# callers patched. `_trajectories` and `_retention` read `_recording`'s
# `_live_settings`, so evict them too or a stale sibling would resolve the wrong
# facade.
sys.modules.pop(f"{__name__}._recording", None)
sys.modules.pop(f"{__name__}._trajectories", None)
sys.modules.pop(f"{__name__}._retention", None)

# Absolute imports (not `from ._recording`) grouped at <= 8 names per statement,
# following the `worktrees.py` re-export-hub convention so the facade adds no
# local-folder-import or too-many-names finding. The E402 suppressions are
# because these must follow the cache eviction above.
from orchestrator.analytics._recording import (  # noqa: E402
    _parse_db_url as _parse_db_url,
    _parse_log_path as _parse_log_path,
    _parse_retention_days as _parse_retention_days,
    _parse_track_skill_triggers as _parse_track_skill_triggers,
    _parse_trajectory_log_path as _parse_trajectory_log_path,
    _parse_trajectory_retention_days as _parse_trajectory_retention_days,
)
from orchestrator.analytics._recording import (  # noqa: E402
    _FILE_LOCK as _FILE_LOCK,
    append_record as append_record,
    log as log,
    os as os,
)
from orchestrator.analytics._recording import (  # noqa: E402
    build_record as build_record,
    record_agent_exit as record_agent_exit,
    record_repo_skill_catalog as record_repo_skill_catalog,
    record_stage_enter as record_stage_enter,
    record_stage_evaluation as record_stage_evaluation,
)

# The opt-in trajectory sink lives in `_trajectories` (imported after
# `_recording`, which it reads `_live_settings` off). Importing it here also
# binds `analytics._trajectories`, the attribute `_recording._persist_agent_exit`
# reaches to hand off each run's trajectory on the same package instance.
from orchestrator.analytics._trajectories import (  # noqa: E402
    _TRAJECTORY_FIELD_HEAD as _TRAJECTORY_FIELD_HEAD,
    _TRAJECTORY_FIELD_TAIL as _TRAJECTORY_FIELD_TAIL,
    _TRAJECTORY_FILE_LOCK as _TRAJECTORY_FILE_LOCK,
    _TRAJECTORY_RECORD_BUDGET as _TRAJECTORY_RECORD_BUDGET,
    append_trajectory_record as append_trajectory_record,
)

# By-age retention pruning for both sinks lives in `_retention` (imported after
# `_recording` / `_trajectories`, whose `_FILE_LOCK` / `_TRAJECTORY_FILE_LOCK`
# it shares and whose `_live_settings` it reads). The prune entry points stay
# re-exported here so `main._run_tick` and the cron viewer keep importing them
# from `orchestrator.analytics` unchanged.
from orchestrator.analytics._retention import (  # noqa: E402
    prune_old_records as prune_old_records,
    prune_trajectory_records as prune_trajectory_records,
    prune_with_retention_logging as prune_with_retention_logging,
)

# These attributes remain bound for established test and compatibility patch
# targets even though the package facade does not call them directly.
_COMPATIBILITY_EXPORTS = (
    AgentResult,
    usage,
    _FILE_LOCK,
    log,
    os,
    _TRAJECTORY_FIELD_HEAD,
    _TRAJECTORY_FIELD_TAIL,
    _TRAJECTORY_FILE_LOCK,
    _TRAJECTORY_RECORD_BUDGET,
)

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

# Sink configuration bound on the package facade. Parsed at import so a fresh
# process picks up the operator's env immediately; re-parsed on every package
# reload and patched here directly by tests
# (`patch.object(analytics, "ANALYTICS_LOG_PATH", ...)`). The recorders in
# `_recording` read these back off this package at call time.
ANALYTICS_LOG_PATH: Optional[Path] = _parse_log_path()
ANALYTICS_RETENTION_DAYS: int = _parse_retention_days()
ANALYTICS_DB_URL: Optional[str] = _parse_db_url()
TRACK_SKILL_TRIGGERS: bool = _parse_track_skill_triggers()
TRAJECTORY_LOG_PATH: Optional[Path] = _parse_trajectory_log_path()
TRAJECTORY_RETENTION_DAYS: int = _parse_trajectory_retention_days()
