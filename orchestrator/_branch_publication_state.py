# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.branch_publication` leaves."""
from __future__ import annotations

import logging
import re

log = logging.getLogger('orchestrator.branch_publication')

_CONVENTIONAL_TYPES = (
    "feat", "fix", "chore", "docs", "refactor",
    "test", "perf", "build", "ci", "style", "revert",
)

_CONVENTIONAL_TYPES_ALT = "|".join(_CONVENTIONAL_TYPES)

_CONVENTIONAL_RE = re.compile(
    rf"^(?:{_CONVENTIONAL_TYPES_ALT})"
    r"(?:\([^)]+\))?!?:\s+\S",
)

_PREFIXED_RE = re.compile(r"^[a-z][a-z0-9-]*(?:\([^)]+\))?!?:\s+\S")

_PREFIX_TOKEN_RE = re.compile(r"^([a-z][a-z0-9-]*)(?:\([^)]+\))?!?:\s+\S")
