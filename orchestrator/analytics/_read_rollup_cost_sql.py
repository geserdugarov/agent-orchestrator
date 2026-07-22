# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Reusable token-share SQL fragments for rollup cost queries."""

from __future__ import annotations

_ROLLUP_CACHE_TOKENS_SQL = (
    "(COALESCE(total_cached_tokens, 0) + COALESCE(total_cache_read_tokens, 0) + COALESCE(total_cache_write_tokens, 0))"
)
_ROLLUP_ALL_TOKENS_SQL = (
    "(COALESCE(total_input_tokens, 0) "
    "+ COALESCE(total_output_tokens, 0) "
    "+ COALESCE(total_cache_read_tokens, 0) "
    "+ COALESCE(total_cache_write_tokens, 0))"
)
_ROLLUP_CACHE_FRACTION_SQL = (
    f"CASE WHEN {_ROLLUP_ALL_TOKENS_SQL} = 0 THEN 0 "
    f"ELSE {_ROLLUP_CACHE_TOKENS_SQL}::numeric "
    f"/ {_ROLLUP_ALL_TOKENS_SQL}::numeric END"
)
