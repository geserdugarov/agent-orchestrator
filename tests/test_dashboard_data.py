# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard read-key, data-preparation, Plotly, and cache-key tests."""

import unittest


from datetime import date


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
    load_dashboard_theme as _theme_module,
)
from orchestrator.analytics.read import (
    ReviewRoundBucketRow,
    ThroughputDayRow,
    TimeSeriesPoint,
)

_MAY_DAY = 22
_MAY_DAY_SECONDARY = 28
_TOKENS_DAILY_SPARKS_TOTAL_CO = 12.0
_TOKENS_DAILY_SPARKS_TOTAL_OU = 20
_TOKENS_DAILY_SPARKS_SECONDARY = 6.0
_TOKENS_DAILY_SPARKS_COST_USD = 1.5
_TOKENS_DAILY_SPARKS_C_TERTIARY = 4.0
_TOKENS_DAILY_SPARKS_T_TERTIARY = 5.0
_TOKENS_DAILY_SPARKS_QUATERNARY = 3.0


SKIP_DOTENV_ENV = "ORCHESTRATOR_SKIP_DOTENV"


TOKEN_FILE_ENV = "ORCHESTRATOR_TOKEN_FILE"


MISSING_TOKEN_FILE = "/tmp/agent-orchestrator-token-missing"


ANALYTICS_READ_MODULE = "orchestrator.analytics.read"


DASHBOARD_MODULE = "orchestrator.dashboard"


DASHBOARD_CARDS_MODULE = "orchestrator.dashboard_cards"


DASHBOARD_KPI_STRIP_MODULE = "orchestrator.dashboard_kpi_strip"


DASHBOARD_READS_MODULE = "orchestrator.dashboard_reads"


DASHBOARD_WIDGETS_MODULE = "orchestrator.dashboard_widgets"


DASHBOARD_STATE_MODULE = "orchestrator.dashboard_state"


_RELOAD_POP_MODULES = (
    "orchestrator.config",
    ANALYTICS_READ_MODULE,
    "orchestrator.analytics",
    DASHBOARD_STATE_MODULE,
    "orchestrator.dashboard_kpis",
    "orchestrator.dashboard_html",
    DASHBOARD_CARDS_MODULE,
    DASHBOARD_KPI_STRIP_MODULE,
    "orchestrator.dashboard_skill_adoption",
    "orchestrator.dashboard_skill_matrix",
    DASHBOARD_READS_MODULE,
    DASHBOARD_WIDGETS_MODULE,
    DASHBOARD_MODULE,
)


_YEAR = 2026


MAY01 = date(_YEAR, 5, 1)


MAY07 = date(_YEAR, 5, 7)


MAY22 = date(_YEAR, 5, _MAY_DAY)


MAY28 = date(_YEAR, 5, _MAY_DAY_SECONDARY)


ISSUE_NUMBER = 42


EVENT_AGENT_EXIT = "agent_exit"


EVENT_STAGE_ENTER = "stage_enter"


STAGE_IMPLEMENTING = "implementing"


BACKEND_CLAUDE = "claude"


BACKEND_CODEX = "codex"


KPI_TOTAL_TOKENS = "Total tokens"


BUCKET_INITIAL = "0"


BUCKET_FIRST_ROUND = "1"


CACHE_REPO = "acme/widgets"


EVENT_NAMES = (EVENT_AGENT_EXIT, EVENT_STAGE_ENTER)


STAGE_NAMES = (STAGE_IMPLEMENTING,)


def _kpi_inputs(dashboard):
    return dashboard._KpiInputs(
        theme=_theme_module(),
        summary=dashboard.Summary(
            total_cost_usd=_TOKENS_DAILY_SPARKS_TOTAL_CO,
            total_input_tokens=10,
            total_output_tokens=_TOKENS_DAILY_SPARKS_TOTAL_OU,
            total_cache_read_tokens=3,
            total_cache_write_tokens=7,
        ),
        prev_summary=dashboard.Summary(
            total_cost_usd=_TOKENS_DAILY_SPARKS_SECONDARY,
            total_input_tokens=5,
            total_output_tokens=5,
            total_cache_read_tokens=5,
            total_cache_write_tokens=5,
        ),
        ts_points=[
            TimeSeriesPoint(
                day=MAY01,
                event=EVENT_AGENT_EXIT,
                count=1,
                cost_usd=_TOKENS_DAILY_SPARKS_COST_USD,
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=2,
                cache_write_tokens=3,
            ),
            TimeSeriesPoint(
                day=MAY01,
                event=EVENT_AGENT_EXIT,
                count=1,
                cost_usd=0.5,
                input_tokens=1,
                output_tokens=2,
            ),
            TimeSeriesPoint(
                day=MAY07,
                event=EVENT_AGENT_EXIT,
                count=1,
                cost_usd=_TOKENS_DAILY_SPARKS_C_TERTIARY,
                input_tokens=2,
                output_tokens=3,
                cache_read_tokens=1,
                cache_write_tokens=1,
            ),
        ],
        throughput_rows=[
            ThroughputDayRow(day=MAY01, resolved=2, rejected=1),
            ThroughputDayRow(day=MAY07, resolved=0, rejected=1),
        ],
        review_round_rows=[
            ReviewRoundBucketRow(
                bucket=BUCKET_INITIAL,
                runs=2,
                total_cost_usd=_TOKENS_DAILY_SPARKS_T_TERTIARY,
            ),
            ReviewRoundBucketRow(
                bucket=BUCKET_FIRST_ROUND,
                runs=1,
                total_cost_usd=_TOKENS_DAILY_SPARKS_QUATERNARY,
            ),
        ],
        days_in_window=2,
    )


class BuildReadKeysTest(unittest.TestCase):
    """`_build_read_keys` composes the current + previous-window cache
    keys the staged fan-out binds to cached reader tasks, so the previous
    key must carry the same filters over
    the immediately-preceding equal-length window.
    """

    def test_current_and_previous_keys(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(MAY22, MAY28)
        key, prev_key = dashboard._build_read_keys(
            window=window,
            repo_filter=CACHE_REPO,
            event_filter=list(EVENT_NAMES),
            stage_filter=None,
            issue_filter=ISSUE_NUMBER,
        )
        self.assertEqual(
            key,
            (window.start, window.end, CACHE_REPO, EVENT_NAMES, None, ISSUE_NUMBER),
        )
        prev = dashboard.previous_window(window)
        self.assertEqual(
            prev_key,
            (prev.start, prev.end, CACHE_REPO, EVENT_NAMES, None, ISSUE_NUMBER),
        )


class DashboardDataPrepTest(unittest.TestCase):
    """Small data-prep helpers keep `main()` focused on render sequencing."""

    def test_kpis_use_cache_tokens_and_daily_sparks(self) -> None:
        _, dashboard = _reload()
        kpis, resolved, rejected = dashboard._build_kpi_strip_data(
            _kpi_inputs(dashboard)
        )

        by_label = {kpi["label"]: kpi for kpi in kpis}
        self.assertEqual(resolved, 2)
        self.assertEqual(rejected, 2)
        self.assertEqual(by_label[KPI_TOTAL_TOKENS]["value"], "40")
        self.assertEqual(by_label[KPI_TOTAL_TOKENS]["delta"], 1.0)
        self.assertEqual(by_label[KPI_TOTAL_TOKENS]["spark"], [23.0, 7.0])
        self.assertEqual(by_label["Total spend"]["spark"], [2.0, 4.0])
        self.assertEqual(by_label["Cost / resolved issue"]["value"], "$6.00")
        self.assertEqual(
            by_label["Cost / resolved issue"]["spark"],
            [2, 0],
        )
        self.assertEqual(by_label["Rework share"]["value"], "38%")

    def test_backend_day_tokens_sum_duplicate_cells(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import BackendDailyTokensRow

        rows = [
            BackendDailyTokensRow(day=MAY01, backend=BACKEND_CLAUDE, total_tokens=10),
            BackendDailyTokensRow(day=MAY01, backend=BACKEND_CLAUDE, total_tokens=5),
            BackendDailyTokensRow(day=MAY01, backend=BACKEND_CODEX, total_tokens=3),
            BackendDailyTokensRow(day=MAY07, backend=BACKEND_CLAUDE, total_tokens=8),
        ]

        self.assertEqual(
            dashboard._backend_tokens_by_day(rows),
            {
                MAY01: {BACKEND_CLAUDE: 15.0, BACKEND_CODEX: 3.0},
                MAY07: {BACKEND_CLAUDE: 8.0},
            },
        )


class PlotlyConfigTest(unittest.TestCase):
    """`PLOTLY_CONFIG` is passed to every `st.plotly_chart` so the
    hover modebar (camera / zoom / pan) stays off every card --
    the standalone mock has no chart chrome.
    """

    def test_plotly_config_disables_modebar(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.PLOTLY_CONFIG.get("displayModeBar"), False)


class CacheKeyTest(unittest.TestCase):
    """`st.cache_data` hashes the cache key tuple; lists from
    multiselects need to become tuples, and `None` must be preserved
    so the tri-state filter contract (None / [] / [...]) does not
    collapse at the cache layer.
    """

    def test_lists_become_tuples(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(MAY01, MAY07)
        key = dashboard.cache_key(
            window,
            CACHE_REPO,
            list(EVENT_NAMES),
            list(STAGE_NAMES),
            ISSUE_NUMBER,
        )
        self.assertEqual(
            key,
            (
                window.start,
                window.end,
                CACHE_REPO,
                EVENT_NAMES,
                STAGE_NAMES,
                ISSUE_NUMBER,
            ),
        )
        hash(key)  # must be hashable

    def test_none_is_preserved(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(MAY01, MAY07)
        key = dashboard.cache_key(window, None, None, None, None)
        self.assertEqual(key, (window.start, window.end, None, None, None, None))

    def test_empty_list_distinct_from_none(self) -> None:
        # Empty events / stages mean "cleared multiselect, show
        # nothing"; the cache key must keep the empty tuple distinct
        # from None so the two SQL shapes do not collide in cache.
        _, dashboard = _reload()
        window = dashboard.to_window(MAY01, MAY07)
        empty = dashboard.cache_key(window, CACHE_REPO, [], [], None)
        none = dashboard.cache_key(window, CACHE_REPO, None, None, None)
        self.assertNotEqual(empty, none)
        self.assertEqual(empty[3], ())
        self.assertEqual(empty[4], ())
