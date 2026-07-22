# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dependency modules used by the trajectory recording facade."""

from __future__ import annotations

import importlib

from orchestrator import usage as usage


_recording = importlib.import_module("orchestrator.analytics._recording")
_trajectory_models = importlib.import_module("orchestrator.analytics._trajectory_models")
_trajectory_persistence = importlib.import_module("orchestrator.analytics._trajectory_persistence")
_trajectory_sanitize = importlib.import_module("orchestrator.analytics._trajectory_sanitize")
_trajectory_serialize = importlib.import_module("orchestrator.analytics._trajectory_serialize")


_DEPENDENCIES = (
    usage,
    _recording,
    _trajectory_models,
    _trajectory_persistence,
    _trajectory_sanitize,
    _trajectory_serialize,
)
