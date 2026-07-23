# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from orchestrator import config, workflow
from orchestrator.github import PinnedState

BASE_BRANCH = "main"
MIGRATION_REPO_SLUG = "geserdugarov/agent-orchestrator"
MIGRATION_TARGET_ROOT = Path("/tmp/x")
ALICE_REPO_SLUG = "alice/repo"
LOCK_SUFFIX_SLUG = "owner/foo.lock"
DOUBLE_DOT_SLUG = "owner/foo..bar"
BRANCH_KEY = "branch"
LEGACY_BRANCH = "orchestrator/issue-7"
NAMESPACED_BRANCH = "orchestrator/geserdugarov__agent-orchestrator/issue-7"
STAGE_LAYOUT_ISSUE_NUMBER = 11
SHARED_BRANCH_ISSUE_NUMBER = 15
PR_NUMBER = 42


def _spec(repo_slug: str) -> config.RepoSpec:
    return config.RepoSpec(
        slug=repo_slug,
        target_root=Path(f"/tmp/{workflow._sanitize_slug(repo_slug)}-target"),
        base_branch=BASE_BRANCH,
    )


def _branch(repo_slug: str, issue_number: int = 1) -> str:
    return workflow._branch_name(_spec(repo_slug), issue_number)


def _migration_spec() -> config.RepoSpec:
    return config.RepoSpec(
        slug=MIGRATION_REPO_SLUG,
        target_root=MIGRATION_TARGET_ROOT,
        base_branch=BASE_BRANCH,
    )


def _state(state_data=None) -> PinnedState:
    return PinnedState(comment_id=None, data=dict(state_data or {}))
