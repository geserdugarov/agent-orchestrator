# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard skill-matrix, adoption, and metadata wiring tests."""

import inspect


import unittest


from types import MappingProxyType


from tests.dashboard_reload_helpers import (
    reload_dashboard as _reload,
)

_TTL_FIVE_MINUTES_STATIC_META = 300


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


ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"


CONFIGURED_DB_URL = "postgresql://h/db"


CONFIGURED_DB_ENV = MappingProxyType({ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL})


STATIC_METADATA_READER_NAMES = (
    "_read_data_extent",
    "_read_filter_options",
)


SCOPED_READ_CALL_FRAGMENT = "_scoped_read("


ENTRYPOINT_ATTR = "main"


SCOPED_READ_MEMBER = "_scoped_read"


SECOND_WAVE_READERS_MEMBER = "_second_wave_readers"


SKILL_MATRIX_READER_MEMBER = "_read_skill_trigger_matrix"


SKILL_ADOPTION_READER_MEMBER = "_read_skill_adoption"


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


class SkillMatrixWiringTest(_MainSourceTest):
    """The invocation-level per-skill trigger matrix rides the same cached
    / fan-out read pattern as every other widget (its wrapper lives in
    `_widget_readers`) and renders as the second table inside the
    invocation-level diagnostics expander, beneath the session-adoption
    matrix. Streamlit is not installed for the default sync, so these
    inspect the rendered sources rather than driving the page under
    Streamlit.
    """

    def test_matrix_read_calls_matrix_read_model(self) -> None:
        src = self._source_of(SKILL_MATRIX_READER_MEMBER)
        self.assertIn("analytics_read.get_skill_trigger_matrix", src)

    def test_matrix_read_forwards_scoped_connection(self) -> None:
        # Reuse the cached-read pattern: the matrix wrapper delegates via
        # `_read_filtered` to `_scoped_read`, which checks out the thread-local
        # connection and forwards it to the read helper, so the matrix
        # read shares the open socket rather than opening its own.
        src = self._source_of(SKILL_MATRIX_READER_MEMBER)
        self.assertIn("_read_filtered(", src)
        self.assertIn("analytics_read.get_skill_trigger_matrix", src)
        filtered_src = self._source_of("_read_filtered")
        self.assertIn(SCOPED_READ_CALL_FRAGMENT, filtered_src)
        scoped_src = self._source_of(SCOPED_READ_MEMBER)
        self.assertIn("analytics_read.analytics_connection()", scoped_src)
        self.assertIn("conn=conn", scoped_src)

    def test_read_cache_key_omits_connection(self) -> None:
        # `conn` must not appear in the wrapper's parameter list -- it
        # would land in the `st.cache_data` key and crash on the
        # unhashable psycopg connection.
        src = self._source_of(SKILL_MATRIX_READER_MEMBER)
        marker = "def _read_skill_trigger_matrix("
        head = src.index(marker)
        tail = src.index("):", head)
        self.assertNotIn(" conn", src[head:tail])

    def test_matrix_dispatched_in_second_wave(self) -> None:
        src = self._source_of(SECOND_WAVE_READERS_MEMBER)
        self.assertIn(
            '_widget_task(st, "skill_matrix_rows", _read_skill_trigger_matrix, key)',
            src,
        )

    def test_matrix_is_second_diagnostic_table(self) -> None:
        # Inside the diagnostics expander the matrix is the SECOND table:
        # it renders after the aggregate `_skill_triggers_html(skill_rows)`
        # trigger-rate table.
        src = self._source_of("_render_skill_invocation_diagnostics")
        agg = src.index("_skill_triggers_html(skill_rows)")
        matrix = src.index("_skill_matrix_html(")
        self.assertLess(agg, matrix)

    def test_diagnostics_in_collapsed_expander(self) -> None:
        # The invocation-level views fold into a collapsed expander
        # (mirroring the "Recent agent runs" block) so they do not dominate
        # the card beneath the primary adoption matrix. Both the aggregate
        # table and the matrix render after an `st.expander(...,
        # expanded=False)` clearly named an invocation-level diagnostic.
        src = self._source_of("_render_skill_invocation_diagnostics")
        expander = src.index('with st.expander(\n        "Invocation-level')
        aggregate = src.index("_skill_triggers_html(")
        matrix = src.index("_skill_matrix_html(")
        self.assertLess(expander, aggregate)
        self.assertLess(expander, matrix)
        # The expander block carrying the diagnostics opens collapsed.
        block = src[expander:matrix]
        self.assertIn("expanded=False", block)


class SkillAdoptionWiringTest(_MainSourceTest):
    """The primary per-session skill-adoption matrix rides the same cached
    / fan-out read pattern as every other widget (its wrapper lives in
    `_widget_readers`) and renders as the headline table of the skill
    panel, above the invocation-level diagnostics. Streamlit is not
    installed for the default sync, so these inspect the rendered sources
    rather than driving the page under Streamlit.
    """

    def test_adoption_read_calls_adoption_read_model(self) -> None:
        src = self._source_of(SKILL_ADOPTION_READER_MEMBER)
        self.assertIn("analytics_read.get_skill_adoption", src)

    def test_adoption_read_forwards_scoped_connection(self) -> None:
        # Reuse the cached-read pattern: the adoption wrapper delegates via
        # `_read_filtered` to `_scoped_read`, which checks out the
        # thread-local connection and forwards it to the read helper, so the
        # adoption read shares the open socket rather than opening its own.
        src = self._source_of(SKILL_ADOPTION_READER_MEMBER)
        self.assertIn("_read_filtered(", src)
        self.assertIn("analytics_read.get_skill_adoption", src)

    def test_adoption_read_cache_key_omits_connection(self) -> None:
        # `conn` must not appear in the wrapper's parameter list -- it
        # would land in the `st.cache_data` key and crash on the
        # unhashable psycopg connection.
        src = self._source_of(SKILL_ADOPTION_READER_MEMBER)
        marker = "def _read_skill_adoption("
        head = src.index(marker)
        tail = src.index("):", head)
        self.assertNotIn(" conn", src[head:tail])

    def test_adoption_dispatched_in_second_wave(self) -> None:
        src = self._source_of(SECOND_WAVE_READERS_MEMBER)
        self.assertIn(
            '_widget_task(st, "skill_adoption_rows", _read_skill_adoption, key)',
            src,
        )

    def test_adoption_is_primary_render(self) -> None:
        # The session-adoption matrix is the headline table: it renders
        # before the invocation-level diagnostics expander, inside the same
        # card.
        src = self._source_of("_render_skill_adoption")
        adoption = src.index("_skill_adoption_html(")
        diagnostics = src.index("_render_skill_invocation_diagnostics(")
        self.assertLess(adoption, diagnostics)

    def test_adoption_needs_aggregate_rows(self) -> None:
        # The adoption render sits after the empty early return, so a window
        # with no `agent_exit` rows shows the single notice rather than an
        # empty-state per table.
        src = self._source_of("_render_skill_adoption")
        branch = src.index("if not skill_rows:")
        else_branch = src.index(
            'st.info("No `agent_exit` rows match the current filters.")',
            branch,
        )
        adoption = src.index("_skill_adoption_html(")
        self.assertLess(branch, adoption)
        self.assertLess(else_branch, adoption)


class StaticMetadataCacheTest(_MainSourceTest):
    """`get_data_extent` and `get_filter_options` (issue #379) carry
    no filter inputs and only change as `analytics.sync` ingests new
    events, so the `_read_static_metadata` helper wraps them in
    `@st.cache_data` under the longer `STATIC_METADATA_TTL_SECONDS`
    (5 min) instead of the per-filter 60 s TTL. Together these collapse
    the topbar / sidebar round-trip on every Streamlit rerun.
    """

    def test_ttl_is_five_minutes(self) -> None:
        # Pin the constant so a future tweak changes it deliberately.
        # A 5-minute TTL is long enough to absorb the typical rerun
        # cadence (Streamlit rerenders on every widget interaction)
        # but short enough that a freshly-synced repo / event value
        # surfaces within one `analytics.sync` cycle.
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertEqual(dashboard.STATIC_METADATA_TTL_SECONDS, _TTL_FIVE_MINUTES_STATIC_META)

    def test_extent_reader_decorated_with_longer_ttl(self) -> None:
        src = self._metadata_source()
        marker = "read_data_extent = st.cache_data("
        self.assertIn(marker, src)
        head = src.index(marker)
        tail = src.index(")(_read_data_extent)", head)
        decorator_window = src[head:tail]
        self.assertIn("st.cache_data(", decorator_window)
        self.assertIn("ttl=STATIC_METADATA_TTL_SECONDS", decorator_window)
        self.assertIn("show_spinner=False", decorator_window)

    def test_filter_options_use_longer_ttl(self) -> None:
        src = self._metadata_source()
        marker = "read_filter_options = st.cache_data("
        self.assertIn(marker, src)
        head = src.index(marker)
        tail = src.index(")(_read_filter_options)", head)
        decorator_window = src[head:tail]
        self.assertIn("st.cache_data(", decorator_window)
        self.assertIn("ttl=STATIC_METADATA_TTL_SECONDS", decorator_window)
        self.assertIn("show_spinner=False", decorator_window)

    def test_extent_and_options_readers_take_no_args(self) -> None:
        # The static-metadata readers carry no filter inputs, so the
        # cache key is empty -- they tolerate the longer TTL because
        # the values only change as `analytics.sync` ingests new
        # events, not when the operator adjusts the filter bar. Pin
        # the empty signature so a future refactor cannot silently
        # re-introduce a parameter (e.g. a connection) that would
        # turn into part of the cache key.
        for name in STATIC_METADATA_READER_NAMES:
            with self.subTest(name=name):
                src = self._source_of(name)
                marker = f"def {name}("
                head = src.index(marker)
                tail = src.index("):", head)
                self.assertEqual(src[head:tail + 1], f"{marker})")

    def test_main_dispatches_through_metadata_helper(self) -> None:
        # `main` reads the extent / options through the
        # `_read_static_metadata` helper, not the raw reads -- the bare
        # `get_data_extent` / `get_filter_options` calls live only
        # inside the helper's cached wrappers (routed through the
        # thread-local connection).
        run_src = self._source_of("_run_dashboard")
        self.assertIn("_read_static_metadata(", run_src)
        self.assertNotIn("get_data_extent(", run_src)
        self.assertNotIn("get_filter_options(", run_src)
        # The helper itself dispatches through the cached wrappers.
        meta_src = self._metadata_source()
        self.assertIn("read_data_extent()", meta_src)
        self.assertIn("read_filter_options()", meta_src)

    def _metadata_source(self) -> str:
        return self._source_of("_read_static_metadata")
