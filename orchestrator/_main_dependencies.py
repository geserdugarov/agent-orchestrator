# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Runtime collaborators retained on the polling entry-point façade."""
from __future__ import annotations

import os as _os
import signal as _signal
import sys
from types import ModuleType

from orchestrator import agents as _agents
from orchestrator import analytics as _analytics
from orchestrator import config as _config
from orchestrator import workflow as _workflow
from orchestrator import github as _github
from orchestrator import scheduler as _scheduler

os = _os
signal = _signal
agents = _agents
analytics = _analytics
config = _config
workflow = _workflow
GitHubClient = _github.GitHubClient
IssueScheduler = _scheduler.IssueScheduler


def current_config() -> ModuleType:
    """Return the configuration instance loaded for the current main import."""
    return sys.modules["orchestrator.config"]
