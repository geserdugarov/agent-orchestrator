# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Typed dashboard drill-down request and legacy call adapter."""
from __future__ import annotations

from dataclasses import dataclass
from inspect import Parameter, Signature
from typing import Any, Optional, Sequence

from orchestrator.dashboard_widgets import (
    _DashboardFilters,
    _DashboardModules,
    _render_drilldown_view,
)


@dataclass(frozen=True)
class _DrilldownRequest:
    st: Any
    pd: Any
    window: Any
    repo_filter: Optional[str]
    issue_input_parsed: Optional[int]
    event_filter: Optional[Sequence[str]]
    stage_filter: Optional[Sequence[str]]


def _render_drilldown(*args: Any, **kwargs: Any) -> None:
    """Render a drill-down through the historical dashboard call shape."""
    bound = _DRILLDOWN_SIGNATURE.bind(*args, **kwargs)
    request = _DrilldownRequest(**bound.arguments)
    modules = _DashboardModules(
        st=request.st,
        pd=request.pd,
        charts=None,
        theme=None,
    )
    filters = _DashboardFilters(
        window=request.window,
        repo=request.repo_filter,
        issue_input=request.issue_input_parsed,
        events=request.event_filter,
        stages=request.stage_filter,
    )
    _render_drilldown_view(modules, filters)


_KEYWORD_ONLY = Parameter.KEYWORD_ONLY
_DRILLDOWN_SIGNATURE = Signature(
    parameters=(
        Parameter("st", _KEYWORD_ONLY),
        Parameter("pd", _KEYWORD_ONLY),
        Parameter("window", _KEYWORD_ONLY),
        Parameter("repo_filter", _KEYWORD_ONLY),
        Parameter("issue_input_parsed", _KEYWORD_ONLY),
        Parameter("event_filter", _KEYWORD_ONLY),
        Parameter("stage_filter", _KEYWORD_ONLY),
    ),
)
_render_drilldown.__signature__ = _DRILLDOWN_SIGNATURE
