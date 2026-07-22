# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared JSONL vocabulary and types for usage parsing."""

from __future__ import annotations

import re
from typing import Optional


TYPE = "type"
MESSAGE = "message"
USAGE = "usage"
ID = "id"
MODEL = "model"
INPUT = "input"
OUTPUT = "output"
INPUT_TOKENS = "input_tokens"
OUTPUT_TOKENS = "output_tokens"
CACHED = "cached"
CACHED_TOKENS = "cached_tokens"
CACHE_READ = "cache_read"
CACHE_WRITE_FIVE_MIN = "cache_write_5m"
CACHE_WRITE_ONE_HOUR = "cache_write_1h"
TOTAL_TOKEN_USAGE = "total_token_usage"
PAYLOAD = "payload"
INFO_KEY = "info"
ITEM_KEY = "item"
RESULT_KEY = "result"
ASSISTANT = "assistant"
UNKNOWN = "unknown"
CLAUDE = "claude"
CODEX = "codex"
LONG_CONTEXT_THRESHOLD = "long_context_threshold"
LONG_CONTEXT_INPUT_MULT = "long_context_input_mult"
LONG_CONTEXT_OUTPUT_MULT = "long_context_output_mult"
TOKENS_PER_MILLION = 1_000_000

TokenBucket = dict[str, int]
ClaudeRateMap = dict[str, float]
ClaudeRateRow = tuple[re.Pattern[str], ClaudeRateMap]
CodexRateMap = dict[str, Optional[float]]
CodexRateRow = tuple[str, CodexRateMap]
CodexUsageEvent = tuple[str, TokenBucket]
ModelPath = tuple[str, ...]
