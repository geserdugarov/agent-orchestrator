# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree decomposition."""
from __future__ import annotations

from orchestrator import _worktree_lifecycle_state as _state
from orchestrator import worktree_lifecycle as _owner

Path = _owner.Path
config = _owner.config
log = _state.log


def _decompose_worktree_path(spec: config.RepoSpec, issue_number: int) -> Path:
    return _owner._repo_worktrees_root(spec) / f"decompose-{issue_number}"


def _ensure_decompose_worktree(spec: config.RepoSpec, issue_number: int) -> Path:
    """Create the decomposer's worktree fresh from current origin/<base>.

    Force-removes any existing decomposer worktree first; the decomposer
    is read-only and stateless across runs, so we always want it to see
    the current base, not whatever was left over from a prior run.

    Serialized by the per-target_root lock for the same `.git/config.lock`
    reason described on `_ensure_worktree`.
    """
    with _owner._target_root_lock(spec.target_root):
        _owner._repo_worktrees_root(spec).mkdir(parents=True, exist_ok=True)
        wt = _owner._decompose_worktree_path(spec, issue_number)
        if wt.exists():
            _owner._git(
                "worktree", "remove", "--force", str(wt),
                cwd=spec.target_root,
            )
        _owner._authed_target_fetch(spec, spec.base_branch)
        worktree_result = _owner._git(
            "worktree", "add", "--detach", str(wt),
            f"{spec.remote_name}/{spec.base_branch}",
            cwd=spec.target_root,
        )
        if worktree_result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {worktree_result.stderr}"
            )
        return wt


def _run_decompose_worktree_removal(spec: config.RepoSpec, issue_number: int) -> None:
    """Force-remove the decomposer worktree under the parent lock if present."""
    wt = _owner._decompose_worktree_path(spec, issue_number)
    if wt.exists():
        with _owner._target_root_lock(spec.target_root):
            _owner._git(
                "worktree", "remove", "--force", str(wt),
                cwd=spec.target_root,
            )


def _cleanup_decompose_worktree(spec: config.RepoSpec, issue_number: int) -> None:
    """Remove the decomposer's worktree if it exists.

    Called at every `_handle_decomposing` exit except the dirty/commits
    park (where the operator may want to inspect before resuming). Every
    step -- including path resolution -- rides the best-effort guard so a
    failure is logged but never raised: cleanup must not mask the real exit.

    Serialized by the per-target_root lock because `worktree remove`
    rewrites the parent clone's `.git/config` and its `worktrees/<name>/`
    metadata directory; without it, a concurrent worker doing
    `_ensure_worktree` against the same target_root can collide on
    `.git/config.lock`.
    """
    try:
        _owner._run_decompose_worktree_removal(spec, issue_number)
    except Exception:
        log.exception(
            "issue=#%d failed to clean up decomposer worktree", issue_number,
        )
