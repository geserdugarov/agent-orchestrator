# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy compatibility hooks for :mod:`orchestrator.stages.implementing`."""

from __future__ import annotations

from orchestrator._compat_exports import build_exports
from orchestrator.stages._implementing_export_manifest import EXPORTED_NAMES, EXPORTS

resolve_export, exported_dir = build_exports("orchestrator.stages.implementing", EXPORTS, EXPORTED_NAMES)
