# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Stable sync-row imports grouped by parsing and mapping responsibility."""

from __future__ import annotations

from orchestrator.analytics._sync_row_mapping import (
    _PreparedRecord as _PreparedRecord,
    _RowProvenance as _RowProvenance,
    _build_insert_sql as _build_insert_sql,
    _prepare_record as _prepare_record,
    _row_values as _row_values,
    _split_row as _split_row,
)
from orchestrator.analytics._sync_row_parse import (
    _canonical_json as _canonical_json,
    _content_hash as _content_hash,
    _extra_columns as _extra_columns,
    _issue_number as _issue_number,
    _parse_ts as _parse_ts,
    _required_columns as _required_columns,
    _required_text as _required_text,
)
from orchestrator.analytics._sync_row_schema import (
    _COL_EVENT as _COL_EVENT,
    _COL_ISSUE as _COL_ISSUE,
    _COL_REPO as _COL_REPO,
    _COL_TS as _COL_TS,
    _JSONB_COLUMNS as _JSONB_COLUMNS,
    _PROMOTED_COLUMNS as _PROMOTED_COLUMNS,
    _REQUIRED_KEYS as _REQUIRED_KEYS,
)


_COMPATIBILITY_EXPORTS = (
    _PreparedRecord,
    _RowProvenance,
    _build_insert_sql,
    _prepare_record,
    _row_values,
    _split_row,
    _canonical_json,
    _content_hash,
    _extra_columns,
    _issue_number,
    _parse_ts,
    _required_columns,
    _required_text,
    _COL_EVENT,
    _COL_ISSUE,
    _COL_REPO,
    _COL_TS,
    _JSONB_COLUMNS,
    _PROMOTED_COLUMNS,
    _REQUIRED_KEYS,
)
