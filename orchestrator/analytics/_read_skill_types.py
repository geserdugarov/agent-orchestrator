# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared skill aggregation keys and row offsets."""

from __future__ import annotations

_SkillCohort = tuple[str, str, str]
_SkillMatrixKey = tuple[str, str, str, str]
_SkillAdoptionKey = tuple[str, str, str, str]

_SESSION_RESUME_INDEX = 3
_SESSION_ID_INDEX = 4
_SESSION_ROW_INDEX = 5
