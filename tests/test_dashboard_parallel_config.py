# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard parallel-read configuration tests."""

import unittest


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


PARALLEL_READS_ENV = "DASHBOARD_PARALLEL_READS"


class DashboardParallelReadsEnabledTest(unittest.TestCase):
    """`DASHBOARD_PARALLEL_READS` is the A/B knob for the parallel
    read fan-out. Default off so the sequential behavior holds until
    an operator opts in; truthy spellings follow the same vocabulary
    as the rest of the codebase (`DECOMPOSE=on` etc.).
    """

    def test_unset_defaults_to_false(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertFalse(dashboard.dashboard_parallel_reads_enabled())

    def test_empty_string_is_false(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: "", PARALLEL_READS_ENV: ""})
        self.assertFalse(dashboard.dashboard_parallel_reads_enabled())

    def test_truthy_spellings_enable_parallel(self) -> None:
        # The four documented truthy sentinels all enable the fan-out
        # regardless of case so an operator can paste whichever spelling
        # their team's playbook uses.
        for sentinel in ("1", "true", "on", "yes", "ON", "Yes", "TRUE"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload(
                    {
                        ANALYTICS_DB_URL_ENV: "",
                        PARALLEL_READS_ENV: sentinel,
                    }
                )
                self.assertTrue(dashboard.dashboard_parallel_reads_enabled())

    def test_falsy_spellings_keep_sequential(self) -> None:
        for sentinel in ("0", "false", "off", "no", "disabled", "none"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload(
                    {
                        ANALYTICS_DB_URL_ENV: "",
                        PARALLEL_READS_ENV: sentinel,
                    }
                )
                self.assertFalse(dashboard.dashboard_parallel_reads_enabled())

    def test_whitespace_is_stripped(self) -> None:
        # Operators paste env values from playbooks; tolerate leading /
        # trailing whitespace so a stray newline does not silently fall
        # back to the sequential path.
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: "", PARALLEL_READS_ENV: "  on  "})
        self.assertTrue(dashboard.dashboard_parallel_reads_enabled())
