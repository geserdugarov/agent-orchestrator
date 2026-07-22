# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Skill payload normalization and cohort ordering."""

from __future__ import annotations

import json
from typing import Any, Sequence

from orchestrator.analytics._read_skill_types import _SkillCohort


def _as_skill_names(raw: Any) -> list[str]:
    """Coerce a JSONB skill-name array column into a list of strings.

    psycopg adapts a `jsonb` array to a Python list, so the common path
    is a passthrough; a driver / fixture that hands back the raw JSON
    text is tolerated too. ``None`` (the absent-key result of
    ``extras -> 'skills_...'``), a non-list payload, or a non-string
    element collapses to an empty list / is skipped so a malformed
    `extras` blob never raises mid-read.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [name for name in raw if isinstance(name, str)]


def _label_or_unknown(raw: Any) -> str:
    if raw is None:
        return "unknown"
    return str(raw)


def _row_label(row: Sequence[Any], index: int) -> str:
    if len(row) <= index:
        return "unknown"
    return _label_or_unknown(row[index])


def _skill_matrix_order_key(
    key: tuple[str, str, str, str],
    *,
    counts: dict[tuple[str, str, str, str], int],
    cohort_runs: dict[tuple[str, str, str], int],
) -> list:
    """Lexicographic sort key: most-run cohorts first, then name order."""
    repo, role, backend, skill = key
    return [
        -counts.get(key, 0),
        -cohort_runs.get((repo, role, backend), 0),
        repo,
        role,
        backend,
        skill,
    ]


def _skill_cohort(row: Sequence[Any]) -> _SkillCohort:
    """Normalize one row's repository, role, and backend cohort."""
    return (
        _label_or_unknown(row[0]),
        _row_label(row, 1),
        _row_label(row, 2),
    )
