# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Pytest fixtures shared by the whole test suite.

The only fixture here disables the analytics sink for every test.
`workflow._run_agent_tracked` reads `config.ANALYTICS_LOG_PATH` at call
time and appends a record per tracked agent run; the config module's
default points at `<LOG_DIR>/analytics.jsonl` under the repo root, so
any test that drives a stage handler (directly or via the workflow
mixin) would otherwise scribble into the operator's real log directory.
The autouse fixture below patches the path to `None` (the documented
"off" knob) so the suite is hermetic by default.

Tests that need the sink (e.g. `AgentAnalyticsTest`) override the
patch inline -- nested `patch.object` lets the inner temp path win for
the duration of its context, then unwinds back to `None`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator import config


@pytest.fixture(autouse=True)
def _disable_analytics_sink():
    with patch.object(config, "ANALYTICS_LOG_PATH", None):
        yield
