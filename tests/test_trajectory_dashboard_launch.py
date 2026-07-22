# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Trajectory dashboard lazy-import and direct-script launch tests."""

import importlib


import runpy


import sys


import tempfile


import unittest


from pathlib import Path

from tests.script_launch_helpers import (
    clear_modules as _drop_modules,
    drop_repo_root as _strip_repo_root,
    arm_launch_cleanup as _arm_launch_cleanup,
)


_ORCH = "orchestrator"


_ORCH_PREFIX = "orchestrator."


_SCRIPT_LAUNCH = "script_launch"


_DASHBOARD_MODULE = "orchestrator.trajectory_dashboard"


_SCRIPT_LAUNCH_MODULE = "orchestrator.script_launch"


_READER_MODULE = "orchestrator.trajectory_reader"


def _is_orchestrator_module(name):
    return name == _ORCH or name.startswith(_ORCH_PREFIX)


def _is_orch_or_script_launch(name):
    return _is_orchestrator_module(name) or name == _SCRIPT_LAUNCH


def _is_stray_launch_module(name):
    return name in (_DASHBOARD_MODULE, _SCRIPT_LAUNCH_MODULE, _SCRIPT_LAUNCH)


class LazyImportTest(unittest.TestCase):
    """The page module must load without importing `streamlit`,
    `pandas`, or `plotly` -- the same boundary `orchestrator.dashboard`
    holds so the polling tick never needs the dashboard group.
    """

    def test_dashboard_only_modules_absent_after_load(self) -> None:
        for mod in (
            "orchestrator.trajectory_dashboard",
            "streamlit",
            "pandas",
            "plotly",
        ):
            sys.modules.pop(mod, None)
        # `import_module` re-executes off the popped `sys.modules`, so the
        # load is real; a `from orchestrator import ...` could bind a stale
        # package attribute and pass without importing the module at all.
        importlib.import_module(_DASHBOARD_MODULE)
        self.assertNotIn("streamlit", sys.modules)
        self.assertNotIn("pandas", sys.modules)
        self.assertNotIn("plotly", sys.modules)


class ScriptPathLaunchTest(unittest.TestCase):
    """Guard `streamlit run orchestrator/trajectory_dashboard.py`.

    Streamlit executes the file as a top-level script via `runpy` with
    only the *script's* directory on `sys.path` (not the repo root), so a
    naked relative import or a bare absolute import without the sys.path
    shim raises `ImportError` before any Streamlit code can render. We
    reproduce that launch shape here without pulling Streamlit in (the
    dashboard group is opt-in): strip the repo root, insert the script's
    dir, then `runpy` the file with a non-`__main__` run name so `main()`
    is not invoked.
    """

    def test_runs_without_repo_root_on_syspath(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        script = repo_root / _ORCH / "trajectory_dashboard.py"

        _arm_launch_cleanup(self, _is_orchestrator_module)

        _strip_repo_root(repo_root)
        sys.path.insert(0, str(script.parent))
        _drop_modules(_is_orchestrator_module)

        namespace = runpy.run_path(str(script), run_name="not_main")
        self.assertIn("main", namespace)
        self.assertIn("trajectory_reader", namespace)

    def test_stale_parent_cannot_shadow_repo(self) -> None:
        # With only `orchestrator/` on `sys.path`, importing `orchestrator.<x>`
        # before the shim prepends the repo root would bind the parent
        # `orchestrator` package to whatever stale copy is importable and route
        # every later absolute import through it. The shim adds the repo root
        # without importing `orchestrator.*` first, so the real package
        # resolves even with a decoy parent behind the script dir on the path.
        repo_root = Path(__file__).resolve().parent.parent
        script = repo_root / _ORCH / "trajectory_dashboard.py"

        _arm_launch_cleanup(self, _is_orch_or_script_launch)
        with tempfile.TemporaryDirectory() as decoy_root:
            # A bare `orchestrator` package with none of the real submodules,
            # standing in for a stale install that shadows the repo root.
            decoy_pkg = Path(decoy_root) / _ORCH
            decoy_pkg.mkdir()
            (decoy_pkg / "__init__.py").write_text("")
            _strip_repo_root(repo_root)
            sys.path.insert(0, decoy_root)
            sys.path.insert(0, str(script.parent))
            _drop_modules(_is_orch_or_script_launch)

            namespace = runpy.run_path(str(script), run_name="not_main")
            self.assertIn("main", namespace)
            # The real reader landed -- not the decoy package (which has no
            # `trajectory_reader` submodule and would raise on import).
            self.assertEqual(
                namespace["trajectory_reader"].__name__,
                _READER_MODULE,
            )

    def test_package_import_ignores_stray_script(self) -> None:
        # A package import (`import orchestrator.trajectory_dashboard`) must
        # resolve the shim via `orchestrator.script_launch`, never a bare
        # `import script_launch`. An unrelated top-level `script_launch.py`
        # earlier on `sys.path` would otherwise shadow the helper or fail the
        # import outright, so the package path must not probe the bare name.
        _arm_launch_cleanup(self, _is_stray_launch_module)
        with tempfile.TemporaryDirectory() as stray_dir:
            # A stray top-level `script_launch` that detonates on import, so a
            # bare `import script_launch` during the package import would fail
            # loudly instead of silently binding the wrong helper.
            (Path(stray_dir) / "script_launch.py").write_text(
                "raise RuntimeError('stray script_launch must not be imported')\n"
            )
            sys.path.insert(0, stray_dir)
            _drop_modules(_is_stray_launch_module)
            module = importlib.import_module(_DASHBOARD_MODULE)
            self.assertTrue(hasattr(module, "main"))
            # The package path used `orchestrator.script_launch` and never
            # probed the bare name, so the stray stayed unimported.
            self.assertNotIn(_SCRIPT_LAUNCH, sys.modules)
