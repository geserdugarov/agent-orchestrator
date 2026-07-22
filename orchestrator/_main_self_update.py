# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git probes for detecting self-modifying upstream merges."""
from __future__ import annotations

import subprocess
import sys
from types import ModuleType
from typing import Optional


def _main_module() -> ModuleType:
    return sys.modules["orchestrator.main"]


def git(*args: str) -> subprocess.CompletedProcess:
    """Run a captured git command against the orchestrator checkout."""
    config = _main_module().config
    return subprocess.run(
        ["git", *args],
        cwd=str(config.REPO_ROOT),
        capture_output=True,
        text=True,
    )


def own_head_sha() -> Optional[str]:
    """Return the orchestrator checkout's HEAD when resolvable."""
    head_revision = git("rev-parse", "HEAD")
    return (
        head_revision.stdout.strip()
        if head_revision.returncode == 0
        else None
    )


def self_modifying_merge_happened(start_sha: str) -> bool:
    """Detect a forward upstream move that touched runtime source files."""
    config = _main_module().config
    git("fetch", "--quiet", "origin", config.ORCHESTRATOR_BASE_BRANCH)
    current_sha = git(
        "rev-parse",
        f"origin/{config.ORCHESTRATOR_BASE_BRANCH}",
    ).stdout.strip()
    if not current_sha or current_sha == start_sha:
        return False
    if git(
        "merge-base",
        "--is-ancestor",
        start_sha,
        current_sha,
    ).returncode != 0:
        return False
    changed_paths = git(
        "diff",
        "--name-only",
        start_sha,
        current_sha,
    ).stdout
    return any(
        changed_path.startswith("orchestrator/")
        for changed_path in changed_paths.splitlines()
    )
