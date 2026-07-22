# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Environment parsing for analytics sink settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_DISABLED_SENTINELS = ("off", "disabled", "none")


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
        "1",
        "true",
        "on",
        "yes",
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
