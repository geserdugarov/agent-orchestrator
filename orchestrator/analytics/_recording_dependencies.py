# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dependency modules used by the analytics recording facade."""

from __future__ import annotations

import importlib

from orchestrator import agents as agents, usage as usage


_recording_agent_exit = importlib.import_module(
    "orchestrator.analytics._recording_agent_exit",
)
_recording_catalog = importlib.import_module("orchestrator.analytics._recording_catalog")
_recording_io = importlib.import_module("orchestrator.analytics._recording_io")
_recording_models = importlib.import_module("orchestrator.analytics._recording_models")
_recording_settings = importlib.import_module("orchestrator.analytics._recording_settings")
_recording_skills = importlib.import_module("orchestrator.analytics._recording_skills")
_recording_usage = importlib.import_module("orchestrator.analytics._recording_usage")


_DEPENDENCIES = (
    agents,
    usage,
    _recording_agent_exit,
    _recording_catalog,
    _recording_io,
    _recording_models,
    _recording_settings,
    _recording_skills,
    _recording_usage,
)
