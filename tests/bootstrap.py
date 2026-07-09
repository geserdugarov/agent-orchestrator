# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Import-time environment normalization for the test suite.

The settings here are read by `orchestrator.config` during import, so
they must be normalized before any test imports orchestrator modules.
"""
from __future__ import annotations

import os

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

# Agent specs and comment-trust authors are import-time config values. Tests
# patch the parsed config objects inline when they need non-default behavior.
for _name in (
    "DEV_AGENT",
    "REVIEW_AGENT",
    "DECOMPOSE_AGENT",
    "ALLOWED_ISSUE_AUTHORS",
):
    os.environ.pop(_name, None)
