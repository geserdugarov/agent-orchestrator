# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Dashboard lazy-import and direct-script launch tests."""

import importlib


import os


import runpy


import sys


import tempfile


import unittest


from pathlib import Path


from types import SimpleNamespace


from unittest.mock import patch

from tests.dashboard_reload_helpers import (
    hermetic_environment as _hermetic_env,
)

from tests.script_launch_helpers import (
    clear_modules as _clear_modules,
    drop_repo_root as _drop_repo_root_from_sys_path,
    script_launch_sandbox as _script_launch_sandbox,
)


SKIP_DOTENV_ENV = "ORCHESTRATOR_SKIP_DOTENV"


TOKEN_FILE_ENV = "ORCHESTRATOR_TOKEN_FILE"


MISSING_TOKEN_FILE = "/tmp/agent-orchestrator-token-missing"


ORCHESTRATOR_PKG = "orchestrator"


ANALYTICS_READ_MODULE = "orchestrator.analytics.read"


DASHBOARD_MODULE = "orchestrator.dashboard"


DASHBOARD_CHARTS_MODULE = "orchestrator.dashboard_charts"


ENTRYPOINT_ATTR = "main"


def _is_orchestrator_module(name: str) -> bool:
    return name == ORCHESTRATOR_PKG or name.startswith("orchestrator.")


def _is_orchestrator_launch_module(name: str) -> bool:
    return _is_orchestrator_module(name) or name == "script_launch"


def _is_dashboard_script_launch_module(name: str) -> bool:
    return name in (
        DASHBOARD_MODULE,
        "orchestrator.script_launch",
        "script_launch",
    )


def _dashboard_launch_paths() -> SimpleNamespace:
    repo_root = Path(__file__).resolve().parent.parent
    dashboard_path = repo_root / ORCHESTRATOR_PKG / "dashboard.py"
    return SimpleNamespace(
        repo_root=repo_root,
        dashboard_path=dashboard_path,
        script_dir=dashboard_path.parent,
    )


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
                ANALYTICS_READ_MODULE,
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
            namespace = runpy.run_path(str(launch.dashboard_path), run_name="not_main")
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

            namespace = runpy.run_path(str(launch.dashboard_path), run_name="not_main")
            self.assertIn(ENTRYPOINT_ATTR, namespace)
            # The real read model landed -- not the decoy package (which
            # has no `analytics` submodule and would raise on import).
            self.assertEqual(
                namespace["analytics_read"].__name__,
                ANALYTICS_READ_MODULE,
            )

    def test_package_import_ignores_stray_script(self) -> None:
        # A normal package import (`import orchestrator.dashboard`) must
        # resolve the shim via `orchestrator.script_launch`, never a bare
        # `import script_launch`. An unrelated top-level `script_launch.py`
        # earlier on `sys.path` would otherwise shadow the helper or fail the
        # import outright, so the package path must not probe the bare name.
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
