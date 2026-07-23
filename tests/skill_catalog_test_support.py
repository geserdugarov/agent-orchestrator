# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Shared builders and projections for skill-catalog tests."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import analytics, config


_TEST_REPO_SLUG = "geserdugarov/agent-orchestrator"
_TEST_BASE_BRANCH = "main"
_TEST_REMOTE_NAME = "origin"


def _spec(
    *,
    slug: str = _TEST_REPO_SLUG,
    target_root: str = "/tmp/orchestrator-skill-catalog-target",
    base_branch: str = _TEST_BASE_BRANCH,
    remote_name: str = _TEST_REMOTE_NAME,
) -> config.RepoSpec:
    return config.RepoSpec(
        slug=slug,
        target_root=Path(target_root),
        base_branch=base_branch,
        remote_name=remote_name,
    )


def _completed(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
):
    return subprocess.CompletedProcess(
        args=["git", "ls-tree"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _capture_analytics_records(
    case: unittest.TestCase,
) -> list[dict]:
    captured: list[dict] = []
    patcher = patch.object(analytics, "append_record", captured.append)
    patcher.start()
    case.addCleanup(patcher.stop)
    return captured


def _catalog_identity(record: dict) -> tuple:
    return (
        record["event"],
        record["issue"],
        record["repo"],
        record["base_branch"],
        record["remote_name"],
    )


def _make_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "# skill\n",
        encoding="utf-8",
    )
