# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Repository and backend values shared by workflow tests."""
from pathlib import Path

from orchestrator import config


TEST_REPO_SLUG = "geserdugarov/agent-orchestrator"
TEST_BASE_BRANCH = "main"

STATE_CLOSED = "closed"
STATE_OPEN = "open"

BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"

_FAKE_WT = Path("/tmp/orchestrator-test-wt-doesnt-matter")
_TEST_SPEC = config.RepoSpec(
    slug=TEST_REPO_SLUG,
    target_root=Path("/tmp/orchestrator-test-target-root"),
    base_branch=TEST_BASE_BRANCH,
)
