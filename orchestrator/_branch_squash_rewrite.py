# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Branch squash rewrite."""
from __future__ import annotations

from orchestrator import _branch_publication_state as _state
from orchestrator import branch_publication as _owner

_SquashPlan = _owner._SquashPlan
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
Tuple = _owner.Tuple
config = _owner.config
os = _owner.os
subprocess = _owner.subprocess
log = _state.log


def _squash_failure(
    error: str,
) -> Tuple[bool, Optional[str], int, Optional[str]]:
    """Return the uniform failure result while leaving commits intact."""
    return False, None, 0, error


def _squash_commit_env() -> dict[str, str]:
    """Return the hardened agent identity used for the squash commit."""
    return {
        **os.environ,
        **_owner._GIT_NO_PROMPT_ENV,
        "GIT_AUTHOR_NAME": config.AGENT_GIT_NAME,
        "GIT_AUTHOR_EMAIL": config.AGENT_GIT_EMAIL,
        "GIT_COMMITTER_NAME": config.AGENT_GIT_NAME,
        "GIT_COMMITTER_EMAIL": config.AGENT_GIT_EMAIL,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _rollback_squash(
    plan: _SquashPlan,
    worktree: Path,
    issue: Issue,
    reason: str,
    error: str,
) -> Tuple[bool, Optional[str], int, Optional[str]]:
    """Restore the original branch after a post-reset failure."""
    rollback_result = _owner._git_hardened(
        "reset", "--hard", plan.original_head, cwd=worktree,
    )
    if rollback_result.returncode != 0:
        log.error(
            "issue=#%s rollback to %s after %s failed; worktree may be "
            "in an inconsistent state: %s",
            issue.number,
            plan.original_head,
            reason,
            (rollback_result.stderr or "").strip(),
        )
    return _owner._squash_failure(error)


def _create_squash_commit(
    worktree: Path, message: str,
) -> subprocess.CompletedProcess:
    """Create the orchestrator-owned commit with hooks and signing disabled."""
    return subprocess.run(
        [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=",
            "-c", "commit.gpgsign=false",
            "commit", "-m", message,
        ],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        env=_owner._squash_commit_env(),
    )


def _rewrite_squash(
    spec: config.RepoSpec,
    worktree: Path,
    branch: str,
    issue: Issue,
    plan: _SquashPlan,
) -> Tuple[bool, Optional[str], int, Optional[str]]:
    """Apply a prepared squash and force-publish it with a pinned lease."""
    reset_result = _owner._git_hardened(
        "reset", "--soft", plan.base_sha, cwd=worktree,
    )
    if reset_result.returncode != 0:
        detail = (reset_result.stderr or "").strip()
        return _owner._squash_failure(f"reset --soft failed: {detail}")

    commit_result = _owner._create_squash_commit(worktree, plan.message)
    if commit_result.returncode != 0:
        detail = (commit_result.stderr or "").strip()
        return _owner._rollback_squash(
            plan,
            worktree,
            issue,
            "squash commit",
            f"squash commit failed: {detail}",
        )

    new_sha = _owner._head_sha(worktree)
    if not new_sha:
        return _owner._rollback_squash(
            plan,
            worktree,
            issue,
            "post-commit head read",
            "could not read new HEAD after squash",
        )
    if not _owner._push_branch(
        spec, worktree, branch, force_with_lease=plan.original_head,
    ):
        return _owner._rollback_squash(
            plan,
            worktree,
            issue,
            "force-push",
            "force-push with lease rejected (concurrent update on the "
            "remote, or lease violation); see orchestrator logs",
        )
    return True, new_sha, len(plan.subjects), None
