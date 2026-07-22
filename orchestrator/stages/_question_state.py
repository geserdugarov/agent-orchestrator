# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared immutable values for :mod:`orchestrator.stages.question` leaves."""
from __future__ import annotations

_QUESTION_STAGE = "question"

_QUESTION_AGENT_KEY = "question_agent"

_QUESTION_SESSION_KEY = "question_session_id"

_QUESTION_ANSWER = "question_answer"

_QUESTION_COMMITS = "question_commits"

_QUESTION_DIRTY = "question_dirty"

_QUESTION_SILENT = "question_silent"

_QUESTION_TIMEOUT = "question_timeout"

_UNSAFE_QUESTION_PARKS = frozenset((
    _QUESTION_TIMEOUT, _QUESTION_COMMITS, _QUESTION_DIRTY,
))
