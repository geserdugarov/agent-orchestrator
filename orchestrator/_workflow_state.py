# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.workflow` leaves."""
from __future__ import annotations

from orchestrator.state_machine import WorkflowLabel
from types import MappingProxyType
from typing import Mapping
from typing import Optional
import logging

log = logging.getLogger('orchestrator.workflow')

_FAMILY_AWARE_LABELS = frozenset((
    WorkflowLabel.DECOMPOSING, WorkflowLabel.BLOCKED, WorkflowLabel.UMBRELLA,
))

_CAP_EXEMPT_FAMILY_LABELS = frozenset((
    "blocked", "umbrella",
))

_PROCESSING_FAILED_LOG = "repo=%s issue=#%s processing failed"

_STATE_ATTR = "state"

_ISSUE_STATE_OPEN = "open"

_ISSUE_STATE_CLOSED = "closed"

_FAMILY_BUCKET_ISSUE: int = 0

_ISSUE_HANDLER_NAMES: Mapping[Optional[str], str] = MappingProxyType({
    None: "_handle_pickup",
    "decomposing": "_handle_decomposing",
    "ready": "_handle_ready",
    "blocked": "_handle_blocked",
    "umbrella": "_handle_umbrella",
    "implementing": "_handle_implementing",
    "documenting": "_handle_documenting",
    "validating": "_handle_validating",
    "in_review": "_handle_in_review",
    "fixing": "_handle_fixing",
    "resolving_conflict": "_handle_resolving_conflict",
    "question": "_handle_question",
})
