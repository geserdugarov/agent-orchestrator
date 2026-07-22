# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard read-orchestration extraction tests."""

import sys


import unittest


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)


DASHBOARD_READS_MODULE = "orchestrator.dashboard_reads"


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


CONFIGURED_DB_URL = "postgresql://h/db"


CONFIGURED_DB_ENV = MappingProxyType({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})


WIDGET_READER_WRAPPER_NAMES = (
    "_read_summary",
    "_read_prev_kpi",
    "_read_time_series",
    "_read_stage_breakdown",
    "_read_recent_agent_exits",
    "_read_top_cost_issues",
    "_read_review_round",
    "_read_backend_efficiency",
    "_read_repo_breakdown",
    "_read_cost_coverage",
    "_read_hourly_heatmap",
    "_read_throughput",
    "_read_backend_daily_tokens",
    "_read_skill_adoption",
    "_read_skill_trigger_rates",
    "_read_skill_trigger_matrix",
)


_MOVED_READ_MEMBERS = (
    "_filter_list",
    "_read_filter_kwargs",
    "_scoped_read",
    "_read_filtered",
    "_read_data_extent",
    "_read_filter_options",
    "_read_static_metadata",
    *WIDGET_READER_WRAPPER_NAMES,
    "_widget_task",
    "_first_wave_readers",
    "_second_wave_readers",
    "_widget_readers",
    "_build_read_keys",
    "_dispatch_reads",
    "_log_dashboard_load",
    "_run_read_waves",
    "_DashboardReadPlan",
)


_READS_FACADE_CONSTANTS = (
    "DEFAULT_RECENT_AGENT_EXITS",
    "STATIC_METADATA_TTL_SECONDS",
    "LOADING_INDICATOR_MESSAGE",
)


class ReadOrchestrationExtractionTest(unittest.TestCase):
    """The dashboard read orchestration -- filter-to-query adapters,
    cached reader wrappers, reader registries, the staged parallel
    dispatch + two-wave data load, the static-metadata load, and the
    load-timing log -- lives in `orchestrator.dashboard_reads`, and
    `orchestrator.dashboard` re-exports every member under the same
    name so the `dashboard.<name>` surface and its test patch
    points keep resolving to the same object.
    """

    def test_read_members_defined_in_reads_module(self) -> None:
        _reload(CONFIGURED_DB_ENV)
        reads = sys.modules[DASHBOARD_READS_MODULE]
        for name in _MOVED_READ_MEMBERS:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(reads, name).__module__,
                    DASHBOARD_READS_MODULE,
                )

    def test_facade_reexports_reads_objects(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        reads = sys.modules[DASHBOARD_READS_MODULE]
        for name in (*_MOVED_READ_MEMBERS, *_READS_FACADE_CONSTANTS):
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(dashboard, name),
                    f"dashboard dropped the historical {name!r} alias",
                )
                self.assertIs(getattr(dashboard, name), getattr(reads, name))
