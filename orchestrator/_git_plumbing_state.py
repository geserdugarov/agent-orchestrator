# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.git_plumbing` leaves."""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping
import logging

from orchestrator._git_lock_registry import TargetRootLockRegistry

log = logging.getLogger('orchestrator.git_plumbing')

_GIT_NO_PROMPT_ENV: Mapping[str, str] = MappingProxyType({
    "GIT_TERMINAL_PROMPT": "0",
})

_GIT = "git"

_FETCH = "fetch"

_ASKPASS_MODE = 0o700

_GIT_CONFIG_FLAG = "-c"

_AUTHED_GIT_PREFIX = (
    _GIT,
    _GIT_CONFIG_FLAG, "core.hooksPath=/dev/null",
    _GIT_CONFIG_FLAG, "credential.helper=",
    _GIT_CONFIG_FLAG, "core.fsmonitor=",
)

_HARDENED_GIT_PREFIX = (
    *_AUTHED_GIT_PREFIX,
    _GIT_CONFIG_FLAG, "commit.gpgsign=false",
    _GIT_CONFIG_FLAG, "rebase.autoStash=false",
)

_TARGET_ROOT_LOCKS = TargetRootLockRegistry()

_TARGET_ROOT_LOCKS_LOCK = _TARGET_ROOT_LOCKS.guard

_UNSAFE_TRANSPORT_CONFIG_RE = (
    r"^(url\..*\.(insteadof|pushinsteadof)|http\..*)$"
)
