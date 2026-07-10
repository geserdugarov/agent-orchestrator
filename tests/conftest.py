# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pytest fixtures shared by the whole test suite.

The only fixture here disables the analytics sinks for every test.
`workflow._run_agent_tracked` reads `analytics.ANALYTICS_LOG_PATH` at
call time and appends a record per tracked agent run; the analytics
module's default points at `<LOG_DIR>/analytics.jsonl` under the repo
root, so any test that drives a stage handler (directly or via the
workflow mixin) would otherwise scribble into the operator's real log
directory. The autouse fixture below patches the path to `None` (the
documented "off" knob) so the suite is hermetic by default.

The same handler also writes a redacted `agent_trajectory` record to
`analytics.TRAJECTORY_LOG_PATH` -- the opt-in trajectory sink. It
defaults off (unset env), but an operator who exported
`TRAJECTORY_LOG_PATH` before running `pytest` would have the resolved
path live at import, so every tracked-agent test would scribble
trajectories into their real file. The fixture pins it to `None` too,
for the same hermeticity reason.

Tests that need a sink (e.g. `AgentAnalyticsTest`, the trajectory
recording tests) override the patch inline -- nested `patch.object`
lets the inner temp path win for the duration of its context, then
unwinds back to `None`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests import bootstrap  # noqa: F401 -- normalize settings before orchestrator imports
from orchestrator import analytics


@pytest.fixture(autouse=True)
def _disable_analytics_sink():
    with patch.object(analytics, "ANALYTICS_LOG_PATH", None), \
            patch.object(analytics, "TRAJECTORY_LOG_PATH", None):
        yield
