# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Base sync refresh."""
from __future__ import annotations

from orchestrator import _base_sync_state as _state
from orchestrator import base_sync as _owner

GitHubClient = _owner.GitHubClient
IssueScheduler = _owner.IssueScheduler
Optional = _owner.Optional
Path = _owner.Path
Tuple = _owner.Tuple
config = _owner.config
log = _state.log


def _rebase_base_into_worktree(
    spec: config.RepoSpec, worktree: Path
) -> Tuple[bool, list[str]]:
    """Run `git rebase origin/<base>` in the worktree.

    Returns `(succeeded, conflicted_files)`. On success, `conflicted_files`
    is empty -- whether the rebase was a no-op or replayed commits is the
    caller's job to detect via the HEAD-SHA delta. On failure, the
    conflicted-file list is the unmerged paths from
    `git diff --name-only --diff-filter=U`; an empty list means the rebase
    failed for a non-conflict reason (hooks, permissions, etc.) and the
    caller should park rather than ask the agent to resolve nothing.

    Both subprocess calls run under `_git_hardened`: the diff is
    read-only but still executes inside an agent-writable worktree, so
    a planted hooksPath / fsmonitor would otherwise execute attacker
    code under the orchestrator's UID at diff time.
    """
    rebase_result = _owner._git_hardened(
        "rebase",
        f"{spec.remote_name}/{spec.base_branch}", cwd=worktree,
    )
    if rebase_result.returncode == 0:
        return True, []
    conflicted = _owner._git_hardened(
        "diff", "--name-only", "--diff-filter=U", cwd=worktree,
    )
    files = [
        line.strip() for line in (conflicted.stdout or "").splitlines()
        if line.strip()
    ]
    return False, files


def _merge_base_into_worktree(
    spec: config.RepoSpec, worktree: Path
) -> Tuple[bool, list[str]]:
    """Compatibility alias for older patches/imports.

    TODO(remove after 2026-08-24): drop once out-of-repo patches have moved
    to `_rebase_base_into_worktree`.
    """
    return _owner._rebase_base_into_worktree(spec, worktree)


def _rebase_state_exists(worktree: Path, state_dir: str) -> bool:
    """Resolve one git rebase-state path and report whether it exists."""
    git_path_result = _owner._git_hardened(
        "rev-parse", "--git-path", state_dir, cwd=worktree,
    )
    if git_path_result.returncode != 0:
        return False
    path = (git_path_result.stdout or "").strip()
    if not path:
        return False
    state_path = Path(path)
    if not state_path.is_absolute():
        state_path = worktree / state_path
    return state_path.exists()


def _rebase_in_progress(worktree: Path) -> bool:
    """Return True when the worktree still has an unfinished rebase."""
    return any(
        _owner._rebase_state_exists(worktree, state_dir)
        for state_dir in ("rebase-merge", "rebase-apply")
    )


def _issue_worktree_number(worktree: Path) -> Optional[int]:
    """Return an issue number only for a valid issue worktree directory."""
    if not worktree.is_dir() or not worktree.name.startswith("issue-"):
        return None
    try:
        return int(worktree.name[len("issue-"):])
    except ValueError:
        return None


def _sync_discovered_worktree(
    gh: GitHubClient,
    spec: config.RepoSpec,
    worktree: Path,
    issue_number: int,
    scheduler: Optional[IssueScheduler],
) -> None:
    """Sync one discovered worktree unless its handler is still active."""
    if scheduler is not None and scheduler.is_active(
        spec.slug, issue_number,
    ):
        log.debug(
            "repo=%s issue=#%d active in scheduler; skipping base "
            "sync until the worker completes", spec.slug, issue_number,
        )
        return
    try:
        _owner._sync_worktree_with_base(gh, spec, worktree, issue_number)
    except Exception:
        log.exception(
            "repo=%s issue=#%d base sync failed; continuing",
            spec.slug, issue_number,
        )


def _refresh_base_and_worktrees(
    gh: GitHubClient,
    spec: config.RepoSpec,
    *,
    scheduler: Optional[IssueScheduler] = None,
) -> None:
    """Fetch `origin/<base>` once for the spec and bring every existing
    per-issue worktree up to date.

    Runs at the start of each tick so a base-branch update on the remote
    propagates into in-flight issue worktrees. The per-stage
    `_ensure_*_worktree` helpers only fetch base on (re)creation, so a
    worktree that survives across ticks would otherwise stay anchored at
    whatever `origin/<base>` looked like when it was first added.

    Two paths depending on whether a PR already exists for the issue:

    * **Pre-PR worktrees** (no `pr_number` in pinned state): rebase
      the local worktree onto `origin/<base>` -- no remote yet, so there
      is nothing to push.

    * **PR-having worktrees** (validating / documenting / in_review /
      fixing): rebasing
      locally WITHOUT pushing would diverge local HEAD from `pr.head.sha` and
      break the validating reviewer (it reads local HEAD, so it would
      review a SHA that isn't on the PR) and
      `_squash_and_force_push`'s `--force-with-lease=<original_head>`
      (the lease compares against the un-rebased remote tip). So
      `_sync_pr_worktree_to_base` attempts the rebase in the refresh
      itself: on a clean rebase it pushes (force-with-lease pinned to
      the pre-rebase SHA), resets `review_round`, and relabels to
      `validating` so the reviewer re-runs against the rewritten
      branch directly; the single docs pass is deferred to the post-
      approval handoff to `documenting` in `_handle_validating`. Only
      when the rebase actually leaves conflicted files does the issue
      get relabeled to `resolving_conflict` -- the
      `_handle_resolving_conflict` handler then drives the dev agent to
      resolve the conflict. Issues already labeled
      `resolving_conflict` are left alone (the handler runs this tick
      anyway); other labels are skipped (no PR worktree to refresh in
      those states).

    Rebase keeps the PR history linear after sibling PRs land. Every
    pushed rebase resets `review_round`, so the reviewer must re-run
    against the rewritten SHA before any merge gate can pass.

    Conflicts on the pre-PR path abort the rebase so the worktree stays
    on its original SHA -- conflict resolution still belongs to
    `_handle_resolving_conflict`. Dirty worktrees are skipped so a
    crash-recovered tree with uncommitted edits is never disturbed
    (mirrors `_on_dirty_worktree`'s rule). All failures are logged at
    info/warning and swallowed: keeping every issue moving matters more
    than perfect base sync.

    `scheduler`, when supplied, is consulted before each per-issue
    worktree sync: an issue whose handler is currently in flight in
    that scheduler is skipped this tick. Without this gate, a polling
    pass can rebase a pre-PR worktree under a still-running agent or
    relabel/state-mutate a PR worktree while its handler is still
    running, racing the base refresh against the live worker. The
    scheduler's `submit` path also rejects a duplicate active issue,
    so the workflow handler itself does not run for the in-flight
    issue this tick -- the refresh skip keeps the worktree contract
    matching that "active issues are skipped until completion"
    guarantee. `None` preserves the legacy behavior so direct test
    invocations that supply no scheduler still refresh every worktree.
    """
    fetch_r = _owner._authed_target_fetch(spec, spec.base_branch)
    if fetch_r.returncode != 0:
        log.warning(
            "repo=%s base fetch of %s/%s failed: %s",
            spec.slug, spec.remote_name, spec.base_branch,
            (fetch_r.stderr or "").strip(),
        )
        return

    root = _owner._repo_worktrees_root(spec)
    if not root.exists():
        return

    for worktree in sorted(root.iterdir()):
        issue_number = _owner._issue_worktree_number(worktree)
        if issue_number is not None:
            _owner._sync_discovered_worktree(
                gh, spec, worktree, issue_number, scheduler,
            )
