# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Scalar coercion helpers for raw analytics rows."""

from __future__ import annotations

from typing import Any, Optional, Sequence


def _int_or_none(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    return int(raw)


def _float_or_none(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    return float(raw)


def _row_int(row: Sequence[Any], index: int) -> int:
    if len(row) <= index:
        return 0
    return int(row[index] or 0)


def _bool_or_none(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    return bool(raw)


def _empty_filter_selected(selection: Optional[Sequence[str]]) -> bool:
    if selection is None:
        return False
    return len(selection) == 0
