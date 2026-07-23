# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Focused setup helpers for configuration parsing tests."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from tests import config_reload_helpers as _reload
from tests import config_test_values as _config_cases


_AGENT_DEFAULTS = (
    (_config_cases._DEV_AGENT_ENV, _config_cases._CLAUDE),
    (_config_cases._REVIEW_AGENT_ENV, _config_cases._CODEX),
)


def only_repo_spec(specs):
    if len(specs) != 1:
        raise AssertionError(f"expected one repo spec, got {len(specs)}")
    return specs[0]


def exit_with_config_error(message: str) -> None:
    sys.exit(message)


class ConfigErrorRecorder:
    def __init__(self, errors: list[str]) -> None:
        self._errors = errors

    def __call__(self, message: str) -> None:
        self._errors.append(message)
        sys.exit(message)


def _apply_agent_specs(config: ModuleType) -> None:
    for role, default in _AGENT_DEFAULTS:
        backend, arguments = config._parse_agent_spec(
            role,
            os.environ.get(role, default),
        )
        setattr(config, role, backend)
        setattr(config, f"{role}_ARGS", arguments)


def load_config_from_dotenv(
    dotenv_body: str,
    *,
    extra_environment: dict[str, str] | None = None,
) -> ModuleType:
    """Load a detached config module against one temporary dotenv file."""
    environment = {
        _config_cases._SKIP_DOTENV_ENV: _config_cases._ENABLED_ENV,
        _config_cases._TOKEN_FILE_ENV: _config_cases._MISSING_TOKEN_PATH,
    }
    if extra_environment:
        environment.update(extra_environment)
    config = _reload.load_config(environment)
    with tempfile.TemporaryDirectory() as temp_root:
        Path(temp_root, ".env").write_text(dotenv_body)
        with patch.dict(os.environ, environment, clear=True):
            os.environ.pop(_config_cases._SKIP_DOTENV_ENV, None)
            for key in _config_cases._DOTENV_OWNED_KEYS:
                os.environ.pop(key, None)
            with patch.object(config, "REPO_ROOT", Path(temp_root)):
                config._load_dotenv()
            _apply_agent_specs(config)
    return config
