# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.stages.validating` leaves."""
from __future__ import annotations

from types import MappingProxyType
from typing import Optional
from typing import Tuple
import re

_ReviewRoundsCommand = Tuple[int, Optional[str]]

_ADD_REVIEW_ROUNDS_RE = re.compile(
    r"^\s*/orchestrator\s+add-review-rounds\s+(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_PARK_REASON = "park_reason"

_PRE_DEV_FIX_SHA = "pre_dev_fix_sha"

_REVIEW_ROUND = "review_round"

_REASON_PUSH_FAILED = "push_failed"

_REASON_AGENT_TIMEOUT = "agent_timeout"

_REASON_REVIEWER_TIMEOUT = "reviewer_timeout"

_REASON_REVIEWER_FAILED = "reviewer_failed"

_REASON_REVIEW_CAP = "review_cap"

_OUTCOME_PARKED = "parked"

_OUTCOME_PUSHED = "pushed"

_OUTCOME_STUCK = "stuck"

_OUTCOME_RETURN = "return"

_SHORT_SHA_LEN = 12

_VALIDATING_TRANSIENT_PARK_REASONS = frozenset(
    (_REASON_PUSH_FAILED, _REASON_AGENT_TIMEOUT, _REASON_REVIEWER_TIMEOUT, _REASON_REVIEWER_FAILED)
)

_VERIFY_STATUS_TO_REASON = MappingProxyType({
    "failed": "verify_failed",
    "timeout": "verify_timeout",
    "dirty": "verify_dirty",
    "head_changed": "verify_head_changed",
})
