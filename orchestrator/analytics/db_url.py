# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Analytics DB URL resolution for the read path.

A single indirection so every read helper resolves a caller-supplied
`db_url=` (or falls back to `analytics.ANALYTICS_DB_URL` when the
caller passes `None`) through one function. Kept apart from the
connection helpers so the URL-source policy has one obvious home.
"""
from __future__ import annotations

from typing import Optional


def _resolve_db_url(db_url: Optional[str]) -> Optional[str]:
    # Import the parent package at call time rather than binding a
    # module-level reference: `ANALYTICS_DB_URL` is read off whatever
    # `orchestrator.analytics` is current in `sys.modules`, so a test
    # that pops + reloads the package to land a patched env sees the
    # fresh value without having to also pop this module.
    if db_url is None:
        from .. import analytics as _analytics

        return _analytics.ANALYTICS_DB_URL
    return db_url
