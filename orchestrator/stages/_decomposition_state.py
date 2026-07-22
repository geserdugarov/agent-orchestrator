# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.stages.decomposition` leaves."""
from __future__ import annotations

_AWAITING_HUMAN = "awaiting_human"

_LAST_ACTION_COMMENT_ID = "last_action_comment_id"

_CHILDREN = "children"

_UMBRELLA = "umbrella"

_PARK_REASON = "park_reason"

_PARENT_NUMBER = "parent_number"

_CREATED_AT = "created_at"

_DONE = "done"

_HeldChild = tuple[int, list[int]]
