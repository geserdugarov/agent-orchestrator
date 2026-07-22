# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard facade compatibility tests."""

import importlib


import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


DASHBOARD_STATE_MODULE = "orchestrator.dashboard_state"


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


class FacadeReExportCompatibilityTest(unittest.TestCase):
    """The helper split moved the pure logic into `dashboard_state` /
    `dashboard_kpis` / `dashboard_html` / `dashboard_skill_matrix` /
    `dashboard_reads` / `dashboard_widgets`, but `orchestrator.dashboard`
    must keep re-exporting every name that lived on it on `origin/main`
    -- including the module-private `_parse_parallel_reads_flag` /
    `_TRUTHY` the parallel-reads knob is parsed through -- so
    `from orchestrator.dashboard import <helper>` keeps resolving
    against the facade rather than raising `ImportError`. Helpers a
    module keeps to itself (e.g. `dashboard_skill_matrix`'s internal
    sort / header / row functions) were never on the facade and stay
    off it.
    """

    def test_parallel_read_internals_are_reexported(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        state = importlib.import_module(DASHBOARD_STATE_MODULE)
        # Each facade name is the very object the extracted module
        # defines -- a genuine re-export, not a shadow copy.
        self.assertIs(
            dashboard._parse_parallel_reads_flag,
            state._parse_parallel_reads_flag,
        )
        self.assertIs(dashboard._TRUTHY, state._TRUTHY)
        # And the re-exported helper still works through the alias.
        self.assertIsInstance(dashboard._parse_parallel_reads_flag(), bool)
