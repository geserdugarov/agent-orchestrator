# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Lazy compatibility hooks for :mod:`orchestrator.base_sync`."""

from __future__ import annotations

from orchestrator._compat_exports import build_exports
from orchestrator._base_sync_export_manifest import EXPORTED_NAMES, EXPORTS

resolve_export, exported_dir = build_exports("orchestrator.base_sync", EXPORTS, EXPORTED_NAMES)
