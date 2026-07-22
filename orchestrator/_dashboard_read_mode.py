# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard database availability and parallel read fan-out."""
from __future__ import annotations

import importlib
import os
import sys
from typing import Any, Callable, Sequence

from orchestrator import _dashboard_state_constants as constants


NamedReader = tuple[str, Callable[[], Any]]


def parse_parallel_reads_flag() -> bool:
    raw_flag = os.environ.get(constants.PARALLEL_READS_ENV, "").strip().lower()
    return raw_flag in constants.TRUTHY


def db_unconfigured_message() -> str | None:
    analytics = importlib.import_module("orchestrator.analytics")
    if not analytics.ANALYTICS_DB_URL:
        return constants.UNCONFIGURED_DB_MESSAGE
    return None


def dashboard_parallel_reads_enabled() -> bool:
    state_module = sys.modules["orchestrator.dashboard_state"]
    return state_module.DASHBOARD_PARALLEL_READS


def fan_out_reads(
    readers: Sequence[NamedReader],
    *,
    parallel: bool,
    max_workers: int = constants.PARALLEL_READS_MAX_WORKERS,
) -> dict[str, Any]:
    if not parallel:
        return {name: reader() for name, reader in readers}
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [(name, pool.submit(reader)) for name, reader in readers]
        return {name: future.result() for name, future in futures}
