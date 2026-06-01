# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Tests for the non-Streamlit logic in `orchestrator.dashboard`.

The Streamlit and pandas imports inside `dashboard.main` are
deliberately lazy so the orchestrator polling tick never pulls them
in. These tests exercise the pure helpers (date window math, the
disabled-DB banner, the issue-number drill-down parser) and assert
the lazy-import invariant -- the module must load even when
`streamlit` is not on the install path. That way the suite stays
hermetic regardless of which dependency group an operator synced.

The module-reload pattern mirrors `tests/test_analytics_read.py`:
re-import under a hermetic env so the dashboard's `from orchestrator
import analytics` picks up the patched `ANALYTICS_DB_URL` instead of
whatever the earlier test-session import left cached.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch


def _hermetic_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
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
    """
    with patch.dict(os.environ, _hermetic_env(env), clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.analytics.read", None)
        sys.modules.pop("orchestrator.analytics", None)
        sys.modules.pop("orchestrator.dashboard", None)
        import orchestrator.analytics as analytics
        import orchestrator.dashboard as dashboard
        return analytics, dashboard


class DefaultDateRangeTest(unittest.TestCase):

    def test_default_window_covers_n_days_including_today(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=date(2026, 5, 28), days=7
        )
        self.assertEqual(end, date(2026, 5, 28))
        self.assertEqual(start, date(2026, 5, 22))

    def test_days_one_yields_today_only(self) -> None:
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=date(2026, 5, 28), days=1
        )
        self.assertEqual(start, end)

    def test_days_zero_clamps_to_today_only(self) -> None:
        # `days=0` is non-sensical (an empty window) so the helper
        # clamps to "today only" instead of returning end < start.
        _, dashboard = _reload()
        start, end = dashboard.default_date_range(
            today=date(2026, 5, 28), days=0
        )
        self.assertEqual(start, date(2026, 5, 28))
        self.assertEqual(end, date(2026, 5, 28))


class ToWindowTest(unittest.TestCase):

    def test_inclusive_end_becomes_exclusive_midnight(self) -> None:
        # `analytics_read` uses `ts < end`; midnight on the day after
        # `end_date` is what makes events from `end_date` visible.
        _, dashboard = _reload()
        window = dashboard.to_window(date(2026, 5, 1), date(2026, 5, 3))
        self.assertEqual(
            window.start, datetime(2026, 5, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(
            window.end, datetime(2026, 5, 4, tzinfo=timezone.utc)
        )

    def test_reversed_range_is_swapped(self) -> None:
        # The Streamlit two-date input lets the user type end < start.
        # Swapping silently keeps the dashboard useful instead of
        # collapsing to an empty SQL window.
        _, dashboard = _reload()
        window = dashboard.to_window(date(2026, 5, 5), date(2026, 5, 1))
        self.assertEqual(window.start.date(), date(2026, 5, 1))
        self.assertEqual(window.end.date(), date(2026, 5, 6))

    def test_single_day_window(self) -> None:
        _, dashboard = _reload()
        window = dashboard.to_window(date(2026, 5, 1), date(2026, 5, 1))
        self.assertEqual(
            window.start, datetime(2026, 5, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(
            window.end, datetime(2026, 5, 2, tzinfo=timezone.utc)
        )


class ParseIssueNumberTest(unittest.TestCase):

    def test_bare_int(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number("42"), 42)

    def test_hash_prefix_and_whitespace(self) -> None:
        _, dashboard = _reload()
        self.assertEqual(dashboard.parse_issue_number(" #42 "), 42)
        self.assertEqual(dashboard.parse_issue_number("# 42"), 42)

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
        _, dashboard = _reload({"ANALYTICS_DB_URL": ""})
        self.assertEqual(
            dashboard.db_unconfigured_message(),
            dashboard.UNCONFIGURED_DB_MESSAGE,
        )

    def test_disable_sentinel_returns_message(self) -> None:
        # Each of the documented disable sentinels collapses to None
        # inside `config`, so the helper should treat them the same.
        for sentinel in ("off", "disabled", "none", "OFF", "Disabled"):
            with self.subTest(sentinel=sentinel):
                _, dashboard = _reload({"ANALYTICS_DB_URL": sentinel})
                self.assertEqual(
                    dashboard.db_unconfigured_message(),
                    dashboard.UNCONFIGURED_DB_MESSAGE,
                )

    def test_configured_url_returns_none(self) -> None:
        _, dashboard = _reload(
            {"ANALYTICS_DB_URL": "postgresql://h/db"}
        )
        self.assertIsNone(dashboard.db_unconfigured_message())


class LazyImportTest(unittest.TestCase):
    """The dashboard module must load without importing `streamlit`.

    The polling tick loads `orchestrator.*` modules at process start;
    if `dashboard.py` were to import Streamlit at module top, every
    orchestrator deployment would have to install the dashboard
    group. Lazy import inside `main()` is the boundary; this test is
    the guardrail.
    """

    def test_streamlit_absent_from_sys_modules_after_load(self) -> None:
        with patch.dict(os.environ, _hermetic_env(), clear=True):
            sys.modules.pop("orchestrator.config", None)
            sys.modules.pop("orchestrator.analytics.read", None)
            sys.modules.pop("orchestrator.analytics", None)
            sys.modules.pop("orchestrator.dashboard", None)
            sys.modules.pop("streamlit", None)
            sys.modules.pop("pandas", None)
            import orchestrator.dashboard  # noqa: F401
            self.assertNotIn("streamlit", sys.modules)
            self.assertNotIn("pandas", sys.modules)


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
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        dashboard_path = repo_root / "orchestrator" / "dashboard.py"
        script_dir = dashboard_path.parent

        original_path = list(sys.path)
        # Snapshot the `orchestrator.*` modules so a successful
        # re-import inside `runpy` does not poison the rest of the
        # test session with a half-initialised package.
        saved_modules = {
            k: v for k, v in sys.modules.items()
            if k == "orchestrator" or k.startswith("orchestrator.")
        }
        try:
            # Match Streamlit's launch shape: only the script's
            # directory is on sys.path, the repo root is not.
            resolved_root = repo_root.resolve()
            sys.path[:] = [
                p for p in sys.path
                if not p or Path(p).resolve() != resolved_root
            ]
            sys.path.insert(0, str(script_dir))
            for k in list(sys.modules):
                if k == "orchestrator" or k.startswith("orchestrator."):
                    del sys.modules[k]

            # `run_name="not_main"` keeps the `if __name__ == "__main__":`
            # block from firing, so the test does not require Streamlit
            # to be installed -- only the top-level imports must
            # succeed under the script-launch sys.path.
            namespace = runpy.run_path(
                str(dashboard_path), run_name="not_main"
            )
            self.assertIn("main", namespace)
            self.assertIn("analytics_read", namespace)
        finally:
            sys.path[:] = original_path
            for k in list(sys.modules):
                if k == "orchestrator" or k.startswith("orchestrator."):
                    del sys.modules[k]
            sys.modules.update(saved_modules)


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
        result = dashboard.resolve_stage_filter(
            selected=["implementing", "validating"],
            available=("implementing", "validating"),
        )
        self.assertIsNone(result)

    def test_no_available_options_returns_none(self) -> None:
        # Empty filter options (DB is empty or has no non-null
        # stages yet) collapses to `None` so the read-model query
        # runs unconstrained on the stage column.
        _, dashboard = _reload()
        result = dashboard.resolve_stage_filter(
            selected=[], available=()
        )
        self.assertIsNone(result)

    def test_cleared_multiselect_returns_empty_list(self) -> None:
        # Options exist but the operator cleared the selection.
        # The read model encodes `[]` as a tautologically-false
        # predicate; without this branch the cleared state would
        # be indistinguishable from the all-selected default.
        _, dashboard = _reload()
        result = dashboard.resolve_stage_filter(
            selected=[],
            available=("implementing", "validating"),
        )
        self.assertEqual(result, [])

    def test_proper_subset_passes_through(self) -> None:
        _, dashboard = _reload()
        result = dashboard.resolve_stage_filter(
            selected=["implementing"],
            available=("implementing", "validating"),
        )
        self.assertEqual(result, ["implementing"])


if __name__ == "__main__":
    unittest.main()
