# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Sparkline scaling, point projection, and path models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


_EPSILON = 1e-9


def _sparkline_y(
    sample: float,
    *,
    low: float,
    span: float,
    padding: int,
    height: int,
) -> float:
    normalized = (sample - low) / span
    drawable_height = height - padding * 2
    return padding + (1 - normalized) * drawable_height


@dataclass(frozen=True)
class _SparklineLayout:
    low: float
    span: float
    padding: int
    height: int
    step: float


@dataclass(frozen=True)
class _SparklinePaths:
    polyline: str
    area: str


def _sparkline_step(width: int, padding: int, sample_count: int) -> float:
    drawable_width = width - padding * 2
    intervals = max(sample_count - 1, 1)
    return drawable_width / intervals


def _sparkline_layout(
    series: Sequence[float],
    *,
    width: int,
    height: int,
) -> _SparklineLayout:
    low = min(series)
    padding = 2
    return _SparklineLayout(
        low=low,
        span=max(max(series) - low, _EPSILON),
        padding=padding,
        height=height,
        step=_sparkline_step(width, padding, len(series)),
    )


def _sparkline_point(
    index: int,
    sample: float,
    layout: _SparklineLayout,
) -> tuple[float, float]:
    return (
        layout.padding + index * layout.step,
        _sparkline_y(
            sample,
            low=layout.low,
            span=layout.span,
            padding=layout.padding,
            height=layout.height,
        ),
    )


def _sparkline_points(
    series: Sequence[float],
    *,
    width: int,
    height: int,
) -> list[tuple[float, float]]:
    numbers = [float(sample or 0) for sample in series]
    if not numbers or max(numbers) == min(numbers) == 0:
        return []
    layout = _sparkline_layout(numbers, width=width, height=height)
    return [
        _sparkline_point(index, sample, layout)
        for index, sample in enumerate(numbers)
    ]
