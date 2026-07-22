# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Defensive scalar and collection coercion for trajectory records."""

from __future__ import annotations

from typing import Any, Optional


def coerce_int(raw_value: Any) -> Optional[int]:
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, str):
        try:
            return int(raw_value.strip())
        except ValueError:
            return None
    return None


def coerce_float(raw_value: Any) -> Optional[float]:
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    if isinstance(raw_value, str):
        try:
            return float(raw_value.strip())
        except ValueError:
            return None
    return None


def coerce_str(raw_value: Any) -> str:
    if raw_value is None:
        return ""
    if isinstance(raw_value, str):
        return raw_value
    return str(raw_value)


def coerce_str_tuple(raw_value: Any) -> tuple[str, ...]:
    if not isinstance(raw_value, list):
        return ()
    return tuple(coerce_str(name) for name in raw_value if name is not None)


def as_list(raw_value: Any) -> list[Any]:
    return raw_value if isinstance(raw_value, list) else []
