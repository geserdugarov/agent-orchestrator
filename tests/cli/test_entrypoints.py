# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Launch-form coverage for the console script and module entry point."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
import unittest
from importlib import import_module
from pathlib import Path
from typing import Optional
from unittest.mock import call, patch

from orchestrator import cli

_LaunchForm = tuple[str, Optional[list[str]]]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = "orchestrator"
_RUNTIME_MODULE = "orchestrator.main"
_CONSOLE_SCRIPT = "agent-orchestrator"
_ENTRY_POINT = "orchestrator.cli:main"
_HELP_FLAG = "--help"
_ONCE_FLAG = "--once"
_RUNTIME_EXIT_CODE = 7
_HELP_TIMEOUT_SECONDS = 60
_MISSING_SCRIPT_REASON = f"{_CONSOLE_SCRIPT} is not installed; run `uv sync`"


def _console_script() -> Optional[str]:
    return shutil.which(
        _CONSOLE_SCRIPT,
        path=str(Path(sys.executable).parent),
    )


def _launch_forms() -> tuple[_LaunchForm, ...]:
    console_script = _console_script()
    return (
        (_CONSOLE_SCRIPT, [console_script] if console_script else None),
        (f"python -m {_PACKAGE}", [sys.executable, "-m", _PACKAGE]),
        (f"python -m {_RUNTIME_MODULE}", [sys.executable, "-m", _RUNTIME_MODULE]),
    )


def _run_help(command: list[str]) -> subprocess.CompletedProcess:
    # `orchestrator.config` resolves `.env` at import, so pin the documented
    # opt-out to keep the subprocess independent of the operator's file.
    return subprocess.run(
        [*command, _HELP_FLAG],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=_HELP_TIMEOUT_SECONDS,
        env={**os.environ, "ORCHESTRATOR_SKIP_DOTENV": "1"},
    )


class CliDelegationTest(unittest.TestCase):
    """`orchestrator.cli.main` is the console-script target and forwards
    both its argv and the runtime's exit code unchanged. It resolves
    `orchestrator.main.main` at call time, so the polling loop keeps a
    single process-wide patch surface.
    """

    def test_delegates_argv_and_exit_code_to_runtime(self) -> None:
        runtime = import_module(_RUNTIME_MODULE)

        with patch.object(runtime, "main", return_value=_RUNTIME_EXIT_CODE) as runtime_main:
            exit_codes = [cli.main([_ONCE_FLAG]), cli.main()]
            forwarded_argv = list(runtime_main.call_args_list)

        self.assertEqual(exit_codes, [_RUNTIME_EXIT_CODE, _RUNTIME_EXIT_CODE])
        self.assertEqual(forwarded_argv, [call([_ONCE_FLAG]), call(None)])


class ConsoleScriptRegistrationTest(unittest.TestCase):
    """The `agent-orchestrator` console script is the canonical launch
    command, so its declared target has to keep resolving to the CLI's
    `main` even when the project is not installed into the environment.
    """

    def test_declared_target_resolves_to_cli_main(self) -> None:
        manifest = tomllib.loads(
            (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )
        declared_target = manifest["project"]["scripts"][_CONSOLE_SCRIPT]
        module_name, attribute_name = declared_target.split(":")

        self.assertEqual(declared_target, _ENTRY_POINT)
        self.assertIs(
            getattr(import_module(module_name), attribute_name),
            cli.main,
        )


class LaunchFormHelpTest(unittest.TestCase):
    """Every supported launch form reaches the same argument parser: the
    console script, the package module form, and the retained
    `orchestrator.main` module form.
    """

    def test_launch_forms_print_usage(self) -> None:
        for form_name, command in _launch_forms():
            with self.subTest(form=form_name):
                if command is None:
                    self.skipTest(_MISSING_SCRIPT_REASON)
                completed = _run_help(command)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(_ONCE_FLAG, completed.stdout)


if __name__ == "__main__":
    unittest.main()
