# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.stages.implementing` leaves."""
from __future__ import annotations

from typing import Tuple

_SILENT_PARKS_BEFORE_FRESH_SESSION = 2

_CLAUDE_STALE_SESSION_STDERR_MARKERS: Tuple[str, ...] = (
    "no conversation found with session id",
    "no conversation found with id",
    "no conversation with session id",
    "conversation not found",
)

_CLAUDE_CONTEXT_OVERFLOW_MARKERS: Tuple[str, ...] = (
    "prompt is too long",
    "input is too long",
    "input length and `max_tokens` exceed context limit",
)

_CLAUDE_SESSION_LIMIT_MESSAGE_MARKERS: Tuple[str, ...] = (
    "you've hit your session limit",
    "you've hit your usage limit",
    "you've reached your session limit",
    "you've reached your usage limit",
    "claude usage limit reached",
    "claude ai usage limit reached",
)

_DEV_AGENT = "dev_agent"

_DEV_SESSION_ID = "dev_session_id"

_CODEX_SESSION_ID = "codex_session_id"

_SILENT_PARK_COUNT = "silent_park_count"

_DEV_RESUME_COUNT = "dev_resume_count"

_RETRY_WINDOW_START = "retry_window_start"

_RETRY_COUNT = "retry_count"

_AWAITING_HUMAN = "awaiting_human"

_LAST_ACTION_COMMENT_ID = "last_action_comment_id"

_AGENT_TIMEOUT = "agent_timeout"

_PARK_REASON = "park_reason"

_PRE_IMPLEMENT_SHA = "pre_implement_sha"

_BRANCH = "branch"

_IMPLEMENTING_STAGE = "implementing"

_REASON_STUCK = "stuck"

_PR_BODY_AGENT_MESSAGE_CAP = 60000

_PR_BODY_TRUNCATION_MARKER = "_…(message truncated)_"
