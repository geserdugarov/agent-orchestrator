# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Normalization and folding for GitHub status/check-run surfaces."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

_CHECK_STATE_FAILURE = "failure"
_CHECK_STATE_PENDING = "pending"
_FAILED_CHECK_RUN_CONCLUSIONS = frozenset(
    (_CHECK_STATE_FAILURE, "timed_out", "action_required", "cancelled"),
)
_SUCCESSFUL_CHECK_RUN_CONCLUSIONS = frozenset(
    ("success", "neutral", "skipped"),
)


@dataclass(frozen=True)
class CheckSurfaceRead:
    """Normalized state and read outcome for one checks surface."""

    state: Optional[str] = None
    read_failed: bool = False


def normalize_combined_status(combined_status: Any) -> Optional[str]:
    """Convert a legacy combined status into the shared state model."""
    status = combined_status.state
    if not status or (
        status == _CHECK_STATE_PENDING
        and not combined_status.total_count
    ):
        return None
    return _CHECK_STATE_FAILURE if status == "error" else status


def normalize_check_runs(check_runs: Iterable[Any]) -> Optional[str]:
    """Convert check-run conclusions into the shared state model."""
    conclusions = {check_run.conclusion for check_run in check_runs}
    if not conclusions:
        return None
    if None in conclusions:
        return _CHECK_STATE_PENDING
    if conclusions & _FAILED_CHECK_RUN_CONCLUSIONS:
        return _CHECK_STATE_FAILURE
    if conclusions <= _SUCCESSFUL_CHECK_RUN_CONCLUSIONS:
        return "success"
    return _CHECK_STATE_FAILURE


def fold_check_states(
    states: Iterable[Optional[str]],
    *,
    read_failed: bool,
) -> str:
    """Fold normalized surfaces using failure-before-pending priority."""
    observed_states = [state for state in states if state]
    if observed_states and read_failed:
        observed_states.append(_CHECK_STATE_PENDING)
    if not observed_states:
        return "none"
    if _CHECK_STATE_FAILURE in observed_states:
        return _CHECK_STATE_FAILURE
    if _CHECK_STATE_PENDING in observed_states:
        return _CHECK_STATE_PENDING
    return "success"
