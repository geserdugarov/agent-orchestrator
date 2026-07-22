# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.base_sync` leaves."""
from __future__ import annotations

from orchestrator.state_machine import WorkflowLabel
import logging

log = logging.getLogger('orchestrator.base_sync')

_PR_REFRESH_DETOUR_LABELS = frozenset(
    (
        WorkflowLabel.VALIDATING, WorkflowLabel.DOCUMENTING,
        WorkflowLabel.IN_REVIEW, WorkflowLabel.FIXING,
    ),
)

_PARK_REASON = "park_reason"

_AWAITING_HUMAN = "awaiting_human"

_REVIEW_ROUND = "review_round"

_CONFLICT_ROUND = "conflict_round"

_PENDING_PUSH_SHA = "pending_auto_base_rebase_push_sha"

_REASON_AUTO_BASE_REBASE_FAILED = "auto_base_rebase_failed"

_REASON_AUTO_BASE_REBASE_PUSH_FAILED = "auto_base_rebase_push_failed"

_ERROR_SNIPPET_LEN = 120

_AUTO_REBASE_PARK_REASONS = frozenset(
    (
        _REASON_AUTO_BASE_REBASE_FAILED,
        "auto_base_rebase_dirty",
        _REASON_AUTO_BASE_REBASE_PUSH_FAILED,
    ),
)
