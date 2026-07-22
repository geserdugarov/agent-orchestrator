# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.worktree_lifecycle` leaves."""
from __future__ import annotations

import logging
import re

log = logging.getLogger('orchestrator.worktree_lifecycle')

_SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")

_SAFE_CHAR = "_"

_SLUG_DIGEST_LEN = 16

_WORKTREE_ADD = ("worktree", "add")

_WORKTREE_REMOVE_FORCE = ("worktree", "remove", "--force")
