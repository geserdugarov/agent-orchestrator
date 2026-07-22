# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree cleanup."""
from __future__ import annotations

from orchestrator import _worktree_lifecycle_state as _state
from orchestrator import worktree_lifecycle as _owner

config = _owner.config
log = _state.log


def _run_issue_worktree_removal(
    spec: config.RepoSpec, issue_number: int, log_prefix: str,
) -> None:
    """Force-remove one issue worktree under the parent lock, logging a
    non-zero git result."""
    worktree = _owner._worktree_path(spec, issue_number)
    if not worktree.exists():
        return
    with _owner._target_root_lock(spec.target_root):
        remove_result = _owner._git(
            "worktree", "remove", "--force", str(worktree),
            cwd=spec.target_root,
        )
    if remove_result.returncode != 0:
        log.warning(
            "issue=#%d %sworktree remove failed: %s",
            issue_number,
            log_prefix,
            (remove_result.stderr or "").strip(),
        )


def _remove_issue_worktree(
    spec: config.RepoSpec, issue_number: int, *, log_prefix: str = "",
) -> None:
    """Best-effort removal of one issue worktree under the parent lock."""
    try:
        _owner._run_issue_worktree_removal(spec, issue_number, log_prefix)
    except Exception:
        log.exception(
            "issue=#%d %sworktree remove raised", issue_number, log_prefix,
        )


def _run_local_branch_deletion(
    spec: config.RepoSpec, issue_number: int, branch: str, log_prefix: str,
) -> None:
    """Delete one local issue branch under the parent lock (no-op when the
    branch is absent), logging a non-zero git result."""
    with _owner._target_root_lock(spec.target_root):
        have_local = _owner._git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
            cwd=spec.target_root,
        ).returncode == 0
        if not have_local:
            return
        delete_result = _owner._git(
            "branch", "-D", branch, cwd=spec.target_root,
        )
    if delete_result.returncode != 0:
        log.warning(
            "issue=#%d %slocal branch %r delete failed: %s",
            issue_number,
            log_prefix,
            branch,
            (delete_result.stderr or "").strip(),
        )


def _delete_local_issue_branch(
    spec: config.RepoSpec,
    issue_number: int,
    branch: str,
    *,
    log_prefix: str = "",
) -> None:
    """Best-effort deletion of one local issue branch under the parent lock."""
    try:
        _owner._run_local_branch_deletion(spec, issue_number, branch, log_prefix)
    except Exception:
        log.exception(
            "issue=#%d %slocal branch %r delete raised",
            issue_number,
            log_prefix,
            branch,
        )
