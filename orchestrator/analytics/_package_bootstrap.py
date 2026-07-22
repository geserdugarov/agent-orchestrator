# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Per-import analytics initialization behind an import-only package."""
from __future__ import annotations

import sys

from orchestrator.analytics._package_exports import exported_dir
from orchestrator.analytics._package_initialization import initialize_package


PACKAGE_NAME = "orchestrator.analytics"


initialize_package(sys.modules[PACKAGE_NAME])
__dir__ = exported_dir
