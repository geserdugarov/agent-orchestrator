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
os.environ.pop("DEV_AGENT", None)
os.environ.pop("REVIEW_AGENT", None)
os.environ.pop("DECOMPOSE_AGENT", None)
os.environ.pop("ALLOWED_ISSUE_AUTHORS", None)
