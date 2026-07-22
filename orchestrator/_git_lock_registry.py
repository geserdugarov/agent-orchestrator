# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Process-local lock ownership for repository git plumbing."""

from __future__ import annotations

import threading
from pathlib import Path


class TargetRootLockRegistry:
    """Own one stable re-entrant lock per resolved target root."""

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def for_root(self, target_root: Path) -> threading.RLock:
        """Return the process-lifetime lock assigned to ``target_root``."""
        key = str(target_root)
        with self.guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    def clear(self) -> None:
        """Clear the compatibility-visible registry during isolated tests."""
        self._locks.clear()
