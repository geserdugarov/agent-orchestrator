# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base sync pre pr."""
from __future__ import annotations

from orchestrator import _base_sync_state as _state
from orchestrator import base_sync as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
config = _owner.config
hard_skip_control_label = _owner.hard_skip_control_label
issue_has_label = _owner.issue_has_label
log = _state.log


def _base_sync_issue(
    gh: GitHubClient, issue_number: int,
) -> Optional[Issue]:
    """Return the issue for a worktree, or None when it is not retrievable."""
    try:
        return gh.get_issue(issue_number)
    except Exception:
        log.debug(
            "issue=#%d not retrievable; skipping base sync", issue_number,
        )
        return None


def _issue_skips_base_sync(issue: Issue, issue_number: int) -> bool:
    """Apply dispatcher hard-skips and the question-stage read-only gate."""
    skip_label = hard_skip_control_label(issue)
    if skip_label is not None:
        log.debug(
            "issue=#%d has %r; skipping base sync",
            issue_number,
            skip_label,
        )
        return True
    if not issue_has_label(issue, "question"):
        return False
    log.debug(
        "issue=#%d has 'question' label; skipping base sync "
        "(read-only stage)",
        issue_number,
    )
    return True


def _worktree_behind_base(
    spec: config.RepoSpec, worktree: Path, issue_number: int,
) -> Optional[int]:
    """Return the base lag, or None when the comparison cannot be read."""
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    behind_result = _owner._git(
        "rev-list", "--count", f"HEAD..{base_ref}", cwd=worktree,
    )
    if behind_result.returncode != 0:
        log.debug(
            "issue=#%d skipping base sync: rev-list failed: %s",
            issue_number,
            (behind_result.stderr or "").strip(),
        )
        return None
    try:
        return int((behind_result.stdout or "0").strip() or "0")
    except ValueError:
        return None


def _sync_pre_pr_worktree(
    spec: config.RepoSpec,
    worktree: Path,
    issue_number: int,
    behind: int,
) -> None:
    """Rebase one clean pre-PR worktree and restore it on failure."""
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    succeeded, conflicted_files = _owner._rebase_base_into_worktree(spec, worktree)
    if succeeded:
        log.info(
            "issue=#%d rebased worktree onto %s (was %d commit(s) behind)",
            issue_number,
            base_ref,
            behind,
        )
        return

    abort_result = _owner._git_hardened("rebase", "--abort", cwd=worktree)
    if abort_result.returncode != 0:
        log.warning(
            "issue=#%d base rebase failed and abort failed: %s",
            issue_number,
            (abort_result.stderr or "").strip(),
        )
    if conflicted_files:
        log.info(
            "issue=#%d base rebase has %d conflict(s); aborted -- "
            "resolving_conflict will handle it once a PR exists",
            issue_number,
            len(conflicted_files),
        )
        return
    log.warning(
        "issue=#%d base rebase failed without conflicted files; aborted",
        issue_number,
    )


def _sync_worktree_with_base(
    gh: GitHubClient, spec: config.RepoSpec, worktree: Path, issue_number: int,
) -> None:
    """Bring one per-issue worktree up to date with the configured base.

    Pre-PR worktrees are rebased locally when clean. PR worktrees always
    reach the PR-aware coordinator so a pinned crash-recovery anchor is
    honored even when local HEAD already contains the latest base.
    """
    issue = _owner._base_sync_issue(gh, issue_number)
    if issue is None or _owner._issue_skips_base_sync(issue, issue_number):
        return

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")
    if pr_number is None and _owner._worktree_dirty_files(worktree):
        log.debug(
            "issue=#%d skipping base sync: worktree has uncommitted changes",
            issue_number,
        )
        return

    behind = _owner._worktree_behind_base(spec, worktree, issue_number)
    if behind is None:
        return
    if pr_number is not None:
        _owner._sync_pr_worktree_to_base(
            gh, spec, issue, state, worktree, int(pr_number), behind,
        )
        return
    if behind:
        _owner._sync_pre_pr_worktree(spec, worktree, issue_number, behind)
