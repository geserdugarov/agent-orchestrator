# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Canonical console entry point for the orchestrator polling loop.

The runtime still lives in `orchestrator.main`; this module resolves its
`main` attribute at call time so signal handlers, test patches, and the
process-wide façade keep pointing at the same collaborator.
"""
from __future__ import annotations

from typing import Optional

from orchestrator import main as _runtime


def main(argv: Optional[list[str]] = None) -> int:
    """Run the polling loop and return its process exit code."""
    return _runtime.main(argv)
