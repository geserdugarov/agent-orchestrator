# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git transport helpers shared by workflow hardening tests."""
from __future__ import annotations

import contextlib
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


@contextlib.contextmanager
def _temp_git_repo_with_local_config(pairs):
    """Yield a temporary repository carrying the requested local config."""
    with tempfile.TemporaryDirectory(prefix="orch-transport-test-") as temp_dir:
        subprocess.run(
            ["git", "init", "-q", temp_dir],
            check=True,
            capture_output=True,
        )
        for key, config_value in pairs:
            subprocess.run(
                ["git", "config", "--local", key, config_value],
                cwd=temp_dir,
                check=True,
                capture_output=True,
            )
        yield Path(temp_dir)


class _GitRunRecorder:
    """Record the token-bearing git invocation behind a config probe."""

    def __init__(self, *, probe_result=None, command_result=None):
        self.probe_result = probe_result
        self.command_result = command_result or MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )
        self.calls = []
        self.args = None
        self.env = None
        self.cwd = None

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if (
            self.probe_result is not None
            and args
            and args[:3] == ["git", "config", "--get-regexp"]
        ):
            return self.probe_result
        self.args = args
        self.env = kwargs.get("env")
        self.cwd = kwargs.get("cwd")
        return self.command_result


class _TokenResolver:
    """Return a slug-derived token while retaining the requested slugs."""

    def __init__(self):
        self.slugs = []

    def __call__(self, slug: str) -> str:
        self.slugs.append(slug)
        return f"ghp-token-for-{slug.replace('/', '-')}"
