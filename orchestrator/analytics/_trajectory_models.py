# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory headline and byte-budget models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class _TrajectoryHeadline:
    """Always-retained trajectory fields charged before variable arrays."""

    user_input: Optional[str]
    system_prompt: Optional[str]
    output: Optional[str]
    run_usage: dict[str, Any]

    @property
    def serialized_size(self) -> int:
        text_fields = (self.user_input, self.system_prompt, self.output)
        text_size = sum(len(text_field or "") for text_field in text_fields)
        return text_size + len(json.dumps(self.run_usage, default=str))


@dataclass
class _TrajectoryBudget:
    """Track serialized variable-field bytes retained in one record."""

    used: int
    limit: int
    truncated: bool = False

    def include(self, field_value: Any) -> bool:
        self.used += len(json.dumps(field_value, default=str))
        if self.used <= self.limit:
            return True
        self.truncated = True
        return False
