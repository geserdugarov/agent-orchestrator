# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the non-Streamlit logic in `orchestrator.dashboard`.

The Streamlit, pandas, Plotly, and chart-builder imports inside
`dashboard.main` are deliberately lazy so the orchestrator polling
tick never pulls them in. These tests exercise the pure helpers
(date window math, preset window selection, KPI deltas, insight
banners, the disabled-DB banner, the issue-number drill-down
parser, and the cache-key shape) and assert the lazy-import
invariant -- the module must load even when `streamlit` is not on
the install path. That way the suite stays hermetic regardless of
which dependency group an operator synced.

The module-reload pattern mirrors `tests/test_analytics_read.py`:
re-import under a hermetic env so the dashboard's `from orchestrator
import analytics` picks up the patched `ANALYTICS_DB_URL` instead of
whatever the earlier test-session import left cached.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch


# Hermetic reload env: skip .env autoloading and point the token file at a
# guaranteed-missing path so no ambient GITHUB_TOKEN leaks into a test.
SKIP_DOTENV_ENV = "ORCHESTRATOR_SKIP_DOTENV"
TOKEN_FILE_ENV = "ORCHESTRATOR_TOKEN_FILE"
MISSING_TOKEN_FILE = "/tmp/agent-orchestrator-token-missing"

# Package + submodule dotted names the reload cycle drops from
# `sys.modules` and the extraction tests look up. Named once so the
# repeated dotted strings do not drift between the reload pop-list and
# the per-module assertions.
ORCHESTRATOR_PKG = "orchestrator"
DASHBOARD_MODULE = "orchestrator.dashboard"
DASHBOARD_CARDS_MODULE = "orchestrator.dashboard_cards"
DASHBOARD_KPI_STRIP_MODULE = "orchestrator.dashboard_kpi_strip"
DASHBOARD_READS_MODULE = "orchestrator.dashboard_reads"
DASHBOARD_WIDGETS_MODULE = "orchestrator.dashboard_widgets"
DASHBOARD_STATE_MODULE = "orchestrator.dashboard_state"
DASHBOARD_CHARTS_MODULE = "orchestrator.dashboard_charts"

# Modules the hermetic reload evicts before re-importing so the fresh
# facade re-binds every extracted leaf against the patched env.
_RELOAD_POP_MODULES = (
    "orchestrator.config",
    "orchestrator.analytics.read",
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


def _hermetic_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        SKIP_DOTENV_ENV: "1",
        TOKEN_FILE_ENV: MISSING_TOKEN_FILE,
    }
    if extra:
        env.update(extra)
    return env


def _reload(env: dict[str, str] | None = None):
    """Reload `analytics` + `dashboard` against the hermetic env.

    Import order matters: `analytics` must come back first so its
    fresh module object is installed as the
    `orchestrator.analytics` package attribute before `dashboard`'s
    `from orchestrator import analytics` runs -- otherwise
    `_handle_fromlist` returns the conftest-cached module and
    `dashboard.analytics.ANALYTICS_DB_URL` keeps the pre-patch value.
    `config` is popped too so the analytics package's
    `from .. import config` reloads against the patched env (it
    still reads `LOG_DIR` for the JSONL default).

    The extracted helper modules (`dashboard_state` / `dashboard_kpis`
    / `dashboard_html` / `dashboard_cards` / `dashboard_kpi_strip` /
    `dashboard_skill_adoption` / `dashboard_skill_matrix` /
    `dashboard_reads` / `dashboard_widgets`)
    are popped alongside `dashboard` so the re-imported facade re-binds
    them too -- otherwise a cached `dashboard_state` would keep its
    pre-patch `from orchestrator import analytics` reference and its
    module-import parse of `DASHBOARD_PARALLEL_READS`, a cached
    `dashboard_reads` / `dashboard_widgets` / `dashboard_cards` /
    `dashboard_kpi_strip` would keep its pre-patch `from
    orchestrator.analytics import read` reference (and, for
    `dashboard_widgets`, its pre-patch `dashboard_reads` / `dashboard_cards`
    / `dashboard_kpi_strip` bindings), and a cached `dashboard_skill_matrix`
    would keep its `_table_css` / `_table_html` / `_UNKNOWN` bound to the
    discarded `dashboard_html` module, defeating the hermetic reload.
    """
    with patch.dict(os.environ, _hermetic_env(env), clear=True):
        for stale_module in _RELOAD_POP_MODULES:
            sys.modules.pop(stale_module, None)
        # `import_module` re-imports the popped module fresh (a plain
        # `from orchestrator import ...` would resolve the stale parent
        # attribute instead). `analytics` must load first so its fresh
        # object is the package attribute `dashboard`'s `from orchestrator
        # import analytics` resolves against.
        analytics = importlib.import_module("orchestrator.analytics")
        dashboard = importlib.import_module(DASHBOARD_MODULE)
        return analytics, dashboard


# The dashboard's only configuration input is the analytics DB URL env
# var; tests flip it between "unset" (the disabled-DB banner / hermetic
# reload) and a syntactically-valid Postgres URL (the source-inspection
# reloads that read `dashboard.main`).
ANALYTICS_DB_URL_ENV = "ANALYTICS_DB_URL"
PARALLEL_READS_ENV = "DASHBOARD_PARALLEL_READS"
CONFIGURED_DB_URL = "postgresql://h/db"
# The reload env that the source-inspection tests share: a configured
# Postgres URL so `dashboard.main` resolves its read wrappers.
CONFIGURED_DB_ENV = {ANALYTICS_DB_URL_ENV: CONFIGURED_DB_URL}

# Every date/datetime fixture builds off this single calendar year so the
# window math reads as day/month offsets rather than repeated literals.
_YEAR = 2026

# Recurring May-2026 anchors for the window / preset / KPI-delta tests.
# The canonical current window is MAY22..MAY28 (7 days); the preset
# data extent spans MAY01..MAY28 with its exclusive end at MAY29, and
# the 3-day preset opens at MAY26.
MAY01 = date(_YEAR, 5, 1)
MAY02 = date(_YEAR, 5, 2)
MAY03 = date(_YEAR, 5, 3)
MAY04 = date(_YEAR, 5, 4)
MAY05 = date(_YEAR, 5, 5)
MAY06 = date(_YEAR, 5, 6)
MAY07 = date(_YEAR, 5, 7)
MAY15 = date(_YEAR, 5, 15)
MAY22 = date(_YEAR, 5, 22)
MAY26 = date(_YEAR, 5, 26)
MAY28 = date(_YEAR, 5, 28)
MAY29 = date(_YEAR, 5, 29)
JAN01 = date(_YEAR, 1, 1)
JUN05_NOON_UTC = datetime(_YEAR, 6, 5, 12, 0, tzinfo=timezone.utc)
JUN05_NOON_NAIVE = datetime(_YEAR, 6, 5, 12, 0)


def _utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


# Incidental first/last-seen timestamps stamped by the issue-summary row
# builders. Never asserted -- the builders only need a valid ordered pair.
FIRST_SEEN = datetime(_YEAR, 5, 1, tzinfo=timezone.utc)
LAST_SEEN = datetime(_YEAR, 5, 2, tzinfo=timezone.utc)

# Sample repo slugs.
REPO_A = "acme/a"
REPO_B = "acme/b"
REPO_C = "acme/c"

# Fan-out reader names grouped by staged-render wave (issue #379): the
# first wave feeds the topbar / KPI strip, the second the remaining
# widgets.
FIRST_WAVE_READER_NAMES = (
    "summary", "prev_summary", "ts_points",
    "review_round_rows", "throughput_rows", "cost_coverage_rows",
)
SECOND_WAVE_READER_NAMES = (
    "stage_rows", "agent_exits", "issues_rows",
    "backend_rows", "repo_rows", "heatmap_rows",
    "backend_daily_rows", "skill_adoption_rows", "skill_rows",
    "skill_matrix_rows",
)
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

ISSUE_NUMBER = 42

# Summary-wide reliability totals used to prove the dashboard ignores
# recent-run row caps when computing headline tiles.
FULL_WINDOW_AGENT_RUNS = 250
FULL_WINDOW_FAILURES = 4
FULL_WINDOW_TIMEOUTS = 17

# Dashboard domain values are local to these pure-helper fixtures; keeping
# them here avoids coupling the dashboard suite to workflow-stage helpers.
EVENT_AGENT_EXIT = "agent_exit"
EVENT_STAGE_ENTER = "stage_enter"
STAGE_IMPLEMENTING = "implementing"
STAGE_VALIDATING = "validating"
ROLE_DEVELOPER = "developer"
ROLE_REVIEWER = "reviewer"
BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
COST_SOURCE_REPORTED = "reported"
COST_SOURCE_UNKNOWN_PRICE = "unknown-price"

KPI_AGENT_RUNS = "Agent runs"
KPI_FAILURES = "Failures"
KPI_TIMEOUTS = "Timeouts"
KPI_TOTAL_TOKENS = "Total tokens"
COLUMN_RUNS = "Runs"

# Review-round bucket labels (`ReviewRoundBucketRow.bucket`): "0" is the
# initial, pre-rework pass that rework cost excludes; "1" is the first
# review round.
BUCKET_INITIAL = "0"
BUCKET_FIRST_ROUND = "1"

SCOPED_READ_CALL_FRAGMENT = "_scoped_read("
REPO_C_CELL_FRAGMENT = ">c/repo<"
ROLE_WITH_MARKUP = "dev<&>"

# Dashboard member / attribute names the source-inspection tests fetch
# through `_source_of`, plus the load-source dispatch fragments they
# search for. Named once so the repeated identifiers stay in sync across
# the assertion bodies (the re-export registries below keep their own
# plain-string listing).
ENTRYPOINT_ATTR = "main"
SCOPED_READ_MEMBER = "_scoped_read"
FIRST_WAVE_READERS_MEMBER = "_first_wave_readers"
SECOND_WAVE_READERS_MEMBER = "_second_wave_readers"
RUN_READ_WAVES_MEMBER = "_run_read_waves"
RENDER_FIRST_WAVE_MEMBER = "_render_first_wave"
SKILL_MATRIX_READER_MEMBER = "_read_skill_trigger_matrix"
SKILL_ADOPTION_READER_MEMBER = "_read_skill_adoption"
DISPATCH_FIRST_WAVE = "reads.first_wave"
DISPATCH_SECOND_WAVE = "reads.second_wave"

# Cache-key fixture inputs: a sample repo plus the event / stage filter
# selections whose list->tuple normalization the cache key must preserve.
CACHE_REPO = "acme/widgets"
EVENT_NAMES = (EVENT_AGENT_EXIT, EVENT_STAGE_ENTER)
STAGE_NAMES = (STAGE_IMPLEMENTING,)

# Skill-matrix sort contract: the query-param names the clickable headers
# write, the two direction tokens, and the column keys in header order.
MTX_SORT_PARAM = "mtx_sort"
MTX_DIR_PARAM = "mtx_dir"
SORT_ASC = "asc"
SORT_DESC = "desc"
MTX_SORT_KEYS = (
    "repo", "role", "backend", "skill", "runs", "skill_runs", "rate",
)
# The numeric-column sort key the header-click and parse tests exercise
# most; named so the repeated query-param value stays a single token.
SORT_KEY_RUNS = "runs"

# Skill-adoption sort contract: the query-param names the clickable
# adoption headers write, and the column keys in header order.
ADOPT_SORT_PARAM = "adopt_sort"
ADOPT_DIR_PARAM = "adopt_dir"
ADOPT_SORT_KEYS = (
    "repo", "role", "backend", "skill",
    "sessions", "adopted", "rate", "loads", "incidental",
)
# The session-denominator sort key the adoption header-click / parse tests
# exercise; named so the repeated query-param value stays a single token.
SORT_KEY_SESSIONS = "sessions"


def _is_orchestrator_module(name: str) -> bool:
    return name == ORCHESTRATOR_PKG or name.startswith("orchestrator.")


def _is_orchestrator_launch_module(name: str) -> bool:
    return (
        _is_orchestrator_module(name)
        or name == "script_launch"
    )


def _is_dashboard_script_launch_module(name: str) -> bool:
    return name in (
        DASHBOARD_MODULE,
        "orchestrator.script_launch",
        "script_launch",
    )


def _clear_modules(predicate) -> None:
    for name in list(sys.modules):
        if predicate(name):
            sys.modules.pop(name, None)


def _restore_sys_path(path_entries: list[str]) -> None:
    sys.path[:] = path_entries


def _dashboard_launch_paths() -> SimpleNamespace:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    dashboard_path = repo_root / ORCHESTRATOR_PKG / "dashboard.py"
    return SimpleNamespace(
        repo_root=repo_root,
        dashboard_path=dashboard_path,
        script_dir=dashboard_path.parent,
    )


def _drop_repo_root_from_sys_path(repo_root) -> None:
    from pathlib import Path

    resolved_root = repo_root.resolve()
    sys.path[:] = [
        path_entry for path_entry in sys.path
        if not path_entry or Path(path_entry).resolve() != resolved_root
    ]


@contextmanager
def _script_launch_sandbox(predicate):
    """Isolate a script-launch import behind sys.path / module restoration.

    Snapshots `sys.path` and the predicate-matched `sys.modules` entries,
    then restores both (and re-clears the predicate's modules) on exit so a
    successful re-import inside the sandbox cannot poison the rest of the
    test session with a half-initialised package. Yields the `ExitStack`
    so a caller can register its own temp dirs on the same teardown scope.
    """
    original_path = list(sys.path)
    saved_modules = {
        name: module for name, module in sys.modules.items()
        if predicate(name)
    }
    with ExitStack() as cleanup:
        cleanup.callback(sys.modules.update, saved_modules)
        cleanup.callback(_clear_modules, predicate)
        cleanup.callback(_restore_sys_path, original_path)
        yield cleanup


def _record_reader_call(name: str, payload: int, calls: list[str]) -> int:
    calls.append(name)
    return payload


def _raise_read_error(
    message: str,
    calls: list[str] | None = None,
    call_name: str | None = None,
) -> None:
    from orchestrator.analytics.read import AnalyticsReadError

    if calls is None or call_name is None:
        raise AnalyticsReadError(message)
    calls.append(call_name)
    raise AnalyticsReadError(message)


def _increment_reader_count(name: str, counts: dict[str, int]) -> str:
    counts[name] = counts.get(name, 0) + 1
    return name


def _return_value(payload: int) -> int:
    return payload


def _record_threaded_reader(
    name: str,
    calls: dict[str, int],
    threads: set[int],
    lock,
) -> str:
    import threading

    with lock:
        calls[name] = calls.get(name, 0) + 1
        threads.add(threading.get_ident())
    return name


def _sleep_then_return(delay: float, payload: str) -> str:
    import time

    time.sleep(delay)
    return payload


def _tile_value_tones(tiles) -> dict:
    """Project `reliability_tile_data` triples to `{label: (value, tone)}`."""
    return {label: (tile_value, tone) for tile_value, label, tone in tiles}


def _tile_values(tiles) -> dict:
    """Project `reliability_tile_data` triples to `{label: value}`."""
    return {label: tile_value for tile_value, label, _ in tiles}


def _tile_tones(tiles) -> dict:
    """Project `reliability_tile_data` triples to `{label: tone}`."""
    return {label: tone for _, label, tone in tiles}


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
        import inspect
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        return inspect.getsource(getattr(dashboard, name))


class DefaultDateRangeTest(unittest.TestCase):

    def test_window_includes_today_and_n_days(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=MAY28, days=7
        )
        self.assertEqual(end, MAY28)
        self.assertEqual(start, MAY22)

    def test_days_one_yields_today_only(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=MAY28, days=1
        )
        self.assertEqual(start, end)

    def test_days_zero_clamps_to_today_only(self) -> None:
        # `days=0` is non-sensical (an empty window) so the helper
        # clamps to "today only" instead of returning end < start.
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=MAY28, days=0
        )
        self.assertEqual(start, MAY28)
        self.assertEqual(end, MAY28)


class ToWindowTest(unittest.TestCase):

    def test_inclusive_end_becomes_exclusive_midnight(self) -> None:
        # `analytics_read` uses `ts < end`; midnight on the day after
        # `end_date` is what makes events from `end_date` visible.
        _, dashboard = _reload()
        window = dashboard.to_window(MAY01, MAY03)
        self.assertEqual(window.start, _utc_midnight(MAY01))
        self.assertEqual(window.end, _utc_midnight(MAY04))

    def test_reversed_range_is_swapped(self) -> None:
        # The Streamlit two-date input lets the user type end < start.
        # Swapping silently keeps the dashboard useful instead of
        # collapsing to an empty SQL window.
        _, dashboard = _reload()
        window = dashboard.to_window(MAY05, MAY01)
        self.assertEqual(window.start.date(), MAY01)
        self.assertEqual(window.end.date(), MAY06)

    def test_single_day_window(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(MAY01, MAY01)
        self.assertEqual(window.start, _utc_midnight(MAY01))
        self.assertEqual(window.end, _utc_midnight(MAY02))


class ParseIssueNumberTest(unittest.TestCase):

    def test_bare_int(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number("42"), ISSUE_NUMBER)

    def test_hash_prefix_and_whitespace(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number(" #42 "), ISSUE_NUMBER)
        self.assertEqual(dashboard.parse_issue_number("# 42"), ISSUE_NUMBER)

    def test_empty_returns_none(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.parse_issue_number(""))
        self.assertIsNone(dashboard.parse_issue_number("   "))
        self.assertIsNone(dashboard.parse_issue_number("#"))

    def test_non_numeric_returns_none(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.parse_issue_number("abc"))
        self.assertIsNone(dashboard.parse_issue_number("#abc"))

    def test_non_positive_returns_none(self) -> None:
        # GitHub issue numbers start at 1; 0 and negatives are not
        # valid drill-down targets.
        _, dashboard = _reload()
        self.assertIsNone(dashboard.parse_issue_number("0"))
        self.assertIsNone(dashboard.parse_issue_number("-3"))


class DbUnconfiguredMessageTest(unittest.TestCase):

    def test_unset_url_returns_message(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertEqual(
            dashboard.db_unconfigured_message(),
            dashboard.UNCONFIGURED_DB_MESSAGE,
        )

    def test_disable_sentinel_returns_message(self) -> None:
        # Each of the documented disable sentinels collapses to None
        # inside `config`, so the helper should treat them the same.
        for sentinel in ("off", "disabled", "none", "OFF", "Disabled"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload({ANALYTICS_DB_URL_ENV: sentinel})
                self.assertEqual(
                    dashboard.db_unconfigured_message(),
                    dashboard.UNCONFIGURED_DB_MESSAGE,
                )

    def test_configured_url_returns_none(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        self.assertIsNone(dashboard.db_unconfigured_message())


class LazyImportTest(unittest.TestCase):
    """The dashboard module must load without importing `streamlit`
    or `plotly`.

    The polling tick loads `orchestrator.*` modules at process start;
    if `dashboard.py` were to import Streamlit (or Plotly via
    `dashboard_charts`) at module top, every orchestrator deployment
    would have to install the dashboard group. Lazy import inside
    `main()` is the boundary; this test is the guardrail.
    """

    def test_dashboard_only_modules_absent_after_load(self) -> None:
        optional_deps = ("streamlit", "pandas", "plotly")
        with patch.dict(os.environ, _hermetic_env(), clear=True):
            for stale_module in (
                "orchestrator.config",
                "orchestrator.analytics.read",
                "orchestrator.analytics",
                DASHBOARD_MODULE,
                DASHBOARD_CHARTS_MODULE,
                *optional_deps,
            ):
                sys.modules.pop(stale_module, None)
            importlib.import_module(DASHBOARD_MODULE)
            for absent in (*optional_deps, DASHBOARD_CHARTS_MODULE):
                self.assertNotIn(absent, sys.modules)


class ScriptPathLaunchTest(unittest.TestCase):
    """Guard the `streamlit run orchestrator/dashboard.py` launch path.

    The Streamlit launcher executes the file as a top-level script via
    `runpy` with no parent package and prepends the *script's*
    directory (not the repo root) to `sys.path`. A naked relative
    import (`from . import ...`) or a bare absolute import without a
    `sys.path` fix raises `ImportError: attempted relative import with
    no known parent package` before any Streamlit code can render --
    the reviewer caught exactly this regression with
    `AppTest.from_file(...).run()`. We reproduce that `sys.path` shape
    here instead of pulling Streamlit in (the dashboard dependency
    group is opt-in and not installed for the default test sync):
    strip the repo root, insert the script's dir, then `runpy` the
    file with a non-`__main__` run name so `main()` is not invoked.
    """

    def test_runs_without_repo_root_on_syspath(self) -> None:
        import runpy

        launch = _dashboard_launch_paths()
        with _script_launch_sandbox(_is_orchestrator_module):
            # Match Streamlit's launch shape: only the script's
            # directory is on sys.path, the repo root is not.
            _drop_repo_root_from_sys_path(launch.repo_root)
            sys.path.insert(0, str(launch.script_dir))
            _clear_modules(_is_orchestrator_module)

            # `run_name="not_main"` keeps the `if __name__ == "__main__":`
            # block from firing, so the test does not require Streamlit
            # to be installed -- only the top-level imports must
            # succeed under the script-launch sys.path.
            namespace = runpy.run_path(
                str(launch.dashboard_path), run_name="not_main"
            )
            self.assertIn(ENTRYPOINT_ATTR, namespace)
            self.assertIn("analytics_read", namespace)

    def test_stale_parent_cannot_shadow_repo(self) -> None:
        # Script-launch mode carries only `orchestrator/` on `sys.path`, so
        # importing `orchestrator.<x>` before the shim prepends the repo root
        # would bind the parent `orchestrator` package to whatever stale copy
        # is importable and route every later absolute import through it. The
        # shim adds the repo root without importing `orchestrator.*` first, so
        # the real package resolves even with a decoy parent behind the script
        # dir on the path.
        import runpy
        import tempfile
        from pathlib import Path

        launch = _dashboard_launch_paths()
        with _script_launch_sandbox(_is_orchestrator_launch_module) as cleanup:
            decoy_root = cleanup.enter_context(tempfile.TemporaryDirectory())
            # A bare `orchestrator` package with none of the real submodules,
            # standing in for a stale install that shadows the repo root.
            decoy_pkg = Path(decoy_root) / ORCHESTRATOR_PKG
            decoy_pkg.mkdir()
            (decoy_pkg / "__init__.py").write_text("")
            _drop_repo_root_from_sys_path(launch.repo_root)
            # Streamlit's shape (script's own dir first), with the decoy
            # parent reachable just behind it.
            sys.path.insert(0, decoy_root)
            sys.path.insert(0, str(launch.script_dir))
            _clear_modules(_is_orchestrator_launch_module)

            namespace = runpy.run_path(
                str(launch.dashboard_path), run_name="not_main"
            )
            self.assertIn(ENTRYPOINT_ATTR, namespace)
            # The real read model landed -- not the decoy package (which
            # has no `analytics` submodule and would raise on import).
            self.assertEqual(
                namespace["analytics_read"].__name__,
                "orchestrator.analytics.read",
            )

    def test_package_import_ignores_stray_script(self) -> None:
        # A normal package import (`import orchestrator.dashboard`) must
        # resolve the shim via `orchestrator.script_launch`, never a bare
        # `import script_launch`. An unrelated top-level `script_launch.py`
        # earlier on `sys.path` would otherwise shadow the helper or fail the
        # import outright, so the package path must not probe the bare name.
        import importlib
        import tempfile
        from pathlib import Path

        with _script_launch_sandbox(_is_dashboard_script_launch_module) as cleanup:
            stray_dir = cleanup.enter_context(tempfile.TemporaryDirectory())
            # A stray top-level `script_launch` that detonates on import, so a
            # bare `import script_launch` during the package import would fail
            # loudly instead of silently binding the wrong helper.
            (Path(stray_dir) / "script_launch.py").write_text(
                "raise RuntimeError('stray script_launch must not be imported')\n"
            )
            sys.path.insert(0, stray_dir)
            _clear_modules(_is_dashboard_script_launch_module)
            module = importlib.import_module(DASHBOARD_MODULE)
            self.assertTrue(hasattr(module, ENTRYPOINT_ATTR))
            # The package path used `orchestrator.script_launch` and never
            # probed the bare name, so the stray stayed unimported.
            self.assertNotIn("script_launch", sys.modules)


class ResolveStageFilterTest(unittest.TestCase):
    """The stage multiselect default ('all known non-null stages')
    must collapse to `stages=None` so the read-model query does
    not emit a `stage IN (...)` clause that silently excludes
    NULL-stage rows. NULL stages are a legitimate case --
    `stage_evaluation` writes `stage=None` when the issue
    carries no workflow label. The cleared-multiselect signal
    (`[]`) must stay distinct so the reviewer-documented "show
    nothing" path still works.
    """

    def test_all_selected_collapses_to_none(self) -> None:
        _, dashboard = _reload()
        resolved = dashboard.resolve_stage_filter(
            selected=[STAGE_IMPLEMENTING, STAGE_VALIDATING],
            available=(STAGE_IMPLEMENTING, STAGE_VALIDATING),
        )
        self.assertIsNone(resolved)

    def test_no_available_options_returns_none(self) -> None:
        # Empty filter options (DB is empty or has no non-null
        # stages yet) collapses to `None` so the read-model query
        # runs unconstrained on the stage column.
        _, dashboard = _reload()
        resolved = dashboard.resolve_stage_filter(
            selected=[], available=()
        )
        self.assertIsNone(resolved)

    def test_cleared_multiselect_returns_empty_list(self) -> None:
        # Options exist but the operator cleared the selection.
        # The read model encodes `[]` as a tautologically-false
        # predicate; without this branch the cleared state would
        # be indistinguishable from the all-selected default.
        _, dashboard = _reload()
        resolved = dashboard.resolve_stage_filter(
            selected=[],
            available=(STAGE_IMPLEMENTING, STAGE_VALIDATING),
        )
        self.assertEqual(resolved, [])

    def test_proper_subset_passes_through(self) -> None:
        _, dashboard = _reload()
        resolved = dashboard.resolve_stage_filter(
            selected=[STAGE_IMPLEMENTING],
            available=(STAGE_IMPLEMENTING, STAGE_VALIDATING),
        )
        self.assertEqual(resolved, [STAGE_IMPLEMENTING])


class PresetWindowTest(unittest.TestCase):
    """The data-extent-bounded presets anchor at the data extent's
    max date (not today): a freshly-deployed Postgres whose latest
    event is a few days old should still surface a useful window
    without the operator having to flip to Custom and reach for a
    calendar. The redesigned page exposes `3D` / `7D` / `All` inline
    in the topbar; `Custom` stays available as the sidebar fallback.
    """

    def test_three_day_preset_anchors_at_max(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(MAY01, MAY28)
        window = dashboard.preset_window(dashboard.PRESET_3D, extent)
        self.assertIsNotNone(window)
        # Three-day preset spans the max date and the two days before
        # it, exclusive end at midnight the day after the max.
        self.assertEqual(window.start.date(), MAY26)
        self.assertEqual(window.end.date(), MAY29)

    def test_seven_day_preset_anchors_at_max(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(MAY01, MAY28)
        window = dashboard.preset_window(dashboard.PRESET_7D, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), MAY22)
        self.assertEqual(window.end.date(), MAY29)

    def test_seven_day_preset_clamps_to_min(self) -> None:
        # Data extent is only 3 days wide -- "Last 7 days" must
        # clamp the start at the data extent's min, not reach
        # before it.
        _, dashboard = _reload()
        extent = self._extent(MAY26, MAY28)
        window = dashboard.preset_window(dashboard.PRESET_7D, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), MAY26)
        self.assertEqual(window.end.date(), MAY29)

    def test_all_preset_covers_full_extent(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(JAN01, MAY28)
        window = dashboard.preset_window(dashboard.PRESET_ALL, extent)
        self.assertIsNotNone(window)
        self.assertEqual(window.start.date(), JAN01)
        self.assertEqual(window.end.date(), MAY29)

    def test_custom_preset_returns_none(self) -> None:
        # The caller renders a date-range picker when the preset is
        # `Custom`; `preset_window` returns `None` so the caller can
        # branch on a falsy value rather than special-casing the
        # preset string in two places.
        _, dashboard = _reload()
        extent = self._extent(MAY01, MAY28)
        self.assertIsNone(
            dashboard.preset_window(dashboard.PRESET_CUSTOM, extent)
        )

    def test_empty_extent_returns_none(self) -> None:
        _, dashboard = _reload()
        empty = dashboard.DataExtent()
        self.assertIsNone(
            dashboard.preset_window(dashboard.PRESET_7D, empty)
        )

    def test_unknown_preset_returns_none(self) -> None:
        _, dashboard = _reload()
        extent = self._extent(MAY01, MAY28)
        self.assertIsNone(
            dashboard.preset_window("not-a-preset", extent)
        )

    def test_preset_options_match_redesign(self) -> None:
        # Pin the inline labels the topbar exposes (3D / 7D / All)
        # and the full option tuple including the Custom fallback so
        # a future refactor cannot silently re-introduce the old
        # `30d` preset.
        _, dashboard = _reload()
        self.assertEqual(
            dashboard.PRESET_OPTIONS,
            (dashboard.PRESET_3D, dashboard.PRESET_7D,
             dashboard.PRESET_ALL, dashboard.PRESET_CUSTOM),
        )
        self.assertEqual(
            set(dashboard.PRESET_INLINE_LABELS),
            {dashboard.PRESET_3D, dashboard.PRESET_7D,
             dashboard.PRESET_ALL},
        )

    def _extent(self, min_d, max_d):
        _, dashboard = _reload()
        return dashboard.DataExtent(
            min_ts=datetime(min_d.year, min_d.month, min_d.day,
                            tzinfo=timezone.utc),
            max_ts=datetime(max_d.year, max_d.month, max_d.day, 23, 59,
                            tzinfo=timezone.utc),
        )


class PreviousWindowTest(unittest.TestCase):
    """The previous-window helper feeds the KPI delta column. It must
    return a window of the same length immediately before `window`
    so the deltas compare like-for-like (e.g. last-30-days vs the
    30 days before that).
    """

    def test_length_preserved(self) -> None:
        _, dashboard = _reload()
        win = dashboard.to_window(MAY01, MAY07)
        prev = dashboard.previous_window(win)
        self.assertEqual(prev.end, win.start)
        self.assertEqual(prev.end - prev.start, win.end - win.start)

    def test_seven_day_window_has_seven_day_prior(self) -> None:
        _, dashboard = _reload()
        win = dashboard.to_window(MAY22, MAY28)
        prev = dashboard.previous_window(win)
        # `to_window`'s end is exclusive (one day past `end_date`),
        # so the seven-day window spans 7 calendar days; the previous
        # window starts seven days before the current start.
        self.assertEqual(prev.start.date(), MAY15)
        self.assertEqual(prev.end.date(), MAY22)


class KpiDeltaTest(unittest.TestCase):

    def test_positive_delta(self) -> None:
        _, dashboard = _reload()
        self.assertAlmostEqual(dashboard.kpi_delta(125, 100), 0.25)

    def test_negative_delta(self) -> None:
        _, dashboard = _reload()
        self.assertAlmostEqual(dashboard.kpi_delta(75, 100), -0.25)

    def test_zero_previous_returns_none(self) -> None:
        # The dashboard hides the delta indicator rather than
        # rendering an infinity for the zero-baseline case.
        _, dashboard = _reload()
        self.assertIsNone(dashboard.kpi_delta(10, 0))

    def test_negative_previous_returns_none(self) -> None:
        _, dashboard = _reload()
        self.assertIsNone(dashboard.kpi_delta(10, -5))


class ComputeInsightsTest(unittest.TestCase):
    """The insight banners are derived computationally from the
    read-model rows; this test pins the threshold semantics so a
    future tuning pass changes them deliberately.
    """

    def test_no_banners_for_healthy_window(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(events=100, agent_runs=50, failed=0, cost=10.0)
        self.assertEqual(dashboard.compute_insights(summary), [])

    def test_high_failure_rate_emits_error(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(agent_runs=10, failed=3)
        banners = dashboard.compute_insights(summary)
        self.assertEqual(len(banners), 1)
        self.assertEqual(banners[0].severity, "error")
        self.assertIn("3 of 10", banners[0].message)

    def test_low_failure_rate_skips_banner(self) -> None:
        _, dashboard = _reload()
        summary = self._summary(agent_runs=100, failed=5)
        self.assertEqual(dashboard.compute_insights(summary), [])

    def test_unpriced_coverage_emits_warning(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import CostCoverageRow
        summary = self._summary()
        cov = [
            CostCoverageRow(cost_source=COST_SOURCE_REPORTED, runs=70),
            CostCoverageRow(cost_source=COST_SOURCE_UNKNOWN_PRICE, runs=20),
            CostCoverageRow(cost_source="unknown", runs=10),
        ]
        banners = dashboard.compute_insights(
            summary, cost_coverage_rows=cov
        )
        # 30 / 100 = 30% unpriced -- well over the 10% threshold.
        self.assertTrue(
            any(
                banner.severity == "warning"
                and "30 of 100" in banner.message
                for banner in banners
            )
        )

    def test_unpriced_below_threshold_skips(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import CostCoverageRow
        summary = self._summary()
        cov = [
            CostCoverageRow(cost_source=COST_SOURCE_REPORTED, runs=99),
            CostCoverageRow(cost_source=COST_SOURCE_UNKNOWN_PRICE, runs=1),
        ]
        self.assertEqual(
            dashboard.compute_insights(summary, cost_coverage_rows=cov),
            [],
        )

    def _summary(
        self,
        *,
        events=0,
        cost=0.0,
        agent_runs=0,
        failed=0,
    ):
        _, dashboard = _reload()
        return dashboard.Summary(
            total_events=events,
            total_agent_runs=agent_runs,
            failed_agent_runs=failed,
            total_cost_usd=cost,
        )


class ReliabilityTileDataTest(unittest.TestCase):
    """The redesigned reliability panel sources every tile from
    `Summary`'s window-wide aggregates so a long window with more
    than `DEFAULT_RECENT_AGENT_EXITS` (100) rows still sees every
    timeout / failure -- the earlier draft computed these off the
    LIMIT-capped recent-runs read and silently undercounted."""

    def test_timeouts_use_full_window_summary(self) -> None:
        _, dashboard = _reload()
        # Window holds far more agent runs than the recent-runs cap, with
        # failures and timeouts mixed in.
        summary = self._summary(
            total_agent_runs=FULL_WINDOW_AGENT_RUNS,
            failed_agent_runs=FULL_WINDOW_FAILURES,
            timed_out_agent_runs=FULL_WINDOW_TIMEOUTS,
        )
        tiles = dashboard.reliability_tile_data(
            summary, resolved=12, rejected=2,
        )
        by_label = _tile_value_tones(tiles)
        # Headline tiles all pulled off Summary directly:
        self.assertEqual(by_label[KPI_AGENT_RUNS][0], FULL_WINDOW_AGENT_RUNS)
        self.assertEqual(by_label[KPI_FAILURES][0], FULL_WINDOW_FAILURES)
        self.assertEqual(by_label[KPI_TIMEOUTS][0], FULL_WINDOW_TIMEOUTS)
        # Tone flips when the count crosses zero so the CSS class
        # paints the tile.
        self.assertEqual(by_label[KPI_TIMEOUTS][1], "bad")
        self.assertEqual(by_label[KPI_FAILURES][1], "warn")

    def test_zero_runs_does_not_divide_by_zero(self) -> None:
        # Empty window: success rate collapses to 0% (no runs, no
        # successes) instead of raising a ZeroDivisionError. The
        # redesigned page renders the tile anyway so the operator
        # can confirm the window really is empty.
        _, dashboard = _reload()
        summary = self._summary(
            total_agent_runs=0,
            failed_agent_runs=0,
            timed_out_agent_runs=0,
        )
        tiles = dashboard.reliability_tile_data(summary)
        by_label = _tile_values(tiles)
        self.assertEqual(by_label[KPI_AGENT_RUNS], 0)
        self.assertEqual(by_label["Success rate"], "0%")
        self.assertEqual(by_label[KPI_TIMEOUTS], 0)

    def test_clean_window_has_neutral_tones(self) -> None:
        # No failures, no timeouts: the warn / bad tones drop off
        # so the panel reads as healthy at a glance.
        _, dashboard = _reload()
        summary = self._summary(
            total_agent_runs=20,
            failed_agent_runs=0,
            timed_out_agent_runs=0,
        )
        tiles = dashboard.reliability_tile_data(summary)
        by_label = _tile_tones(tiles)
        self.assertEqual(by_label[KPI_FAILURES], "")
        self.assertEqual(by_label[KPI_TIMEOUTS], "")

    def _summary(self, **kw):
        _, dashboard = _reload()
        from orchestrator.analytics.read import Summary
        return Summary(**kw)


class ReworkTotalsTest(unittest.TestCase):
    """The rework KPI tile reads off `rework_totals`. Pin the shape so
    a future tweak does not silently shift which buckets count as
    rework.
    """

    def test_initial_bucket_excluded(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import ReviewRoundBucketRow
        rows = [
            ReviewRoundBucketRow(
                bucket=BUCKET_INITIAL, runs=5, failed=0, total_cost_usd=50.0
            ),
            ReviewRoundBucketRow(
                bucket=BUCKET_FIRST_ROUND, runs=2, failed=1, total_cost_usd=20.0
            ),
        ]
        total, rework = dashboard.rework_totals(rows)
        self.assertAlmostEqual(total, 70.0)
        self.assertAlmostEqual(rework, 20.0)

    def test_unknown_bucket_excluded(self) -> None:
        # `unknown` is pre-review work surfaced for visibility, NOT
        # rework -- exclude it from the rework cost.
        _, dashboard = _reload()
        from orchestrator.analytics.read import ReviewRoundBucketRow
        rows = [
            ReviewRoundBucketRow(
                bucket="unknown", runs=3, failed=0, total_cost_usd=10.0
            ),
            ReviewRoundBucketRow(
                bucket="2", runs=1, failed=0, total_cost_usd=5.0
            ),
        ]
        total, rework = dashboard.rework_totals(rows)
        self.assertAlmostEqual(total, 15.0)
        self.assertAlmostEqual(rework, 5.0)

    def test_empty_rows_returns_zero(self) -> None:
        _, dashboard = _reload()
        total, rework = dashboard.rework_totals([])
        self.assertAlmostEqual(total, 0.0)
        self.assertAlmostEqual(rework, 0.0)


class TopExpensiveIssuesTest(unittest.TestCase):

    def test_sorts_by_cost_desc(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue(REPO_A, 1, 0.1),
            self._issue(REPO_B, 2, 1.0),
            self._issue(REPO_C, 3, 0.5),
        ]
        top = dashboard.top_expensive_issues(rows, limit=2)
        self.assertEqual([(row.repo, row.issue) for row in top],
                         [(REPO_B, 2), (REPO_C, 3)])

    def test_none_cost_sorts_last(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue(REPO_A, 1, None),
            self._issue(REPO_B, 2, 0.1),
        ]
        top = dashboard.top_expensive_issues(rows, limit=5)
        self.assertEqual([row.issue for row in top], [2, 1])

    def test_limit_zero_returns_empty(self) -> None:
        _, dashboard = _reload()
        rows = [self._issue(REPO_A, 1, 0.1)]
        self.assertEqual(dashboard.top_expensive_issues(rows, limit=0), [])

    def test_ties_break_on_event_count_then_identity(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._issue(REPO_A, 1, 1.0, events=2),
            self._issue(REPO_A, 2, 1.0, events=10),
            self._issue(REPO_B, 1, 1.0, events=2),
        ]
        top = dashboard.top_expensive_issues(rows)
        # Higher event count first, then (repo, issue) ascending.
        self.assertEqual(
            [(row.repo, row.issue) for row in top],
            [(REPO_A, 2), (REPO_A, 1), (REPO_B, 1)],
        )

    def _issue(self, repo, num, cost, events=1):
        _, dashboard = _reload()
        from orchestrator.analytics.read import IssueSummaryRow
        return IssueSummaryRow(
            repo=repo,
            issue=num,
            event_count=events,
            first_seen=FIRST_SEEN,
            last_seen=LAST_SEEN,
            latest_stage=STAGE_IMPLEMENTING,
            agent_exits=1,
            total_cost_usd=cost,
            total_input_tokens=0,
            total_output_tokens=0,
        )


class IssuesTableHtmlTest(unittest.TestCase):
    """The "Most expensive issues" panel is hand-rolled HTML (rather
    than `st.dataframe`) so it can carry the standalone mock's
    in-row cost bars and clean / fail status pills.
    """

    def test_columns_match_standalone_mock(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, 12.0)]
        html = dashboard._issues_table_html(rows)
        for header in ("Issue", "Cost", COLUMN_RUNS, "Review rds",
                       "Retries", "Status"):
            self.assertIn(f">{header}<", html)

    def test_clean_pill_without_failures(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, 4.0, failed=0)]
        html = dashboard._issues_table_html(rows)
        self.assertIn('class="orch-pill ok"', html)
        self.assertIn(">clean<", html)
        self.assertNotIn('class="orch-pill bad"', html)

    def test_fail_pill_with_failures(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, 4.0, failed=3)]
        html = dashboard._issues_table_html(rows)
        self.assertIn('class="orch-pill bad"', html)
        self.assertIn(">3 fail<", html)

    def test_in_row_cost_bar_relative_to_max(self) -> None:
        # Cheapest issue's bar is a fraction of the most expensive
        # issue's full-width bar.
        _, dashboard = _reload()
        rows = [
            self._row(REPO_A, 1, 10.0),
            self._row(REPO_B, 2, 5.0),
        ]
        html = dashboard._issues_table_html(rows)
        # Full-width bar on the most expensive issue and a half-
        # width bar on the cheaper one.
        self.assertIn("width:100.0%", html)
        self.assertIn("width:50.0%", html)

    def test_review_rounds_three_or_more_warn_tone(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(REPO_A, 1, 4.0, max_round=4)]
        html = dashboard._issues_table_html(rows)
        # High-review-round cells get the warn class so the operator
        # can spot rework-heavy issues at a glance.
        self.assertIn('class="orch-badge-warn">4', html)

    def _row(self, repo, issue, cost, *, failed=0, max_round=None,
             max_retry=None):
        _, dashboard = _reload()
        from orchestrator.analytics.read import IssueSummaryRow
        return IssueSummaryRow(
            repo=repo,
            issue=issue,
            event_count=10,
            first_seen=FIRST_SEEN,
            last_seen=LAST_SEEN,
            latest_stage=STAGE_IMPLEMENTING,
            agent_exits=4,
            total_cost_usd=cost,
            total_input_tokens=0,
            total_output_tokens=0,
            max_review_round=max_round,
            failed_agent_runs=failed,
            max_retry_count=max_retry,
        )


class SkillTriggersHtmlTest(unittest.TestCase):
    """The skill-trigger-rates aggregate table (the invocation-level
    diagnostic beneath the session-adoption matrix) is hand-rolled HTML
    (matching the backend-efficiency cards and cost-coverage bar) so the
    small, categorical per-(role, backend) table reads cleanly even when
    every rate is 0% -- the `TRACK_SKILL_TRIGGERS=off` baseline.
    """

    def test_columns_present(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(ROLE_DEVELOPER, BACKEND_CLAUDE, 9, 3, 3)]
        html = dashboard._skill_triggers_html(rows)
        for header in ("Role", "Backend", COLUMN_RUNS, "Skill runs",
                       "Trigger rate", "Triggers"):
            self.assertIn(f">{header}<", html)

    def test_rate_rendered_as_percent(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1, 1)]
        html = dashboard._skill_triggers_html(rows)
        # 1 of 4 runs triggered a skill -> 25%.
        self.assertIn(">25%<", html)

    def test_rate_bar_relative_to_busiest_group(self) -> None:
        _, dashboard = _reload()
        rows = [
            self._row(ROLE_DEVELOPER, BACKEND_CLAUDE, 10, 10, 10),  # rate 1.0
            self._row(ROLE_REVIEWER, BACKEND_CODEX, 10, 5, 5),       # rate 0.5
        ]
        html = dashboard._skill_triggers_html(rows)
        # Full-width bar on the 100%-rate group, half-width on the 50%.
        self.assertIn("width:100.0%", html)
        self.assertIn("width:50.0%", html)

    def test_zero_rate_group_renders_zero_percent(self) -> None:
        # A quiet reviewer (0 skill runs) is a real signal, not a
        # dropped row: it renders as an explicit 0% with an empty bar.
        _, dashboard = _reload()
        rows = [self._row(ROLE_REVIEWER, BACKEND_CODEX, 5, 0, 0)]
        html = dashboard._skill_triggers_html(rows)
        self.assertIn(">0%<", html)
        self.assertIn("width:0.0%", html)

    def test_role_html_escaped(self) -> None:
        _, dashboard = _reload()
        rows = [self._row(ROLE_WITH_MARKUP, BACKEND_CLAUDE, 1, 0, 0)]
        html = dashboard._skill_triggers_html(rows)
        self.assertIn("dev&lt;&amp;&gt;", html)
        self.assertNotIn(ROLE_WITH_MARKUP, html)

    def _row(self, role, backend, runs, skill_runs, triggers):
        from orchestrator.analytics.read import SkillTriggerRateRow
        return SkillTriggerRateRow(
            agent_role=role,
            backend=backend,
            runs=runs,
            skill_runs=skill_runs,
            total_triggers=triggers,
        )


class SkillMatrixHtmlTest(unittest.TestCase):
    """The per-skill trigger matrix is the second table in the skill
    panel's invocation-level diagnostics expander -- a hand-rolled HTML
    table over `get_skill_trigger_matrix` with one row per
    `(repo, agent_role, backend, skill)` cell. It folds each repo's
    skill catalog into the observed triggers so an offered-but-never-
    triggered skill renders as an explicit `0` cell, and degrades to a
    clear fallback notice when no catalog-backed matrix can be built.
    """

    def test_columns_match_issue_spec(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("owner/repo", "develop", ROLE_DEVELOPER, BACKEND_CLAUDE, 2)]
        html = dashboard._skill_matrix_html(rows)
        for header in ("Repo", "Role", "Backend", "Skill",
                       COLUMN_RUNS, "Runs with skill", "Trigger rate"):
            self.assertIn(f">{header}<", html)

    def test_cell_values_rendered(self) -> None:
        _, dashboard = _reload()
        # Distinct cohort total (Runs) and trigger count (Runs with skill)
        # so both columns are exercised independently.
        rows = [self._row(
            "owner/repo", "develop", ROLE_DEVELOPER, BACKEND_CLAUDE, 5, skill_runs=3,
        )]
        html = dashboard._skill_matrix_html(rows)
        # Full repo path (not just the trailing component) so two repos
        # that share a short name stay distinct in a cross-repo matrix.
        self.assertIn(">owner/repo<", html)
        self.assertIn(">developer<", html)
        self.assertIn(">claude<", html)
        self.assertIn(">develop<", html)
        self.assertIn(">5<", html)
        self.assertIn(">3<", html)
        # Trigger rate is derived from the two counts (3/5) and rounds to
        # a whole percent, matching the aggregate table's format.
        self.assertIn(">60%<", html)

    def test_zero_count_renders_muted_zero(self) -> None:
        # An offered-but-never-triggered catalog cell is a real
        # "offered but quiet" signal, not a dropped row: its "Runs with
        # skill" renders as an explicit (muted) 0 rather than going
        # missing, while the cohort `Runs` total stays a plain number.
        _, dashboard = _reload()
        rows = [self._row(
            "owner/repo", "review", ROLE_DEVELOPER, BACKEND_CLAUDE, 4, skill_runs=0,
        )]
        html = dashboard._skill_matrix_html(rows)
        self.assertIn("orch-skillmatrix-zero", html)
        self.assertIn(">0<", html)
        # The derived trigger rate is `0%` and muted on the same signal,
        # so an offered-but-quiet cell reads consistently across columns.
        self.assertIn('<span class="orch-skillmatrix-zero">0%</span>', html)
        # The cohort total is not muted -- it is a plain right-aligned cell.
        self.assertIn('<td class="r">4</td>', html)

    def test_repo_role_backend_skill_html_escaped(self) -> None:
        # Every free-text cell is HTML-escaped so a skill / repo / role
        # name carrying markup cannot break out of the table.
        _, dashboard = _reload()
        rows = [self._row("o/<r&>", "sk<i>ll", ROLE_WITH_MARKUP, "back<end>", 1)]
        html = dashboard._skill_matrix_html(rows)
        self.assertIn("o/&lt;r&amp;&gt;", html)
        self.assertIn("sk&lt;i&gt;ll", html)
        self.assertIn("dev&lt;&amp;&gt;", html)
        self.assertIn("back&lt;end&gt;", html)
        self.assertNotIn("<r&>", html)
        self.assertNotIn(ROLE_WITH_MARKUP, html)

    def test_empty_rows_render_fallback_not_table(self) -> None:
        # No catalog records matched and no run fired a skill -> there
        # is no catalog-backed matrix to build, so a clear fallback
        # notice renders in place of the table.
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html([])
        self.assertIn("orch-skillmatrix-empty", html)
        self.assertIn("No catalog-backed skill matrix", html)
        # Names the opt-in switch so a quiet panel is not mistaken for a
        # bug, mirroring the aggregate table's caption.
        self.assertIn("TRACK_SKILL_TRIGGERS", html)
        # The table markup itself is not emitted on the fallback path.
        self.assertNotIn("<table", html)

    def test_fallback_message_is_html_escaped(self) -> None:
        # The fallback message is escaped before it lands in the div, so
        # the apostrophe-carrying copy renders without breaking out.
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html([])
        self.assertIn("&#x27;", html)

    def _row(self, repo, skill, role, backend, runs, skill_runs=None):
        from orchestrator.analytics.read import SkillTriggerMatrixRow
        return SkillTriggerMatrixRow(
            repo=repo,
            skill=skill,
            agent_role=role,
            backend=backend,
            runs=runs,
            skill_runs=runs if skill_runs is None else skill_runs,
        )


class SkillMatrixSortTest(unittest.TestCase):
    """The per-skill trigger matrix column headers are clickable sort
    controls: each is an anchor writing `mtx_sort` / `mtx_dir` query
    params, and the caller feeds the parsed `(column, direction)` back
    into `_skill_matrix_html` so the rows re-sort on that column and the
    active header shows a ▲ / ▼ indicator.
    """

    def test_headers_link_to_self_for_sorting(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html(self._rows())
        # Every column is an in-tab anchor pointing at its own sort param.
        for key in MTX_SORT_KEYS:
            self.assertIn(f"?{MTX_SORT_PARAM}={key}&{MTX_DIR_PARAM}=", html)
        self.assertIn('target="_self"', html)
        # Text columns default a first click to ascending, numeric ones to
        # descending (largest first is the interesting end for counts).
        self.assertIn(f"?{MTX_SORT_PARAM}=repo&{MTX_DIR_PARAM}={SORT_ASC}", html)
        self.assertIn(
            f"?{MTX_SORT_PARAM}=runs&{MTX_DIR_PARAM}={SORT_DESC}", html
        )
        # With no active sort no header carries a direction indicator (the
        # class still appears in the CSS block, so match the span markup).
        self.assertNotIn('<span class="orch-skillmatrix-sort">', html)

    def test_descending_shows_down_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html(
            self._rows(), sort_key=SORT_KEY_RUNS, descending=True,
        )
        # Exactly one column is marked active, and it shows the ▼ arrow.
        self.assertEqual(
            html.count('<span class="orch-skillmatrix-sort">'), 1,
        )
        self.assertIn(
            '<span class="orch-skillmatrix-sort">▼</span>', html,
        )
        # Re-clicking the active (descending) column flips it to ascending.
        self.assertIn(
            f"?{MTX_SORT_PARAM}=runs&{MTX_DIR_PARAM}={SORT_ASC}", html
        )

    def test_ascending_shows_up_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_matrix_html(
            self._rows(), sort_key="repo", descending=False,
        )
        self.assertIn(
            '<span class="orch-skillmatrix-sort">▲</span>', html,
        )
        self.assertIn(
            f"?{MTX_SORT_PARAM}=repo&{MTX_DIR_PARAM}={SORT_DESC}", html
        )

    def test_rows_render_in_selected_column_order(self) -> None:
        _, dashboard = _reload()
        asc = dashboard._skill_matrix_html(
            self._rows(), sort_key=SORT_KEY_RUNS, descending=False,
        )
        # runs 2 < 5 < 9 -> repos b, c, a in that order.
        self.assertLess(asc.index(">b/repo<"), asc.index(REPO_C_CELL_FRAGMENT))
        self.assertLess(asc.index(REPO_C_CELL_FRAGMENT), asc.index(">a/repo<"))
        desc = dashboard._skill_matrix_html(
            self._rows(), sort_key=SORT_KEY_RUNS, descending=True,
        )
        self.assertLess(desc.index(">a/repo<"), desc.index(REPO_C_CELL_FRAGMENT))
        self.assertLess(desc.index(REPO_C_CELL_FRAGMENT), desc.index(">b/repo<"))

    def test_unsorted_defaults_repo_asc_rate_desc(self) -> None:
        # No sort key -> the default view orders rows by repo ascending,
        # then trigger rate descending within each repo, so each repo's
        # hottest skills lead. Two rows share a repo with different rates
        # so both keys are exercised (skills identify the rows uniquely).
        _, dashboard = _reload()
        rows = [
            self._row("b/repo", "alpha", ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1),
            self._row("a/repo", "beta", ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1),
            self._row("a/repo", "gamma", ROLE_REVIEWER, BACKEND_CODEX, 4, 3),
        ]
        html = dashboard._skill_matrix_html(rows)
        # Within a/repo, rate descending: gamma (75%) precedes beta (25%).
        self.assertLess(
            html.index(">gamma<"), html.index(">beta<"),
        )
        # Repo ascending: the a/repo rows precede the b/repo row.
        self.assertLess(
            html.index(">beta<"), html.index(">alpha<"),
        )

    def test_sort_helper_unknown_key_is_identity(self) -> None:
        from orchestrator import dashboard_skill_matrix
        rows = self._rows()
        sorted_rows = dashboard_skill_matrix._sort_skill_matrix_rows(rows, None, False)
        self.assertEqual(sorted_rows, rows)
        sorted_rows = dashboard_skill_matrix._sort_skill_matrix_rows(rows, "bogus", True)
        self.assertEqual(sorted_rows, rows)

    def test_parse_matrix_sort_from_query_params(self) -> None:
        _, dashboard = _reload()
        cases = [
            ({}, (None, False)),
            ({MTX_SORT_PARAM: SORT_KEY_RUNS}, (SORT_KEY_RUNS, False)),
            ({MTX_SORT_PARAM: SORT_KEY_RUNS, MTX_DIR_PARAM: SORT_DESC}, (SORT_KEY_RUNS, True)),
            ({MTX_SORT_PARAM: SORT_KEY_RUNS, MTX_DIR_PARAM: SORT_ASC}, (SORT_KEY_RUNS, False)),
            ({MTX_SORT_PARAM: "rate", MTX_DIR_PARAM: SORT_DESC}, ("rate", True)),
            # An unknown / stale column degrades to the default order
            # rather than raising.
            ({MTX_SORT_PARAM: "bogus", MTX_DIR_PARAM: SORT_DESC}, (None, False)),
            ({MTX_DIR_PARAM: SORT_DESC}, (None, False)),
        ]
        for query_params, expected in cases:
            with self.subTest(params=query_params):
                self.assertEqual(
                    dashboard.parse_skill_matrix_sort(query_params), expected,
                )
        # `params` is the public keyword; callers may pass it by name.
        self.assertEqual(
            dashboard.parse_skill_matrix_sort(params={MTX_SORT_PARAM: SORT_KEY_RUNS}),
            (SORT_KEY_RUNS, False),
        )

    def _row(self, repo, skill, role, backend, runs, skill_runs=None):
        from orchestrator.analytics.read import SkillTriggerMatrixRow
        return SkillTriggerMatrixRow(
            repo=repo,
            skill=skill,
            agent_role=role,
            backend=backend,
            runs=runs,
            skill_runs=runs if skill_runs is None else skill_runs,
        )

    def _rows(self):
        # Distinct repo / runs values per row so an ordering assertion can
        # key off either without ambiguity.
        return [
            self._row("b/repo", "alpha", ROLE_DEVELOPER, BACKEND_CLAUDE, 2, 1),
            self._row("a/repo", "beta", ROLE_REVIEWER, BACKEND_CODEX, 9, 9),
            self._row("c/repo", "gamma", ROLE_DEVELOPER, BACKEND_CLAUDE, 5, 0),
        ]


class SkillAdoptionHtmlTest(unittest.TestCase):
    """The primary per-session skill-adoption matrix -- a hand-rolled HTML
    table over `get_skill_adoption` with one row per
    `(repo, agent_role, backend, skill)` cell. It counts skill use by
    logical agent session, so an incidental `SKILL.md` reference surfaces as
    its own diagnostic column and can never raise the adoption rate, and it
    degrades to a clear fallback notice when no session evidence exists.
    """

    def test_columns_match_issue_spec(self) -> None:
        _, dashboard = _reload()
        rows = [self._row("owner/repo", "develop", ROLE_DEVELOPER, BACKEND_CLAUDE, 3, 2)]
        html = dashboard._skill_adoption_html(rows)
        for header in ("Repo", "Role", "Backend", "Skill", "Sessions",
                       "Sessions using skill", "Adoption rate",
                       "Invocation loads", "Incidental references"):
            self.assertIn(f">{header}<", html)

    def test_cell_values_rendered(self) -> None:
        _, dashboard = _reload()
        # Distinct session denominator / numerator / diagnostics so every
        # column is exercised independently.
        rows = [self._row(
            "owner/repo", "develop", ROLE_DEVELOPER, BACKEND_CLAUDE,
            41, 37, invocations=122, load_rows=38, incidental=2,
        )]
        html = dashboard._skill_adoption_html(rows)
        # Full repo path (not just the trailing component) so two repos that
        # share a short name stay distinct in a cross-repo matrix.
        self.assertIn(">owner/repo<", html)
        self.assertIn(">developer<", html)
        self.assertIn(">claude<", html)
        self.assertIn(">develop<", html)
        self.assertIn('<td class="r">41</td>', html)
        self.assertIn('<td class="r">37</td>', html)
        # Adoption rate is derived from the two session counts (37/41) and
        # rounds to a whole percent.
        self.assertIn('<td class="r">90%</td>', html)
        self.assertIn('<td class="r">38</td>', html)
        self.assertIn('<td class="r">2</td>', html)

    def test_incidental_reference_never_raises_adoption(self) -> None:
        # A purely-incidental cell -- the skill's `SKILL.md` was referenced
        # but never loaded, and no session had it available -- carries zero
        # sessions / zero adopted and an undefined (em-dash) rate, so its
        # incidental count can never be mistaken for adoption.
        _, dashboard = _reload()
        rows = [self._row(
            "owner/repo", "review", ROLE_DEVELOPER, BACKEND_CLAUDE,
            0, 0, invocations=1, load_rows=0, incidental=1,
        )]
        html = dashboard._skill_adoption_html(rows)
        # No available session -> the rate is undefined, rendered as a muted
        # em-dash rather than a misleading percentage.
        self.assertIn('<span class="orch-skilladopt-zero">—</span>', html)
        # The incidental reference is its own diagnostic column, visible as a
        # plain count while sessions / adopted stay a muted zero.
        self.assertIn('<td class="r">1</td>', html)
        self.assertIn('<span class="orch-skilladopt-zero">0</span>', html)
        # An incidental mention never produces an adoption percentage.
        self.assertNotIn("%</td>", html)
        self.assertNotIn("%</span>", html)

    def test_available_but_unadopted_renders_muted_zero_percent(self) -> None:
        # A skill available to sessions that none loaded is a real "offered
        # but ignored" signal: its adoption rate renders as an explicit
        # (muted) 0% rather than the undefined em-dash.
        _, dashboard = _reload()
        rows = [self._row(
            "owner/repo", "review", ROLE_DEVELOPER, BACKEND_CLAUDE, 5, 0,
        )]
        html = dashboard._skill_adoption_html(rows)
        self.assertIn('<span class="orch-skilladopt-zero">0%</span>', html)
        # The session denominator is a real count, not muted.
        self.assertIn('<td class="r">5</td>', html)

    def test_repo_role_backend_skill_html_escaped(self) -> None:
        # Every free-text cell is HTML-escaped so a skill / repo / role name
        # carrying markup cannot break out of the table.
        _, dashboard = _reload()
        rows = [self._row("o/<r&>", "sk<i>ll", ROLE_WITH_MARKUP, "back<end>", 1, 1)]
        html = dashboard._skill_adoption_html(rows)
        self.assertIn("o/&lt;r&amp;&gt;", html)
        self.assertIn("sk&lt;i&gt;ll", html)
        self.assertIn("dev&lt;&amp;&gt;", html)
        self.assertIn("back&lt;end&gt;", html)
        self.assertNotIn("<r&>", html)
        self.assertNotIn(ROLE_WITH_MARKUP, html)

    def test_empty_rows_render_fallback_not_table(self) -> None:
        # No session evidence -> a clear fallback notice renders in place of
        # the table, naming the opt-in switch so a quiet panel is not
        # mistaken for a bug.
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html([])
        self.assertIn("orch-skilladopt-empty", html)
        self.assertIn("No per-session skill adoption", html)
        self.assertIn("TRACK_SKILL_TRIGGERS", html)
        # The table markup itself is not emitted on the fallback path.
        self.assertNotIn("<table", html)

    def test_fallback_message_is_html_escaped(self) -> None:
        # The fallback message is escaped before it lands in the div, so the
        # apostrophe-carrying copy renders without breaking out.
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html([])
        self.assertIn("&#x27;", html)

    def _row(
        self, repo, skill, role, backend, sessions, adopted,
        *, invocations=None, load_rows=0, incidental=0,
    ):
        from orchestrator.analytics.read import SkillAdoptionRow
        return SkillAdoptionRow(
            repo=repo,
            skill=skill,
            agent_role=role,
            backend=backend,
            sessions=sessions,
            adopted=adopted,
            invocations=sessions if invocations is None else invocations,
            load_rows=load_rows,
            incidental=incidental,
        )


class SkillAdoptionSortTest(unittest.TestCase):
    """The per-session adoption matrix column headers are clickable sort
    controls: each is an anchor writing `adopt_sort` / `adopt_dir` query
    params, and the caller feeds the parsed `(column, direction)` back into
    `_skill_adoption_html` so the rows re-sort on that column and the active
    header shows a ▲ / ▼ indicator.
    """

    def test_headers_link_to_self_for_sorting(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html(self._rows())
        # Every column is an in-tab anchor pointing at its own sort param.
        for key in ADOPT_SORT_KEYS:
            self.assertIn(f"?{ADOPT_SORT_PARAM}={key}&{ADOPT_DIR_PARAM}=", html)
        self.assertIn('target="_self"', html)
        # Text columns default a first click to ascending, numeric ones to
        # descending (largest first is the interesting end for counts).
        self.assertIn(f"?{ADOPT_SORT_PARAM}=repo&{ADOPT_DIR_PARAM}={SORT_ASC}", html)
        self.assertIn(
            f"?{ADOPT_SORT_PARAM}=sessions&{ADOPT_DIR_PARAM}={SORT_DESC}", html
        )
        # With no active sort no header carries a direction indicator.
        self.assertNotIn('<span class="orch-skilladopt-sort">', html)

    def test_descending_shows_down_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html(
            self._rows(), sort_key=SORT_KEY_SESSIONS, descending=True,
        )
        # Exactly one column is marked active, and it shows the ▼ arrow.
        self.assertEqual(
            html.count('<span class="orch-skilladopt-sort">'), 1,
        )
        self.assertIn(
            '<span class="orch-skilladopt-sort">▼</span>', html,
        )
        # Re-clicking the active (descending) column flips it to ascending.
        self.assertIn(
            f"?{ADOPT_SORT_PARAM}=sessions&{ADOPT_DIR_PARAM}={SORT_ASC}", html
        )

    def test_ascending_shows_up_arrow_and_flips(self) -> None:
        _, dashboard = _reload()
        html = dashboard._skill_adoption_html(
            self._rows(), sort_key="repo", descending=False,
        )
        self.assertIn(
            '<span class="orch-skilladopt-sort">▲</span>', html,
        )
        self.assertIn(
            f"?{ADOPT_SORT_PARAM}=repo&{ADOPT_DIR_PARAM}={SORT_DESC}", html
        )

    def test_rows_render_in_selected_column_order(self) -> None:
        _, dashboard = _reload()
        asc = dashboard._skill_adoption_html(
            self._rows(), sort_key=SORT_KEY_SESSIONS, descending=False,
        )
        # sessions 2 < 5 < 9 -> repos b, c, a in that order.
        self.assertLess(asc.index(">b/repo<"), asc.index(REPO_C_CELL_FRAGMENT))
        self.assertLess(asc.index(REPO_C_CELL_FRAGMENT), asc.index(">a/repo<"))
        desc = dashboard._skill_adoption_html(
            self._rows(), sort_key=SORT_KEY_SESSIONS, descending=True,
        )
        self.assertLess(desc.index(">a/repo<"), desc.index(REPO_C_CELL_FRAGMENT))
        self.assertLess(desc.index(REPO_C_CELL_FRAGMENT), desc.index(">b/repo<"))

    def test_unsorted_defaults_repo_asc_rate_desc(self) -> None:
        # No sort key -> the default view orders rows by repo ascending, then
        # adoption rate descending within each repo, so each repo's
        # most-adopted skills lead. Two rows share a repo with different
        # rates so both keys are exercised (skills identify the rows).
        _, dashboard = _reload()
        rows = [
            self._row("b/repo", "alpha", ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1),
            self._row("a/repo", "beta", ROLE_DEVELOPER, BACKEND_CLAUDE, 4, 1),
            self._row("a/repo", "gamma", ROLE_REVIEWER, BACKEND_CODEX, 4, 3),
        ]
        html = dashboard._skill_adoption_html(rows)
        # Within a/repo, rate descending: gamma (75%) precedes beta (25%).
        self.assertLess(
            html.index(">gamma<"), html.index(">beta<"),
        )
        # Repo ascending: the a/repo rows precede the b/repo row.
        self.assertLess(
            html.index(">beta<"), html.index(">alpha<"),
        )

    def test_sort_helper_unknown_key_is_identity(self) -> None:
        from orchestrator import dashboard_skill_adoption
        rows = self._rows()
        sorted_rows = dashboard_skill_adoption._sort_skill_adoption_rows(
            rows, None, False,
        )
        self.assertEqual(sorted_rows, rows)
        sorted_rows = dashboard_skill_adoption._sort_skill_adoption_rows(
            rows, "bogus", True,
        )
        self.assertEqual(sorted_rows, rows)

    def test_parse_adoption_sort_from_query_params(self) -> None:
        _, dashboard = _reload()
        cases = [
            ({}, (None, False)),
            ({ADOPT_SORT_PARAM: SORT_KEY_SESSIONS}, (SORT_KEY_SESSIONS, False)),
            (
                {ADOPT_SORT_PARAM: SORT_KEY_SESSIONS, ADOPT_DIR_PARAM: SORT_DESC},
                (SORT_KEY_SESSIONS, True),
            ),
            (
                {ADOPT_SORT_PARAM: SORT_KEY_SESSIONS, ADOPT_DIR_PARAM: SORT_ASC},
                (SORT_KEY_SESSIONS, False),
            ),
            ({ADOPT_SORT_PARAM: "rate", ADOPT_DIR_PARAM: SORT_DESC}, ("rate", True)),
            # An unknown / stale column degrades to the default order rather
            # than raising.
            ({ADOPT_SORT_PARAM: "bogus", ADOPT_DIR_PARAM: SORT_DESC}, (None, False)),
            ({ADOPT_DIR_PARAM: SORT_DESC}, (None, False)),
        ]
        for query_params, expected in cases:
            with self.subTest(params=query_params):
                self.assertEqual(
                    dashboard.parse_skill_adoption_sort(query_params), expected,
                )
        # `params` is the public keyword; callers may pass it by name.
        self.assertEqual(
            dashboard.parse_skill_adoption_sort(
                params={ADOPT_SORT_PARAM: SORT_KEY_SESSIONS},
            ),
            (SORT_KEY_SESSIONS, False),
        )

    def _row(self, repo, skill, role, backend, sessions, adopted):
        from orchestrator.analytics.read import SkillAdoptionRow
        return SkillAdoptionRow(
            repo=repo,
            skill=skill,
            agent_role=role,
            backend=backend,
            sessions=sessions,
            adopted=adopted,
            invocations=sessions,
        )

    def _rows(self):
        # Distinct repo / session values per row so an ordering assertion can
        # key off either without ambiguity.
        return [
            self._row("b/repo", "alpha", ROLE_DEVELOPER, BACKEND_CLAUDE, 2, 1),
            self._row("a/repo", "beta", ROLE_REVIEWER, BACKEND_CODEX, 9, 9),
            self._row("c/repo", "gamma", ROLE_DEVELOPER, BACKEND_CLAUDE, 5, 0),
        ]


class _SkillPanelStreamlit:
    """Fake `st` recording the calls the skill-panel renderers make.

    Records the markdown / caption / info payloads and the expander labels,
    and hands back a null context for `container` / `expander`, so the
    render runs end-to-end without the optional Streamlit dependency.
    """

    def __init__(self, query_params=None):
        self.query_params = query_params or {}
        self.markdowns: list = []
        self.captions: list = []
        self.infos: list = []
        self.expanders: list = []

    def container(self, **kwargs):
        return _NullContext()

    def expander(self, label, **kwargs):
        self.expanders.append(label)
        return _NullContext()

    def markdown(self, html, **kwargs) -> None:
        self.markdowns.append(html)

    def caption(self, text) -> None:
        self.captions.append(text)

    def info(self, text) -> None:
        self.infos.append(text)


class SkillAdoptionRenderTest(unittest.TestCase):
    """`_render_skill_adoption` leads with the session-adoption matrix and
    only nags to enable `TRACK_SKILL_TRIGGERS` when there is genuinely no
    evidence. A present row proves tracking is on -- `sessions > 0` means
    availability was recorded, an incidental reference means the stream was
    parsed -- so a zero-adoption window with rows captions a neutral
    genuine-0% result instead. Streamlit is faked so the render runs
    end-to-end and its captions can be observed.
    """

    def _render(self, adoption_rows, *, skill_rows=None):
        _, dashboard = _reload()
        st = _SkillPanelStreamlit()
        if skill_rows is None:
            skill_rows = [self._rate_row()]
        dashboard._render_skill_adoption(
            st=st,
            skill_adoption_rows=adoption_rows,
            skill_rows=skill_rows,
            skill_matrix_rows=[],
        )
        return st

    def test_available_but_unadopted_captions_genuine_zero(self) -> None:
        # sessions > 0 proves availability was tracked, so a 0-adoption
        # window reads as a genuine 0%, never a "turn on tracking" nag.
        st = self._render([self._adopt_row(sessions=5, adopted=0)])
        self.assertEqual(len(st.captions), 1)
        caption = st.captions[0]
        self.assertIn("genuine 0% adoption", caption)
        self.assertNotIn("Enable", caption)
        self.assertNotIn("TRACK_SKILL_TRIGGERS", caption)

    def test_incidental_only_captions_neutral_not_tracking_nag(self) -> None:
        # Incidental evidence with no availability still proves the stream
        # was parsed, so the caption stays neutral rather than recommending
        # the already-on switch.
        st = self._render([self._adopt_row(sessions=0, adopted=0, incidental=1)])
        self.assertEqual(len(st.captions), 1)
        caption = st.captions[0]
        self.assertIn("incidental", caption)
        self.assertNotIn("Enable", caption)
        self.assertNotIn("TRACK_SKILL_TRIGGERS", caption)

    def test_adopted_window_has_no_caption(self) -> None:
        st = self._render([self._adopt_row(sessions=5, adopted=3)])
        self.assertEqual(st.captions, [])

    def test_empty_rows_leave_switch_hint_to_the_table(self) -> None:
        # No adoption rows -> the table itself renders the
        # `TRACK_SKILL_TRIGGERS` fallback; the panel adds no caption so the
        # switch reminder is not doubled.
        st = self._render([])
        self.assertEqual(st.captions, [])
        blob = "".join(st.markdowns)
        self.assertIn("orch-skilladopt-empty", blob)
        self.assertIn("TRACK_SKILL_TRIGGERS", blob)

    def test_no_agent_exit_rows_shows_single_info(self) -> None:
        st = self._render([], skill_rows=[])
        self.assertEqual(len(st.infos), 1)
        self.assertIn("No `agent_exit` rows", st.infos[0])

    def test_load_only_diagnostic_captions_loads_not_incidental(self) -> None:
        # sessions=0, load_rows>0, incidental=0: a session loaded a skill it
        # did not report available. The caption must name the loads (matching
        # the Invocation loads column), never "only incidental references".
        st = self._render([self._adopt_row(sessions=0, adopted=0, load_rows=2)])
        self.assertEqual(len(st.captions), 1)
        caption = st.captions[0]
        self.assertIn("loaded", caption)
        self.assertNotIn("Only incidental", caption)
        self.assertNotIn("Enable", caption)

    def test_mixed_evidence_captions_loads_and_incidental(self) -> None:
        # sessions=0 with both load and incidental evidence: the caption
        # names both so it matches the Invocation loads and Incidental
        # references columns.
        st = self._render(
            [self._adopt_row(sessions=0, adopted=0, load_rows=2, incidental=1)],
        )
        self.assertEqual(len(st.captions), 1)
        caption = st.captions[0]
        self.assertIn("loaded", caption)
        self.assertIn("incidental", caption)
        self.assertNotIn("Enable", caption)

    def test_zero_trigger_diagnostic_stays_neutral_with_adoption_evidence(
        self,
    ) -> None:
        # A window with adoption evidence (sessions>0) but no run triggering a
        # skill must not tell the operator to enable a switch the adoption
        # caption just confirmed is on -- no caption in the panel nags to
        # enable tracking, and the diagnostic reports the genuine no-trigger.
        quiet = self._quiet_rate_row()
        st = self._render(
            [self._adopt_row(sessions=5, adopted=0)], skill_rows=[quiet],
        )
        joined = " ".join(st.captions)
        self.assertNotIn("Enable", joined)
        self.assertNotIn("TRACK_SKILL_TRIGGERS", joined)
        self.assertTrue(
            any("No agent run triggered a skill" in c for c in st.captions),
        )

    def _adopt_row(self, *, sessions, adopted, load_rows=0, incidental=0):
        from orchestrator.analytics.read import SkillAdoptionRow
        return SkillAdoptionRow(
            repo="owner/repo",
            skill="develop",
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            sessions=sessions,
            adopted=adopted,
            invocations=max(sessions, 1),
            load_rows=load_rows,
            incidental=incidental,
        )

    def _rate_row(self):
        # skill_runs > 0 so the invocation-level diagnostics expander adds no
        # caption of its own -- the assertions then observe only the
        # adoption panel's own caption.
        from orchestrator.analytics.read import SkillTriggerRateRow
        return SkillTriggerRateRow(
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            runs=5,
            skill_runs=2,
            total_triggers=2,
        )

    def _quiet_rate_row(self):
        # skill_runs == 0 so the diagnostics zero-trigger caption is exercised.
        from orchestrator.analytics.read import SkillTriggerRateRow
        return SkillTriggerRateRow(
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            runs=5,
            skill_runs=0,
            total_triggers=0,
        )


class SkillTriggersCompatShimTest(unittest.TestCase):
    """`_render_skill_triggers` / `_render_skill_matrix_expander` are stable
    compatibility entry points on the `orchestrator.dashboard` facade:
    keyword-only signatures rendering the "Skill trigger rates" card and its
    fold-out matrix, reachable by name for external callers and patch points.
    """

    def test_shims_are_reexported_from_the_facade(self) -> None:
        _, dashboard = _reload()
        self.assertTrue(hasattr(dashboard, "_render_skill_triggers"))
        self.assertTrue(hasattr(dashboard, "_render_skill_matrix_expander"))

    def test_shim_signatures_preserved(self) -> None:
        import inspect

        _, dashboard = _reload()
        triggers = inspect.signature(dashboard._render_skill_triggers)
        self.assertEqual(
            list(triggers.parameters), ["st", "skill_rows", "skill_matrix_rows"],
        )
        for name in triggers.parameters:
            self.assertEqual(
                triggers.parameters[name].kind,
                inspect.Parameter.KEYWORD_ONLY,
            )
        expander = inspect.signature(dashboard._render_skill_matrix_expander)
        self.assertEqual(
            list(expander.parameters), ["st", "skill_matrix_rows"],
        )

    def test_triggers_shim_renders_trigger_rates_card(self) -> None:
        _, dashboard = _reload()
        st = _SkillPanelStreamlit()
        dashboard._render_skill_triggers(
            st=st,
            skill_rows=[self._rate_row()],
            skill_matrix_rows=[self._matrix_row()],
        )
        blob = "".join(st.markdowns)
        # The original card header, aggregate table, and fold-out matrix.
        self.assertIn("Skill trigger rates", blob)
        self.assertIn("orch-skills", blob)
        self.assertIn("orch-skillmatrix", blob)
        self.assertTrue(
            any("Per-skill trigger matrix" in label for label in st.expanders),
        )

    def test_matrix_expander_shim_opens_collapsed_matrix(self) -> None:
        _, dashboard = _reload()
        st = _SkillPanelStreamlit()
        dashboard._render_skill_matrix_expander(
            st=st, skill_matrix_rows=[self._matrix_row()],
        )
        self.assertTrue(
            any("Per-skill trigger matrix" in label for label in st.expanders),
        )
        self.assertIn("orch-skillmatrix", "".join(st.markdowns))

    def _rate_row(self):
        from orchestrator.analytics.read import SkillTriggerRateRow
        return SkillTriggerRateRow(
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            runs=4,
            skill_runs=1,
            total_triggers=1,
        )

    def _matrix_row(self):
        from orchestrator.analytics.read import SkillTriggerMatrixRow
        return SkillTriggerMatrixRow(
            repo="owner/repo",
            skill="develop",
            agent_role=ROLE_DEVELOPER,
            backend=BACKEND_CLAUDE,
            runs=4,
            skill_runs=1,
        )


class DeltaPillTest(unittest.TestCase):
    """KPI delta pills must paint cost / token increases red and
    drops green. An earlier draft mapped `invert=True && value > 0`
    to the `.down` class (green) for "Total spend" and "Total
    tokens", which painted rising cost green -- backwards for a
    cost dashboard. The fix drops `invert=True` from those KPIs so
    the default mapping (up=red, down=green) lands.
    """

    def test_positive_default_paints_up_red_arrow(self) -> None:
        _, dashboard = _reload()
        html = dashboard._delta_pill(0.25)
        self.assertIn('orch-delta up', html)
        self.assertIn('▲', html)

    def test_negative_default_paints_down_green_arrow(self) -> None:
        _, dashboard = _reload()
        html = dashboard._delta_pill(-0.25)
        self.assertIn('orch-delta down', html)
        self.assertIn('▼', html)

    def test_invert_swaps_only_color_not_arrow(self) -> None:
        # `invert=True` reserved for "up is good" KPIs (issues
        # resolved, success rate). The arrow still follows the
        # value's sign so the direction is unambiguous, but the
        # color swaps so positive growth reads as green.
        _, dashboard = _reload()
        pos = dashboard._delta_pill(0.25, invert=True)
        neg = dashboard._delta_pill(-0.25, invert=True)
        self.assertIn('orch-delta down', pos)
        self.assertIn('▲', pos)
        self.assertIn('orch-delta up', neg)
        self.assertIn('▼', neg)

    def test_none_renders_nothing(self) -> None:
        # No prior window to compare against: the grey placeholder pill
        # read like a (non-functional) minimize control, so the slot is
        # dropped entirely rather than rendering a flat dash.
        _, dashboard = _reload()
        self.assertEqual(dashboard._delta_pill(None), "")

    def test_zero_delta_renders_nothing(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard._delta_pill(0.0), "")

    def test_accepts_value_keyword_via_facade(self) -> None:
        # `_delta_pill` is re-exported through `dashboard.__all__`; `value`
        # is its historical keyword and must stay callable by name.
        _, dashboard = _reload()
        self.assertEqual(dashboard._delta_pill(value=0.0), "")
        self.assertIn("▲", dashboard._delta_pill(value=0.25))


class SparklineSvgTest(unittest.TestCase):
    def test_accepts_historical_keywords_via_facade(self) -> None:
        # `_sparkline_svg` is re-exported through `dashboard.__all__`; its
        # historical keywords are `values`, `w`, and `h`.
        _, dashboard = _reload()
        svg = dashboard._sparkline_svg(
            values=[1.0, 2.0, 3.0], color="#111", w=40, h=12
        )
        self.assertIn('width="40"', svg)
        self.assertIn('height="12"', svg)


class InsightsHtmlTest(unittest.TestCase):
    """The colored icon carries severity, so the rendered message
    no longer leads with a redundant `Warning.` / `Info.` prefix.
    """

    def test_message_renders_without_severity_lead_in(self) -> None:
        _, dashboard = _reload()
        banner = dashboard.InsightBanner(
            severity="warning",
            message="Agent failure rate >= 10% in this window.",
        )
        html = dashboard._insights_html([banner])
        # The message body lands verbatim (with HTML-escaping) and the
        # severity word is NOT prefixed.
        self.assertIn(
            "Agent failure rate &gt;= 10% in this window.",
            html,
        )
        self.assertNotIn("<strong>Warning.</strong>", html)
        # The CSS class still carries the severity so the colored
        # icon / banner background paints correctly.
        self.assertIn('orch-insight warning', html)


class BackendEfficiencyCardHtmlTest(unittest.TestCase):
    """The per-backend efficiency card is hand-rolled HTML so the
    caller can render one `st.markdown` per backend. Token totals
    include the cache band (`input + output + cache_read +
    cache_write`) and cache leverage is `cache_read / (cache_read +
    input)` -- the share of billable input served from cache.
    """

    def test_headline_and_metrics_rendered(self) -> None:
        _, dashboard = _reload()
        from orchestrator import dashboard_theme as theme
        row = self._row(
            backend=BACKEND_CLAUDE, runs=4, total_cost_usd=8.0,
            total_input_tokens=1_000_000, total_output_tokens=0,
            total_cache_read_tokens=1_000_000, total_cache_write_tokens=0,
        )
        html = dashboard._backend_efficiency_card_html(row, theme=theme)
        self.assertIn(BACKEND_CLAUDE, html)
        self.assertIn("4 runs", html)
        # tokens = 2M -> $8 / 2M = $4.00 / 1M tok.
        self.assertIn("$4.00 / 1M tok", html)
        # cache_read 1M / (input 1M + cache_read 1M) = 50% cache hit.
        self.assertIn("50% cache hit", html)
        # $8 / 4 runs = $2.00 / run.
        self.assertIn("$2.00 / run", html)

    def test_zero_tokens_and_runs_avoid_division(self) -> None:
        _, dashboard = _reload()
        from orchestrator import dashboard_theme as theme
        row = self._row(backend=BACKEND_CODEX, runs=0, total_cost_usd=0.0)
        html = dashboard._backend_efficiency_card_html(row, theme=theme)
        self.assertIn("$0.00 / 1M tok", html)
        self.assertIn("0% cache hit", html)
        self.assertIn("$0.00 / run", html)

    def test_backend_name_html_escaped(self) -> None:
        _, dashboard = _reload()
        from orchestrator import dashboard_theme as theme
        row = self._row(backend="ba<ck>", runs=1, total_cost_usd=1.0)
        html = dashboard._backend_efficiency_card_html(row, theme=theme)
        self.assertIn("ba&lt;ck&gt;", html)
        self.assertNotIn("ba<ck>", html)

    def _row(self, **kw):
        from orchestrator.analytics.read import BackendEfficiencyRow
        return BackendEfficiencyRow(**kw)


class CostCoverageBarHtmlTest(unittest.TestCase):
    """The cost-attribution coverage bar sizes segments by token share
    when the window carries token volume, falling back to run share
    only when it does not -- a few high-token runs can dominate cost
    while looking like a thin slice of the run count.
    """

    def test_segments_sized_by_token_share(self) -> None:
        _, dashboard = _reload()
        from orchestrator import dashboard_theme as theme
        # 750 / 1000 tokens = 75% by tokens, NOT 10% by run count.
        rows = [
            self._row(COST_SOURCE_REPORTED, 1, 750),
            self._row(COST_SOURCE_UNKNOWN_PRICE, 9, 250),
        ]
        html = dashboard._cost_coverage_bar_html(rows, theme=theme)
        self.assertIn("Cost attribution coverage", html)
        self.assertIn("width:75.0%", html)
        self.assertIn("25.0%", html)

    def test_falls_back_to_run_share_without_tokens(self) -> None:
        _, dashboard = _reload()
        from orchestrator import dashboard_theme as theme
        # No token volume yet -> size by run share: 3 / 4 = 75%.
        rows = [
            self._row(COST_SOURCE_REPORTED, 3, 0),
            self._row("unknown", 1, 0),
        ]
        html = dashboard._cost_coverage_bar_html(rows, theme=theme)
        self.assertIn("width:75.0%", html)

    def test_cost_source_html_escaped(self) -> None:
        _, dashboard = _reload()
        from orchestrator import dashboard_theme as theme
        rows = [self._row("src<&>", 1, 10)]
        html = dashboard._cost_coverage_bar_html(rows, theme=theme)
        self.assertIn("src&lt;&amp;&gt;", html)
        self.assertNotIn("src<&>", html)

    def _row(self, source, runs, tokens):
        from orchestrator.analytics.read import CostCoverageRow
        return CostCoverageRow(
            cost_source=source, runs=runs, total_tokens=tokens
        )


class ReliabilityTilesHtmlTest(unittest.TestCase):
    """The reliability strip renders the `(value, label, tone)` triples
    from `reliability_tile_data`; numeric values format through
    `fmt_num`, string values pass through verbatim, and the `tone`
    class paints the warn / bad tiles.
    """

    def test_tiles_carry_value_label_and_tone(self) -> None:
        _, dashboard = _reload()
        tiles = [
            (FULL_WINDOW_AGENT_RUNS, KPI_AGENT_RUNS, ""),
            ("0%", "Success rate", "bad"),
            (FULL_WINDOW_TIMEOUTS, KPI_TIMEOUTS, "bad"),
        ]
        html = dashboard._reliability_tiles_html(
            tiles, fmt_num=lambda count: f"{count}"
        )
        self.assertIn("orch-rel-tiles", html)
        self.assertIn(f">{FULL_WINDOW_AGENT_RUNS}<", html)
        self.assertIn(">0%<", html)  # string value passes through
        self.assertIn(">Timeouts<", html)
        self.assertIn("orch-rel-tile bad", html)

    def test_label_html_escaped(self) -> None:
        _, dashboard = _reload()
        tiles = [(1, "la<b>el", "")]
        html = dashboard._reliability_tiles_html(tiles, fmt_num=str)
        self.assertIn("la&lt;b&gt;el", html)
        self.assertNotIn("la<b>el", html)


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
            (window.start, window.end, CACHE_REPO, EVENT_NAMES, None,
             ISSUE_NUMBER),
        )
        prev = dashboard.previous_window(window)
        self.assertEqual(
            prev_key,
            (prev.start, prev.end, CACHE_REPO, EVENT_NAMES, None,
             ISSUE_NUMBER),
        )


class DashboardDataPrepTest(unittest.TestCase):
    """Small data-prep helpers keep `main()` focused on render sequencing."""

    def test_kpis_use_cache_tokens_and_daily_sparks(self) -> None:
        _, dashboard = _reload()
        from orchestrator import dashboard_theme as theme
        from orchestrator.analytics.read import (
            ReviewRoundBucketRow,
            ThroughputDayRow,
            TimeSeriesPoint,
        )

        summary = dashboard.Summary(
            total_cost_usd=12.0,
            total_input_tokens=10,
            total_output_tokens=20,
            total_cache_read_tokens=3,
            total_cache_write_tokens=7,
        )
        prev_summary = dashboard.Summary(
            total_cost_usd=6.0,
            total_input_tokens=5,
            total_output_tokens=5,
            total_cache_read_tokens=5,
            total_cache_write_tokens=5,
        )
        ts_points = [
            TimeSeriesPoint(
                day=MAY01, event=EVENT_AGENT_EXIT, count=1,
                cost_usd=1.5, input_tokens=10, output_tokens=5,
                cache_read_tokens=2, cache_write_tokens=3,
            ),
            TimeSeriesPoint(
                day=MAY01, event=EVENT_AGENT_EXIT, count=1,
                cost_usd=0.5, input_tokens=1, output_tokens=2,
            ),
            TimeSeriesPoint(
                day=MAY07, event=EVENT_AGENT_EXIT, count=1,
                cost_usd=4.0, input_tokens=2, output_tokens=3,
                cache_read_tokens=1, cache_write_tokens=1,
            ),
        ]
        throughput_rows = [
            ThroughputDayRow(day=MAY01, resolved=2, rejected=1),
            ThroughputDayRow(day=MAY07, resolved=0, rejected=1),
        ]
        review_round_rows = [
            ReviewRoundBucketRow(bucket=BUCKET_INITIAL, runs=2, total_cost_usd=5.0),
            ReviewRoundBucketRow(bucket=BUCKET_FIRST_ROUND, runs=1, total_cost_usd=3.0),
        ]

        kpis, resolved, rejected = dashboard._build_kpi_strip_data(
            dashboard._KpiInputs(
                theme=theme,
                summary=summary,
                prev_summary=prev_summary,
                ts_points=ts_points,
                throughput_rows=throughput_rows,
                review_round_rows=review_round_rows,
                days_in_window=2,
            )
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
            by_label["Cost / resolved issue"]["spark"], [2, 0],
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
        self.assertEqual(
            dashboard.PLOTLY_CONFIG.get("displayModeBar"), False
        )


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
            window, CACHE_REPO,
            list(EVENT_NAMES), list(STAGE_NAMES), ISSUE_NUMBER,
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
        self.assertEqual(
            key, (window.start, window.end, None, None, None, None)
        )

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


# Every read member the extraction owns (functions + the read-plan
# dataclass), each of which carries a `__module__`.
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

# The read cache / load constants re-exported alongside the members.
_READS_FACADE_CONSTANTS = (
    "DEFAULT_RECENT_AGENT_EXITS",
    "STATIC_METADATA_TTL_SECONDS",
    "LOADING_INDICATOR_MESSAGE",
)

# The moved page-pipeline members the facade re-exports (functions +
# page-state dataclasses), each of which carries a `__module__`. The
# purely internal token / layout math helpers are not re-exported and
# stay private to the widgets module.
_MOVED_WIDGET_MEMBERS = (
    "_DashboardModules",
    "_DashboardFilters",
    "_DashboardControls",
    "_DashboardPage",
    "_backend_tokens_by_day",
    "_load_dashboard_data",
    "_render_topbar_and_meta",
    "_render_first_wave",
    "_render_chart_widgets",
    "_render_remaining_widgets",
    "_render_dashboard_widgets",
    "_render_dashboard_footer",
    "_render_no_data",
    "_render_empty_window",
    "_render_hero_usage",
    "_render_stage_review_bars",
    "_render_issues_and_backends",
    "_render_repo_and_reliability",
    "_render_activity_heatmap",
    "_render_skill_adoption",
    "_render_skill_invocation_diagnostics",
    "_render_skill_triggers",
    "_render_skill_matrix_expander",
    "_render_recent_runs",
    "_render_drilldown_view",
)

# The page-level constants re-exported alongside the widget members (in
# the facade `__all__`, unlike the module-private members above).
_WIDGETS_FACADE_CONSTANTS = (
    "PLOTLY_CONFIG",
    "NO_DATA_MESSAGE",
    "EMPTY_WINDOW_MESSAGE",
)

# The inline-HTML card builders the facade re-exports from the cards leaf.
_MOVED_CARD_MEMBERS = (
    "_card_header_html",
    "_insights_html",
    "_backend_efficiency_card_html",
    "_cost_coverage_bar_html",
    "_reliability_tiles_html",
)

# The KPI-strip members the facade re-exports from the kpi-strip leaf.
_MOVED_KPI_MEMBERS = (
    "_KpiInputs",
    "_build_kpi_strip_data",
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


class WidgetRenderingExtractionTest(unittest.TestCase):
    """The widget-rendering pipeline -- the two-wave render passes, the
    empty / no-data states, the per-issue drill-down renderer, the page
    footer, and the page-state dataclasses the pipeline threads -- lives in
    `orchestrator.dashboard_widgets`, and `orchestrator.dashboard`
    re-exports the members the page pipeline and these tests reach under
    the same names so the `dashboard.<name>` surface keeps resolving to the
    same object. The KPI-strip aggregations live in
    `orchestrator.dashboard_kpi_strip` (`KpiStripExtractionTest`).
    """

    def test_widget_members_defined_in_widgets_module(self) -> None:
        _reload(CONFIGURED_DB_ENV)
        widgets = sys.modules[DASHBOARD_WIDGETS_MODULE]
        for name in _MOVED_WIDGET_MEMBERS:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(widgets, name).__module__,
                    DASHBOARD_WIDGETS_MODULE,
                )

    def test_facade_reexports_widgets_objects(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        widgets = sys.modules[DASHBOARD_WIDGETS_MODULE]
        for name in (*_MOVED_WIDGET_MEMBERS, *_WIDGETS_FACADE_CONSTANTS):
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(dashboard, name),
                    f"dashboard dropped the historical {name!r} alias",
                )
                self.assertIs(
                    getattr(dashboard, name), getattr(widgets, name)
                )


class CardHtmlExtractionTest(unittest.TestCase):
    """The insight / backend-efficiency / cost-coverage / reliability-tile
    inline-HTML card family lives in `orchestrator.dashboard_cards`, and
    `orchestrator.dashboard` re-exports each builder under the same
    name so the page pipeline and the `dashboard.<name>`
    surface keep resolving to the same object.
    """

    def test_card_members_defined_in_cards_module(self) -> None:
        _reload(CONFIGURED_DB_ENV)
        cards = sys.modules[DASHBOARD_CARDS_MODULE]
        for name in _MOVED_CARD_MEMBERS:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(cards, name).__module__,
                    DASHBOARD_CARDS_MODULE,
                )

    def test_facade_reexports_cards_objects(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        cards = sys.modules[DASHBOARD_CARDS_MODULE]
        for name in _MOVED_CARD_MEMBERS:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(dashboard, name),
                    f"dashboard dropped the historical {name!r} alias",
                )
                self.assertIs(getattr(dashboard, name), getattr(cards, name))


class KpiStripExtractionTest(unittest.TestCase):
    """The KPI-strip aggregations -- the token / throughput / rework
    helpers that turn a `Summary` aggregate plus the first-wave read rows
    into the four KPI tiles and the resolved / rejected throughput totals
    -- live in `orchestrator.dashboard_kpi_strip`. `orchestrator.dashboard`
    re-exports the two members the page pipeline and these tests reach
    (`_KpiInputs` / `_build_kpi_strip_data`) under the same names, and
    `dashboard_widgets` imports `_KpiInputs` back from the leaf.
    """

    def test_kpi_members_defined_in_kpi_strip_module(self) -> None:
        _reload(CONFIGURED_DB_ENV)
        kpi_strip = sys.modules[DASHBOARD_KPI_STRIP_MODULE]
        for name in _MOVED_KPI_MEMBERS:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(kpi_strip, name).__module__,
                    DASHBOARD_KPI_STRIP_MODULE,
                )

    def test_facade_reexports_kpi_strip_objects(self) -> None:
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        kpi_strip = sys.modules[DASHBOARD_KPI_STRIP_MODULE]
        for name in _MOVED_KPI_MEMBERS:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(dashboard, name),
                    f"dashboard dropped the historical {name!r} alias",
                )
                self.assertIs(
                    getattr(dashboard, name), getattr(kpi_strip, name)
                )

    def test_widgets_imports_kpi_inputs_from_the_leaf(self) -> None:
        _reload(CONFIGURED_DB_ENV)
        widgets = sys.modules[DASHBOARD_WIDGETS_MODULE]
        kpi_strip = sys.modules[DASHBOARD_KPI_STRIP_MODULE]
        self.assertIs(widgets._KpiInputs, kpi_strip._KpiInputs)


class _NullContext:
    """`with`-usable stand-in for `st.container(...)` / `st.columns(...)`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingStreamlit:
    """Minimal fake `st` that records the `config=` handed to `plotly_chart`.

    Only the surface `_render_activity_heatmap` touches is implemented; every
    other call is a no-op so the render runs without the optional Streamlit.
    """

    def __init__(self) -> None:
        self.plotly_configs: list = []

    def container(self, **kwargs):
        return _NullContext()

    def markdown(self, *args, **kwargs) -> None:
        """No-op stand-in for `st.markdown`."""

    def selectbox(self, *args, **kwargs) -> None:
        """No-op stand-in for `st.selectbox`."""

    def plotly_chart(self, figure, *, config=None, **kwargs) -> None:
        self.plotly_configs.append(config)


class _StubCharts:
    def hour_weekday_heatmap(self, rows, *, tz_label):
        return object()


class FacadePatchInterceptionTest(unittest.TestCase):
    """The moved page-pipeline resolves its siblings, the read-wave dispatch,
    and `PLOTLY_CONFIG` through the `orchestrator.dashboard` facade at call
    time, so `patch.object(dashboard, ...)` intercepts the running pipeline
    (mirroring the workflow.py stage-handler facade). Identity re-export alone
    would not catch a stubbed rebind, so these drive the callers under a patch.
    """

    def test_patched_run_read_waves_drives_data_load(self) -> None:
        # `_load_dashboard_data` reaches the read-wave dispatch through the
        # facade, so a patched `dashboard._run_read_waves` supplies the data.
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        read_results = {"summary": object()}
        kpis = object()
        modules = SimpleNamespace(st=object())
        page = SimpleNamespace(reads=object())
        with patch.object(
            dashboard, RUN_READ_WAVES_MEMBER, return_value=(read_results, kpis),
        ) as stub:
            loaded = dashboard._load_dashboard_data(modules, page)
            stub.assert_called_once()
        self.assertIs(loaded.read_results, read_results)
        self.assertIs(loaded.kpis, kpis)

    def test_patched_sections_drive_page_render(self) -> None:
        # `_render_dashboard_widgets` reaches both wave renderers through the
        # facade, so patched stubs run in place of the real sections.
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        calls: list[str] = []
        with patch.object(
            dashboard, "_render_chart_widgets",
            side_effect=lambda *args: calls.append("chart"),
        ), patch.object(
            dashboard, "_render_remaining_widgets",
            side_effect=lambda *args: calls.append("remaining"),
        ):
            dashboard._render_dashboard_widgets(object(), object(), object())
        self.assertEqual(calls, ["chart", "remaining"])

    def test_patched_plotly_config_reaches_chart(self) -> None:
        # A leaf renderer reads `PLOTLY_CONFIG` through the facade and hands
        # Plotly a plain-dict copy, so patching `dashboard.PLOTLY_CONFIG`
        # changes the config the chart receives (never the mapping proxy,
        # which is not JSON-serializable).
        _, dashboard = _reload(CONFIGURED_DB_ENV)
        fake_st = _RecordingStreamlit()
        sentinel = {"displayModeBar": True, "scrollZoom": True}
        with patch.object(dashboard, "PLOTLY_CONFIG", sentinel):
            dashboard._render_activity_heatmap(
                st=fake_st,
                dashboard_charts=_StubCharts(),
                heatmap_rows=[],
                tz_offset_choice=0,
            )
        self.assertEqual(fake_st.plotly_configs, [sentinel])
        self.assertIsInstance(fake_st.plotly_configs[0], dict)
        self.assertIsNot(fake_st.plotly_configs[0], sentinel)


class CachedReadConnectionScopingTest(_MainSourceTest):
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
                    " conn", signature,
                    f"{name} must not accept a `conn` argument "
                    "(it would become part of the cache key)",
                )

    def test_wrappers_forward_scoped_connection(self) -> None:
        # Cached readers delegate through `_read_filtered` to the scoped
        # connection adapter. The wrappers stay connection-free so `conn`
        # never lands in the cache key.
        readers_src = self._readers_source()
        self.assertGreaterEqual(
            readers_src.count("_read_filtered("), 16,
            "every widget read should route through `_read_filtered`",
        )
        # The two static-metadata reads route through it too.
        self.assertGreaterEqual(
            self._combined_source(STATIC_METADATA_READER_NAMES).count(
                SCOPED_READ_CALL_FRAGMENT
            ),
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
        self.assertTrue(
            hasattr(dashboard.analytics_read, "analytics_connection")
        )
        self.assertTrue(
            hasattr(
                dashboard.analytics_read, "close_thread_local_connection"
            )
        )
        # Quick smoke: the unset-URL branch yields None without
        # touching any connect factory.
        with dashboard.analytics_read.analytics_connection() as conn:
            self.assertIsNone(conn)


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
        _, dashboard = _reload(
            {ANALYTICS_DB_URL_ENV: "", PARALLEL_READS_ENV: ""}
        )
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
                self.assertTrue(
                    dashboard.dashboard_parallel_reads_enabled()
                )

    def test_falsy_spellings_keep_sequential(self) -> None:
        for sentinel in ("0", "false", "off", "no", "disabled", "none"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload(
                    {
                        ANALYTICS_DB_URL_ENV: "",
                        PARALLEL_READS_ENV: sentinel,
                    }
                )
                self.assertFalse(
                    dashboard.dashboard_parallel_reads_enabled()
                )

    def test_whitespace_is_stripped(self) -> None:
        # Operators paste env values from playbooks; tolerate leading /
        # trailing whitespace so a stray newline does not silently fall
        # back to the sequential path.
        _, dashboard = _reload(
            {ANALYTICS_DB_URL_ENV: "", PARALLEL_READS_ENV: "  on  "}
        )
        self.assertTrue(dashboard.dashboard_parallel_reads_enabled())


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


class DashboardCompatibilityHelperTest(_MainSourceTest):
    """Exported dashboard helpers retain their historical call shapes."""

    def test_topbar_signature_is_stable(self) -> None:
        import inspect

        _, dashboard = _reload(CONFIGURED_DB_ENV)
        self.assertEqual(
            tuple(inspect.signature(dashboard._topbar_html).parameters),
            (
                "extent",
                "distinct_repos",
                "total_events",
                "spend_in_range",
                "fmt_money_exact",
                "fmt_num",
            ),
        )

    def test_drilldown_signature_and_delegate_stable(self) -> None:
        import inspect

        _, dashboard = _reload(CONFIGURED_DB_ENV)
        self.assertEqual(
            tuple(inspect.signature(dashboard._render_drilldown).parameters),
            (
                "st",
                "pd",
                "window",
                "repo_filter",
                "issue_input_parsed",
                "event_filter",
                "stage_filter",
            ),
        )
        self.assertIn(
            "_render_drilldown_view(modules, filters)",
            self._source_of("_render_drilldown"),
        )


class FanOutReadsSequentialTest(unittest.TestCase):
    """The sequential branch of `_fan_out_reads` runs each reader in
    submission order on the calling thread and returns results keyed
    by reader name. The helper lets each staged wave dispatch its bound
    cached-reader tasks through one path and lets tests inject fake
    readers without booting Streamlit.
    """

    def test_results_keep_name_and_submit_order(self) -> None:
        _, dashboard = _reload()
        order: list[str] = []

        readers = [
            ("a", partial(_record_reader_call, "a", 1, order)),
            ("b", partial(_record_reader_call, "b", 2, order)),
            ("c", partial(_record_reader_call, "c", 3, order)),
        ]
        read_results = dashboard._fan_out_reads(readers, parallel=False)
        self.assertEqual(read_results, {"a": 1, "b": 2, "c": 3})
        # Sequential path runs in submission order so a deterministic
        # log line / error message references the right reader.
        self.assertEqual(order, ["a", "b", "c"])

    def test_first_failing_reader_propagates(self) -> None:
        # Sequential path stops at the first error so the caller
        # surfaces one user-friendly message instead of a stack of
        # errors.
        _, dashboard = _reload()
        from orchestrator.analytics.read import AnalyticsReadError
        called: list[str] = []

        readers = [
            ("a", partial(_record_reader_call, "ok", 1, called)),
            ("b", partial(_raise_read_error, "connection refused", called, "boom")),
            ("c", partial(_record_reader_call, "never", 2, called)),
        ]
        with self.assertRaises(AnalyticsReadError):
            dashboard._fan_out_reads(readers, parallel=False)
        self.assertEqual(called, ["ok", "boom"])

    def test_each_reader_runs_exactly_once(self) -> None:
        _, dashboard = _reload()
        counts = {"a": 0, "b": 0}

        readers = [
            ("a", partial(_increment_reader_count, "a", counts)),
            ("b", partial(_increment_reader_count, "b", counts)),
        ]
        dashboard._fan_out_reads(readers, parallel=False)
        self.assertEqual(counts, {"a": 1, "b": 1})


class FanOutReadsParallelTest(unittest.TestCase):
    """The parallel branch dispatches readers across a
    `ThreadPoolExecutor`. Each worker thread is responsible for its
    own analytics connection (the thread-local cache from #383); the
    helper itself only owns dispatch + result collection.
    """

    def test_all_results_returned_keyed_by_name(self) -> None:
        _, dashboard = _reload()

        readers = [
            (f"r{idx}", partial(_return_value, idx)) for idx in range(5)
        ]
        read_results = dashboard._fan_out_reads(
            readers, parallel=True, max_workers=4
        )
        self.assertEqual(
            read_results, {f"r{idx}": idx for idx in range(5)}
        )

    def test_each_reader_runs_once_on_worker(self) -> None:
        # Re-entrant workers must not re-submit a reader (and
        # the dispatch logic must not double-collect). The set of
        # observed thread ids should be > 1 to confirm actual
        # parallelism, but the exact count depends on scheduling so
        # we only assert it ran on a non-main thread when more than
        # one reader was submitted.
        import threading
        _, dashboard = _reload()
        calls: dict[str, int] = {}
        threads: set[int] = set()
        lock = threading.Lock()

        readers = [
            (
                f"r{idx}",
                partial(_record_threaded_reader, f"r{idx}", calls, threads, lock),
            )
            for idx in range(8)
        ]
        dashboard._fan_out_reads(
            readers, parallel=True, max_workers=4
        )
        self.assertEqual(set(calls.values()), {1})
        self.assertEqual(set(calls), {f"r{idx}" for idx in range(8)})
        self.assertNotIn(threading.get_ident(), threads)

    def test_parallel_wall_clock_beats_sequential_sum(self) -> None:
        # Smoke: with 4 workers and 4 readers each sleeping ~80 ms, the
        # wall-clock should be much closer to one reader's runtime than
        # to the sum. Pin a loose ceiling so the test is not flaky on a
        # busy CI host but still fails if the executor degenerates to
        # the sequential path.
        import time
        _, dashboard = _reload()
        delay = 0.08

        readers = [
            (f"r{idx}", partial(_sleep_then_return, delay, "ok"))
            for idx in range(4)
        ]
        t0 = time.perf_counter()
        read_results = dashboard._fan_out_reads(
            readers, parallel=True, max_workers=4
        )
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(read_results), 4)
        # Sequential sum would be 4 * delay = 320 ms; one wave on
        # four workers should land well under 2 * delay.
        self.assertLess(elapsed, delay * 2.5)

    def test_reader_exception_propagates(self) -> None:
        # `AnalyticsReadError` raised in a worker must surface from
        # the helper so the caller's `try/except AnalyticsReadError`
        # in `main()` can render a single `st.error` and stop.
        _, dashboard = _reload()
        from orchestrator.analytics.read import AnalyticsReadError

        readers = [
            ("ok", partial(_return_value, 1)),
            ("boom", partial(_raise_read_error, "query failed")),
        ]
        with self.assertRaisesRegex(AnalyticsReadError, "query failed"):
            dashboard._fan_out_reads(
                readers, parallel=True, max_workers=2
            )


class MainRenderDispatchTest(_MainSourceTest):
    """The page pipeline preserves control and widget render order."""

    def test_render_helpers_called_in_page_order(self) -> None:
        controls_src = self._source_of("_render_dashboard_controls")
        self.assertLess(
            controls_src.index("_render_sidebar_filters("),
            controls_src.index("_render_date_filter_bar("),
        )

        chart_src = self._source_of("_render_chart_widgets")
        chart_order = [
            "_render_hero_usage(",
            "_render_stage_review_bars(",
            "_render_issues_and_backends(",
            "_render_repo_and_reliability(",
            "_render_activity_heatmap(",
        ]
        chart_indexes = [chart_src.index(marker) for marker in chart_order]
        self.assertEqual(chart_indexes, sorted(chart_indexes))

        remaining_src = self._source_of("_render_remaining_widgets")
        remaining_order = [
            "_render_skill_adoption(",
            "_render_recent_runs(",
            "_render_drilldown_view(",
            "_render_dashboard_footer(",
        ]
        remaining_indexes = [
            remaining_src.index(marker) for marker in remaining_order
        ]
        self.assertEqual(remaining_indexes, sorted(remaining_indexes))

        page_src = self._source_of("_render_dashboard_widgets")
        self.assertLess(
            page_src.index("_render_chart_widgets("),
            page_src.index("_render_remaining_widgets("),
        )

    def test_read_and_error_paths_use_helpers(self) -> None:
        # `main` dispatches the staged read fan-out and the empty /
        # error rendering branches through focused helpers rather than
        # inlining the cached wrappers, the fan-out, the load log, and
        # the metadata / no-data / empty-window banners.
        for helper, marker in (
            ("_run_dashboard", "_read_static_metadata("),
            ("_render_dashboard", "_render_no_data("),
            ("_prepare_dashboard_page", "_widget_readers("),
            ("_load_dashboard_data", "_run_read_waves("),
            (RUN_READ_WAVES_MEMBER, "_dispatch_reads("),
            (RUN_READ_WAVES_MEMBER, "_log_dashboard_load("),
            (RENDER_FIRST_WAVE_MEMBER, "_render_empty_window("),
        ):
            with self.subTest(helper=helper, marker=marker):
                self.assertIn(marker, self._source_of(helper))
        # The read-error banners and the cached wrappers belong to the
        # helpers, so `main` never inlines `st.error(`, a
        # `_fan_out_reads` call, or a `_read_*` wrapper definition.
        main_src = self._main_source()
        self.assertNotIn("st.error(", main_src)
        self.assertNotIn("_fan_out_reads(", main_src)
        self.assertNotIn("def _read_summary(", main_src)


class MainParallelFanOutWiringTest(_MainSourceTest):
    """`main()` dispatches the widget reads through `_dispatch_reads`
    (which wraps `_fan_out_reads`), drives the parallel switch off the
    env-backed helper, and logs a single `dashboard.load:` INFO line
    via `_log_dashboard_load` so the A/B rollout has a measurement
    surface. Streamlit is not installed for the default `uv sync
    --locked`, so these inspect the rendered sources rather than
    driving the page under Streamlit.
    """

    def test_dispatch_wraps_fan_out_helper(self) -> None:
        # `_fan_out_reads` is the single dispatch surface; the data load
        # reaches it through `_run_read_waves` -> `_dispatch_reads`, and
        # `_load_dashboard_data` delegates the whole two-wave run to
        # `_run_read_waves`.
        self.assertIn(
            "_run_read_waves(", self._source_of("_load_dashboard_data"),
        )
        self.assertIn(
            "_dispatch_reads(", self._source_of(RUN_READ_WAVES_MEMBER),
        )
        self.assertIn(
            "_fan_out_reads(", self._source_of("_dispatch_reads"),
        )

    def test_main_drives_parallel_off_env_helper(self) -> None:
        src = self._source_of("_prepare_dashboard_page")
        # The env-backed helper is the single source of truth for the
        # flag so a test or shutdown hook can flip it without
        # rewriting `main()`.
        self.assertIn("dashboard_parallel_reads_enabled()", src)

    def test_main_emits_load_timing_log(self) -> None:
        # The instrumentation line carries total wall-clock, reader
        # count, and the parallel flag so the operator can A/B with a
        # single grep; it is emitted by `_log_dashboard_load`, which the
        # `_run_read_waves` data load calls (clocked from the
        # `perf_counter` stamped in `_prepare_dashboard_page`).
        load_src = self._source_of(RUN_READ_WAVES_MEMBER)
        prepare_src = self._source_of("_prepare_dashboard_page")
        self.assertIn("_log_dashboard_load(", load_src)
        self.assertIn("perf_counter()", prepare_src)
        self.assertIn(
            "dashboard.load:", self._source_of("_log_dashboard_load"),
        )

    def test_dispatch_catches_analytics_read_error(self) -> None:
        # The `_dispatch_reads` wave helper wraps the fan-out in a
        # `try/except AnalyticsReadError` -> one `st.error` + stop, so
        # a worker exception surfaces as one banner rather than a
        # trace. (Both waves route through this single helper; the
        # routing is pinned by `StagedRenderErrorTest`.)
        dispatch_src = self._source_of("_dispatch_reads")
        self.assertIn("analytics_read.AnalyticsReadError", dispatch_src)
        self.assertIn("st.error(", dispatch_src)
        self.assertIn("st.stop()", dispatch_src)


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
            '_widget_task(st, "skill_matrix_rows", '
            "_read_skill_trigger_matrix, key)",
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
            '_widget_task(st, "skill_adoption_rows", '
            "_read_skill_adoption, key)",
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
        self.assertEqual(dashboard.STATIC_METADATA_TTL_SECONDS, 300)

    def test_extent_reader_decorated_with_longer_ttl(self) -> None:
        src = self._metadata_source()
        marker = "read_data_extent = st.cache_data("
        self.assertIn(marker, src)
        head = src.index(marker)
        tail = src.index(")(_read_data_extent)", head)
        decorator_window = src[head:tail]
        self.assertIn("st.cache_data(", decorator_window)
        self.assertIn(
            "ttl=STATIC_METADATA_TTL_SECONDS", decorator_window
        )
        self.assertIn("show_spinner=False", decorator_window)

    def test_filter_options_use_longer_ttl(self) -> None:
        src = self._metadata_source()
        marker = "read_filter_options = st.cache_data("
        self.assertIn(marker, src)
        head = src.index(marker)
        tail = src.index(")(_read_filter_options)", head)
        decorator_window = src[head:tail]
        self.assertIn("st.cache_data(", decorator_window)
        self.assertIn(
            "ttl=STATIC_METADATA_TTL_SECONDS", decorator_window
        )
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


class StagedRenderTest(_MainSourceTest):
    """The two read waves preserve their inputs and progressive render order."""

    def test_first_wave_has_only_topbar_inputs(self) -> None:
        wave = self._first_wave_source()
        for name in FIRST_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', wave)
        for name in SECOND_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(f'"{name}"', wave)

    def test_second_wave_has_remaining_reads(self) -> None:
        wave = self._second_wave_source()
        for name in SECOND_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', wave)
        for name in FIRST_WAVE_READER_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(f'"{name}"', wave)

    def test_topbar_and_meta_render_between_waves(self) -> None:
        load_source = self._load_source()
        first = load_source.index(DISPATCH_FIRST_WAVE)
        render = load_source.index("render_first_wave(")
        second = load_source.index(DISPATCH_SECOND_WAVE)
        self.assertLess(first, render)
        self.assertLess(render, second)

        first_render_source = self._source_of(RENDER_FIRST_WAVE_MEMBER)
        self.assertIn("_render_topbar_and_meta(", first_render_source)
        self.assertIn("_kpi_strip_html(", first_render_source)
        topbar_source = self._source_of("_render_topbar_and_meta")
        self.assertIn("topbar_slot.markdown(", topbar_source)
        self.assertIn("meta_slot.markdown(", topbar_source)

    def test_inline_loading_spinner_wraps_fan_out(self) -> None:
        _, dashboard = _reload({ANALYTICS_DB_URL_ENV: ""})
        self.assertEqual(
            dashboard.LOADING_INDICATOR_MESSAGE, "Loading analytics…"
        )
        source = self._load_source()
        spinner = source.index(
            "with st.spinner(LOADING_INDICATOR_MESSAGE):"
        )
        first = source.index(DISPATCH_FIRST_WAVE)
        second = source.index(DISPATCH_SECOND_WAVE)
        self.assertLess(spinner, first)
        self.assertLess(spinner, second)

    def test_widgets_render_on_main_thread(self) -> None:
        for helper in (FIRST_WAVE_READERS_MEMBER, SECOND_WAVE_READERS_MEMBER):
            with self.subTest(helper=helper):
                wave_source = self._source_of(helper)
                self.assertIn("_widget_task(", wave_source)
                self.assertNotIn(".markdown(", wave_source)
                self.assertNotIn(".info(", wave_source)
                self.assertNotIn(".plotly_chart(", wave_source)
        task_source = self._source_of("_widget_task")
        self.assertIn("partial(cached_reader, *args)", task_source)
        self.assertNotIn(".markdown(", task_source)

    def test_empty_window_short_circuits_second_wave(self) -> None:
        load_source = self._load_source()
        first_render = load_source.index("render_first_wave(")
        second = load_source.index(DISPATCH_SECOND_WAVE)
        short_circuit = load_source[first_render:second]
        self.assertIn("if first_wave is None:", short_circuit)
        self.assertIn("return None", short_circuit)

        first_wave_source = self._source_of(RENDER_FIRST_WAVE_MEMBER)
        empty_check = first_wave_source.index("summary.total_events == 0")
        empty_render = first_wave_source.index("_render_empty_window(")
        self.assertLess(empty_check, empty_render)
        self.assertIn("return None", first_wave_source[empty_check:])

    def _first_wave_source(self) -> str:
        return self._source_of(FIRST_WAVE_READERS_MEMBER)

    def _second_wave_source(self) -> str:
        return self._source_of(SECOND_WAVE_READERS_MEMBER)

    def _load_source(self) -> str:
        return self._source_of(RUN_READ_WAVES_MEMBER)


class StagedRenderErrorTest(_MainSourceTest):
    """Both read waves share error handling and retain their render order."""

    def test_both_waves_route_through_dispatch_helper(self) -> None:
        source = self._source_of(RUN_READ_WAVES_MEMBER)
        self.assertIn(DISPATCH_FIRST_WAVE, source)
        self.assertIn(DISPATCH_SECOND_WAVE, source)
        self.assertEqual(source.count("_dispatch_reads("), 2)

    def test_second_wave_error_after_topbar_paints(self) -> None:
        source = self._source_of(RUN_READ_WAVES_MEMBER)
        first_wave_render = source.index("render_first_wave(")
        second_dispatch = source.index(DISPATCH_SECOND_WAVE)
        self.assertLess(first_wave_render, second_dispatch)
        self.assertIn(
            "_render_topbar_and_meta(",
            self._source_of(RENDER_FIRST_WAVE_MEMBER),
        )


class FanOutReadsErrorPropagationTest(unittest.TestCase):
    """The first-wave error path must NOT swallow the worker's
    `AnalyticsReadError` -- the existing fan-out helper already
    propagates the exception, but the staged-render refactor adds a
    second call site, so re-pin the propagation shape for both
    branches of `_fan_out_reads`.
    """

    def test_sequential_propagates_in_staged_call(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import AnalyticsReadError

        with self.assertRaisesRegex(AnalyticsReadError, "first wave dead"):
            dashboard._fan_out_reads(
                [("summary", partial(_raise_read_error, "first wave dead"))],
                parallel=False,
            )

    def test_parallel_propagates_in_staged_call(self) -> None:
        _, dashboard = _reload()
        from orchestrator.analytics.read import AnalyticsReadError

        with self.assertRaisesRegex(AnalyticsReadError, "second wave dead"):
            dashboard._fan_out_reads(
                [("repo_rows", partial(_raise_read_error, "second wave dead"))],
                parallel=True,
                max_workers=2,
            )


class FormatTzOffsetTest(unittest.TestCase):
    """`format_tz_offset` renders the integer offset for the sidebar
    label and the heatmap subtitle."""

    def test_zero_is_utc(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.format_tz_offset(0), "UTC")

    def test_positive_offset(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.format_tz_offset(7), "UTC+7")

    def test_negative_offset(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.format_tz_offset(-5), "UTC-5")

    def test_default_offset_is_plus_seven(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.DEFAULT_TZ_OFFSET_HOURS, 7)
        self.assertIn(dashboard.DEFAULT_TZ_OFFSET_HOURS, dashboard.TZ_OFFSET_OPTIONS)


class ShiftTsTest(unittest.TestCase):
    """`shift_ts` converts a UTC `ts` to the wall-clock of the
    selected offset for display in the "Recent agent runs" table."""

    def test_none_passes_through(self) -> None:
        from datetime import timedelta
        _, dashboard = _reload()
        self.assertIsNone(dashboard.shift_ts(None, timedelta(hours=7)))

    def test_aware_ts_converted_to_offset(self) -> None:
        from datetime import timedelta
        _, dashboard = _reload()
        ts = JUN05_NOON_UTC
        shifted = dashboard.shift_ts(ts, timedelta(hours=7))
        self.assertEqual(shifted.hour, 19)
        self.assertEqual(shifted.utcoffset(), timedelta(hours=7))

    def test_aware_ts_negative_offset(self) -> None:
        from datetime import timedelta
        _, dashboard = _reload()
        ts = JUN05_NOON_UTC
        shifted = dashboard.shift_ts(ts, timedelta(hours=-5))
        self.assertEqual(shifted.hour, 7)
        self.assertEqual(shifted.utcoffset(), timedelta(hours=-5))

    def test_naive_ts_shifted_in_place(self) -> None:
        from datetime import timedelta
        _, dashboard = _reload()
        ts = JUN05_NOON_NAIVE
        shifted = dashboard.shift_ts(ts, timedelta(hours=7))
        self.assertEqual(shifted, JUN05_NOON_NAIVE.replace(hour=19))


if __name__ == "__main__":
    unittest.main()
