# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Inline SVG rendering for dashboard sparklines."""
from __future__ import annotations

from dataclasses import dataclass
from inspect import Parameter, Signature
from typing import Any, Sequence

from orchestrator import _dashboard_sparkline_data as sparkline_data


DEFAULT_SPARKLINE_WIDTH = 96
DEFAULT_SPARKLINE_HEIGHT = 26


@dataclass(frozen=True)
class _SparklineRequest:
    samples: Sequence[float]
    color: str
    width: int
    height: int


def _sparkline_paths(
    points: Sequence[tuple[float, float]],
    *,
    height: int,
) -> sparkline_data._SparklinePaths:
    padding = 2
    polyline = " ".join(map(_sparkline_point_text, points))
    area = _sparkline_area_path(points, height=height, padding=padding)
    return sparkline_data._SparklinePaths(polyline=polyline, area=area)


def _sparkline_point_text(point: tuple[float, float]) -> str:
    return f"{point[0]:.1f},{point[1]:.1f}"


def _sparkline_area_path(
    points: Sequence[tuple[float, float]],
    *,
    height: int,
    padding: int,
) -> str:
    baseline = height - padding
    first_x = points[0][0]
    last_x = points[-1][0]
    segments = " L".join(map(_sparkline_point_text, points))
    return (
        f"M{first_x:.1f},{baseline:.1f}"
        f" L{segments}"
        f" L{last_x:.1f},{baseline:.1f} Z"
    )


def _render_sparkline(request: _SparklineRequest) -> str:
    points = sparkline_data._sparkline_points(
        request.samples,
        width=request.width,
        height=request.height,
    )
    if not points:
        return (
            f'<svg width="{request.width}" height="{request.height}" '
            f'viewBox="0 0 {request.width} {request.height}"></svg>'
        )
    paths = _sparkline_paths(points, height=request.height)
    return (
        f'<svg width="{request.width}" height="{request.height}" '
        f'viewBox="0 0 {request.width} {request.height}" style="display:block">'
        f'<path d="{paths.area}" fill="{request.color}" fill-opacity="0.18" />'
        f'<polyline points="{paths.polyline}" fill="none" stroke="{request.color}" '
        'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />'
        "</svg>"
    )


def _sparkline_svg(*args: Any, **kwargs: Any) -> str:
    """Render an inline SVG through the historical keyword surface."""
    bound = _SPARKLINE_SIGNATURE.bind(*args, **kwargs)
    bound.apply_defaults()
    request = _SparklineRequest(
        samples=bound.arguments["values"],
        color=bound.arguments["color"],
        width=bound.arguments["w"],
        height=bound.arguments["h"],
    )
    return _render_sparkline(request)


_SPARKLINE_SIGNATURE = Signature(
    (
        Parameter("values", Parameter.POSITIONAL_OR_KEYWORD),
        Parameter("color", Parameter.KEYWORD_ONLY),
        Parameter("w", Parameter.KEYWORD_ONLY, default=DEFAULT_SPARKLINE_WIDTH),
        Parameter("h", Parameter.KEYWORD_ONLY, default=DEFAULT_SPARKLINE_HEIGHT),
    ),
)
_sparkline_svg.__signature__ = _SPARKLINE_SIGNATURE
