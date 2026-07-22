# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dependency modules used by the analytics sync facade."""

from __future__ import annotations

import importlib


_sync_cli = importlib.import_module("orchestrator.analytics._sync_cli")
_sync_database = importlib.import_module("orchestrator.analytics._sync_database")
_sync_ingest = importlib.import_module("orchestrator.analytics._sync_ingest")
_sync_models = importlib.import_module("orchestrator.analytics._sync_models")
_sync_redaction = importlib.import_module("orchestrator.analytics._sync_redaction")
_sync_rows = importlib.import_module("orchestrator.analytics._sync_rows")
_sync_run = importlib.import_module("orchestrator.analytics._sync_run")


_DEPENDENCIES = (
    _sync_cli,
    _sync_database,
    _sync_ingest,
    _sync_models,
    _sync_redaction,
    _sync_rows,
    _sync_run,
)
