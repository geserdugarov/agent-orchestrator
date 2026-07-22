# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard cached-read connection scoping and forwarding tests."""

import inspect


import unittest


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)

_FORWARD_SCOPED_CONNECTION_CO = 16


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


CONFIGURED_DB_URL = "postgresql://h/db"


CONFIGURED_DB_ENV = MappingProxyType({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})


STATIC_METADATA_READER_NAMES = (
    "_read_data_extent",
    "_read_filter_options",
)


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


SCOPED_READ_CALL_FRAGMENT = "_scoped_read("


ENTRYPOINT_ATTR = "main"


SCOPED_READ_MEMBER = "_scoped_read"


FIRST_WAVE_READERS_MEMBER = "_first_wave_readers"


def _signature_slice(source: str, marker: str) -> str:
    """Return the `def name(...)` header slice starting at `marker`."""
    head = source.index(marker)
    tail = source.index("):", head)
    return source[head:tail]


class _MainSourceTest(unittest.TestCase):
    """Base for source checks over the lazy entrypoint and page helpers.

    Streamlit / Plotly are opt-in (not installed for the default
    `uv sync --locked`), so these read the rendered function source
    rather than driving the page under Streamlit. The entrypoint loads
    optional modules lazily and the page pipeline delegates controls,
    read waves, empty states, and widget sections to named helpers, so
    `_source_of` fetches the boundary each assertion protects.
    """

    def _main_source(self) -> str:
        return self._source_of(ENTRYPOINT_ATTR)

    def _source_of(self, name: str) -> str:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        return inspect.getsource(getattr(dashboard, name))


class _CachedReadConnectionSupport(_MainSourceTest):
    """The read path reuses a thread-local analytics connection across
    the dashboard's reads instead of opening a socket per call (issue
    #376). The Streamlit cache keys must therefore stay
    connection-free -- a raw `psycopg.Connection` is not a hashable
    cache key and every reload would otherwise look like a cache miss.

    Windowed wrappers route through `_read_filtered`; static metadata and
    the per-issue drill-down call `_scoped_read` directly. That primitive
    owns the `analytics_connection()` checkout and `conn=conn` forwarding,
    so the suite inspects each link without importing the optional
    Streamlit and Plotly dependency group.
    """

    def _readers_source(self) -> str:
        return self._combined_source(WIDGET_READER_WRAPPER_NAMES)

    def _read_filtered_source(self) -> str:
        return self._source_of("_read_filtered")

    def _metadata_source(self) -> str:
        return self._source_of("_read_static_metadata")

    def _drilldown_source(self) -> str:
        return self._source_of("_render_drilldown_view")

    def _combined_source(self, names) -> str:
        return "\n\n".join(self._source_of(name) for name in names)


class CachedReadConnectionScopeTest(_CachedReadConnectionSupport):
    def test_reads_scope_through_analytics_connection(self) -> None:
        # `_scoped_read` is the one place the per-thread persistent socket
        # is checked out via `analytics_connection`; the cached widget
        # wrappers, the static metadata reads, and the per-issue drill-down
        # all route their reads through it so the socket is reused across
        # widgets.
        self.assertIn(
            "analytics_read.analytics_connection()",
            self._source_of(SCOPED_READ_MEMBER),
        )
        self.assertIn("_read_filtered(", self._readers_source())
        for label, src in (
            ("filtered widget read", self._read_filtered_source()),
            ("static metadata", self._combined_source(STATIC_METADATA_READER_NAMES)),
            ("drill-down", self._drilldown_source()),
        ):
            with self.subTest(source=label):
                self.assertIn(SCOPED_READ_CALL_FRAGMENT, src)

    def test_cached_wrappers_do_not_accept_conn_arg(self) -> None:
        # Each `_read_*` wrapper accepts the hashable filter tuple as its
        # cache key. `conn` must NOT appear there -- it would force
        # st.cache_data to hash a connection object, which crashes
        # on the unhashable psycopg.Connection and (with a stringy
        # fallback) treats every refreshed conn as a cache miss.
        for name in WIDGET_READER_WRAPPER_NAMES:
            with self.subTest(name=name):
                # `conn` belongs inside `_scoped_read`, not in the cached
                # wrapper's parameter list.
                src = self._source_of(name)
                marker = f"def {name}("
                self.assertIn(marker, src)
                signature = _signature_slice(src, marker)
                self.assertNotIn(
                    " conn",
                    signature,
                    f"{name} must not accept a `conn` argument (it would become part of the cache key)",
                )


class CachedReadForwardingTest(_CachedReadConnectionSupport):
    def test_wrappers_forward_scoped_connection(self) -> None:
        # Cached readers delegate through `_read_filtered` to the scoped
        # connection adapter. The wrappers stay connection-free so `conn`
        # never lands in the cache key.
        readers_src = self._readers_source()
        self.assertGreaterEqual(
            readers_src.count("_read_filtered("),
            _FORWARD_SCOPED_CONNECTION_CO,
            "every widget read should route through `_read_filtered`",
        )
        # The two static-metadata reads route through it too.
        self.assertGreaterEqual(
            self._combined_source(STATIC_METADATA_READER_NAMES).count(SCOPED_READ_CALL_FRAGMENT),
            2,
        )
        # The shared filtered-read adapter and per-issue drill-down each
        # run their own scoped read.
        self.assertIn(SCOPED_READ_CALL_FRAGMENT, self._read_filtered_source())
        self.assertIn(SCOPED_READ_CALL_FRAGMENT, self._drilldown_source())
        # `_scoped_read` is the single place the scoped connection is
        # opened and forwarded to the read helper.
        scoped_src = self._source_of(SCOPED_READ_MEMBER)
        self.assertIn("analytics_read.analytics_connection()", scoped_src)
        self.assertIn("conn=conn", scoped_src)

    def test_prev_summary_uses_lightweight_kpi_path(self) -> None:
        # Layer 3 split the previous-window read off `get_summary`
        # so the dashboard only pays for the scalars it actually
        # reads off `prev_summary` (cost / token totals + agent-run
        # count for KPI delta pills and the cost-trend banner). The
        # `_read_prev_kpi` wrapper must therefore call
        # `analytics_read.get_kpi_prev` rather than reusing the
        # full `get_summary` shape -- if it falls back to the heavy
        # path, the cold-load wins from Layer 3 evaporate.
        wrapper_src = self._source_of("_read_prev_kpi")
        self.assertIn("analytics_read.get_kpi_prev", wrapper_src)
        self.assertNotIn("analytics_read.get_summary", wrapper_src)
        # And the `prev_summary` entry in the reader fan-out must
        # dispatch through `_read_prev_kpi` so the lightweight path
        # is the one that actually fires when the dashboard renders.
        src = self._source_of(FIRST_WAVE_READERS_MEMBER)
        self.assertIn(
            '_widget_task(st, "prev_summary", _read_prev_kpi, prev_key)',
            src,
        )


class AnalyticsConnectionExposureTest(unittest.TestCase):
    """`analytics_connection` and `close_thread_local_connection` are
    the new public surface from `analytics_read`. The dashboard
    imports the module wholesale (`from orchestrator.analytics import
    read as analytics_read`), so the symbols must be reachable as
    attributes for both `with analytics_read.analytics_connection()`
    and any shutdown hook that wants to drain the thread-local.
    """

    def test_connection_is_a_context_manager(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertTrue(hasattr(dashboard.analytics_read, "analytics_connection"))
        self.assertTrue(hasattr(dashboard.analytics_read, "close_thread_local_connection"))
        # Quick smoke: the unset-URL branch yields None without
        # touching any connect factory.
        with dashboard.analytics_read.analytics_connection() as conn:
            self.assertIsNone(conn)
