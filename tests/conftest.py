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

import os

# `orchestrator.config` reads `DEV_AGENT` / `REVIEW_AGENT` /
# `DECOMPOSE_AGENT` from the ambient env at import time and exposes both
# the raw spec (`*_SPEC`) and the parsed name (`config.REVIEW_AGENT`).
# Tests patch `config.REVIEW_AGENT` to a bare agent name, but stage
# handlers actually pin `config.REVIEW_AGENT_SPEC`, so an operator's
# `REVIEW_AGENT="codex -m ..."` shell export leaks into pinned-state
# assertions and trips `uv run pytest`. Clearing these BEFORE the first
# `orchestrator.config` import below makes the suite hermetic regardless
# of the shell environment.
#
# `ALLOWED_ISSUE_AUTHORS` is the same class of leak: config reads it at
# import time, and the comment-trust filter now drops non-allowlisted
# authors from prompt conversation text and the drift hash. An operator
# who exported it (a real deployment does) would otherwise see the
# suite's default fake authors (`alice`, `human`, `geserdugarov`)
# filtered and dozens of stage tests fail. Clearing it pins the legacy
# empty-allowlist ("trust everyone") default the stage tests assume;
# tests that exercise the allowlist patch `config.ALLOWED_ISSUE_AUTHORS`
# to a populated value inline, which overrides the import-time value.
for _agent_var in (
    "DEV_AGENT", "REVIEW_AGENT", "DECOMPOSE_AGENT", "ALLOWED_ISSUE_AUTHORS",
):
    os.environ.pop(_agent_var, None)

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

from orchestrator import analytics  # noqa: E402


@pytest.fixture(autouse=True)
def _disable_analytics_sink():
    with patch.object(analytics, "ANALYTICS_LOG_PATH", None), \
            patch.object(analytics, "TRAJECTORY_LOG_PATH", None):
        yield
