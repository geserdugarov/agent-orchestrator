# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Configuration package reload behavior tests."""

import importlib
import os
import unittest
from types import MappingProxyType
from unittest.mock import patch

_CONFIG_MODULE = "orchestrator.config"
_HERMETIC = MappingProxyType(
    {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
    }
)
_POLL_INTERVAL_ENV = "POLL_INTERVAL"
_OVERRIDE_POLL_INTERVAL = 137
_INVALID_AGENT_ENV = "DEV_AGENT"
_INVALID_AGENT = "gemini"


class ConfigReloadTest(unittest.TestCase):
    """`orchestrator.config` assembles every setting as it is imported, so
    `importlib.reload(config)` re-parses the current environment and re-runs
    import-time validation rather than returning the values captured at first
    import. Callers and the reload helpers depend on this; the flat module
    gave it for free and the package must keep it.
    """

    def setUp(self) -> None:
        self._config = importlib.import_module(_CONFIG_MODULE)

    def tearDown(self) -> None:
        # Reload in place under the ambient environment so the shared package
        # object keeps its identity and the values the rest of the suite reads.
        importlib.reload(self._config)

    def test_reload_reparses_changed_environment(self) -> None:
        reloaded = self._reload_with(
            {_POLL_INTERVAL_ENV: str(_OVERRIDE_POLL_INTERVAL)},
        )
        self.assertEqual(reloaded.POLL_INTERVAL, _OVERRIDE_POLL_INTERVAL)

    def test_reload_reruns_import_time_validation(self) -> None:
        # An invalid agent spec aborts at import; the abort has to fire again
        # on reload, not be skipped because a cached submodule held the old
        # (valid) value.
        with self.assertRaises(SystemExit):
            self._reload_with({_INVALID_AGENT_ENV: _INVALID_AGENT})

    def _reload_with(self, extra_environment: dict[str, str]):
        with patch.dict(os.environ, {**_HERMETIC, **extra_environment}, clear=True):
            return importlib.reload(self._config)
