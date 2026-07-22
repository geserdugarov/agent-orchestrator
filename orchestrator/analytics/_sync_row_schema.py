# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Column inventory for analytics JSONL-to-database mapping."""

from __future__ import annotations

# Required JSONL/DB record field names, shared by the promoted-column list, the
# required-key guard, and the per-record extraction.
_COL_TS = "ts"
_COL_REPO = "repo"
_COL_ISSUE = "issue"
_COL_EVENT = "event"


# Columns the table promotes from the JSONL record; anything else lands
# in `extras` JSONB so a JSONL record from a newer orchestrator version
# never loses fields. Kept here (not in `orchestrator/analytics/`) because
# it is a database-shape concern, not a record-build concern.
_PROMOTED_COLUMNS = (
    _COL_TS,
    _COL_REPO,
    _COL_ISSUE,
    _COL_EVENT,
    "stage",
    "duration_s",
    "result",
    "agent_role",
    "backend",
    "agent_spec",
    "resume_session_id",
    "session_id",
    "review_round",
    "retry_count",
    "exit_code",
    "timed_out",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "models",
    "turns",
    "cost_usd",
    "cost_source",
)

# JSONB columns; psycopg adapts dict / list to JSON natively but a few
# drivers need an explicit Json wrapper -- callers can pass their own
# `json_adapter` to the sync if needed.
_JSONB_COLUMNS = ("models", "extras")

_REQUIRED_KEYS = (_COL_TS, _COL_REPO, _COL_ISSUE, _COL_EVENT)
