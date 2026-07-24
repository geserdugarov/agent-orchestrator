# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Module launch form: `python -m orchestrator` mirrors the console script."""
from __future__ import annotations

import sys

from orchestrator.cli import main

if __name__ == "__main__":
    sys.exit(main())
