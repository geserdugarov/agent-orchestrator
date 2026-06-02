# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree naming, layout, creation, restoration, and cleanup helpers.

Owns the per-spec worktree directory layout, the worktree creation /
restoration / cleanup flows for both implementer and decomposer roles,
and the `_branch_has_unpushed_commits` / `_has_new_commits` probes the
stage handlers use to decide whether a recovered worktree carries
unpushed work.

Imports the hardened git subprocess layer from `git_plumbing.py` and
reuses its per-target_root lock so concurrent workers cannot race the
parent clone's `.git/config.lock`. The workflow-aware helpers
(`_run_verify_commands`, `_squash_and_force_push`,
`_refresh_base_and_worktrees`, `_sync_worktree_with_base`) stay in
`worktrees.py`; that module re-exports every name below under its
original name so existing imports and
`patch.object(worktrees, "_foo", ...)` test patches keep working.

Each helper preserves the existing security hardening and crash-recovery
semantics; downstream behavior is unchanged by this extraction. Helpers
remain prefixed with `_` because they are module-internal contracts --
the public surface (the dispatcher entry points and the stage handlers
they route to) still lives in `workflow.py` and `orchestrator/stages/`.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from . import config
from .config import RepoSpec
from .git_plumbing import _authed_target_fetch, _git, _target_root_lock
from .github import GitHubClient

log = logging.getLogger(__name__)


def _branch_name(issue_number: int) -> str:
    return f"orchestrator/issue-{issue_number}"


# Allowed characters in a worktree directory segment: alphanumerics plus
# `_`, `.`, `-`. `/` is excluded so the slug stays a single path segment;
# anything else is replaced with `_`. A leading `.` is also escaped so the
# per-repo subdir is never a hidden directory.
_SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize_slug(slug: str) -> str:
    """Turn an `owner/name` repo slug into a single filesystem-safe segment.

    `/` collapses to `__` so two repos with the same issue number cannot
    share a worktree path. Any other character outside `[A-Za-z0-9_.-]`
    becomes `_`. A leading `.` is escaped to `_.` so the per-repo subdir
    is never a dotfile-hidden directory. An empty/all-stripped input
    falls back to `_` rather than collapsing into the bare WORKTREES_DIR.
    """
    cleaned = _SLUG_SAFE_RE.sub("_", (slug or "").replace("/", "__"))
    if not cleaned:
        return "_"
    if cleaned.startswith("."):
        cleaned = "_" + cleaned
    return cleaned


def _repo_worktrees_root(spec: RepoSpec) -> Path:
    """Per-repo subdirectory under WORKTREES_DIR for this spec.

    Two specs with the same issue number must not collide on disk, so the
    issue-N / decompose-N segments live inside a sanitized-slug parent
    instead of directly under WORKTREES_DIR.
    """
    return config.WORKTREES_DIR / _sanitize_slug(spec.slug)


def _worktree_path(spec: RepoSpec, issue_number: int) -> Path:
    return _repo_worktrees_root(spec) / f"issue-{issue_number}"


def _ensure_worktree(spec: RepoSpec, issue_number: int) -> Path:
    """Return a worktree on a per-issue branch, reusing one with unpushed work.

    The reuse is what lets the orchestrator survive a crash between codex
    committing and the orchestrator pushing -- without it, the next tick would
    wipe the worktree and we'd burn another codex run on the same prompt.

    All git operations target `spec.target_root` and therefore mutate the
    parent clone's `.git/config`. The per-target_root lock (see
    `_target_root_lock`) serializes concurrent workers so two tick fan-out
    threads cannot collide on `.git/config.lock`. The lock is released
    before the caller starts the long-running agent run.
    """
    with _target_root_lock(spec.target_root):
        _repo_worktrees_root(spec).mkdir(parents=True, exist_ok=True)
        wt = _worktree_path(spec, issue_number)
        branch = _branch_name(issue_number)

        if wt.exists():
            if _has_new_commits(spec, wt):
                log.info(
                    "issue=#%d worktree has unpushed commits; reusing",
                    issue_number,
                )
                return wt
            _git(
                "worktree", "remove", "--force", str(wt),
                cwd=spec.target_root,
            )

        _authed_target_fetch(spec, spec.base_branch)

        have_branch = _git(
            "rev-parse", "--verify", branch, cwd=spec.target_root
        ).returncode == 0
        if have_branch:
            result = _git(
                "worktree", "add", str(wt), branch, cwd=spec.target_root,
            )
        else:
            result = _git(
                "worktree", "add", "-b", branch, str(wt),
                f"{spec.remote_name}/{spec.base_branch}",
                cwd=spec.target_root,
            )
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr}")
        return wt


def _ensure_pr_worktree(spec: RepoSpec, issue_number: int) -> Path:
    """Like `_ensure_worktree`, but restores the local branch from
    `origin/<branch>` when it is missing instead of branching from
    `origin/<base>`.

    `_ensure_worktree`'s fallback (`worktree add -b <branch> ... origin/<base>`)
    is right for a fresh implementing run -- a brand-new PR branch should
    start at the base. It is the WRONG fallback for `_handle_resolving_conflict`:
    once a PR exists, the conflict resolver MUST land on the same branch
    the PR is open against, with the dev's commits intact. A host
    restart, manual cleanup, or `git branch -D` between ticks deletes
    the local ref but leaves the PR's `origin/<branch>` ref alive on
    GitHub; rebuilding off `origin/<base>` would silently discard the
    PR's commits and leave the PR's conflicts unresolved forever.

    All git invocations run from `spec.target_root` (the orchestrator's
    own clone, not the agent-writable worktree) so authenticated fetch
    uses the operator's git config / credential helpers / SSH keys
    directly. The hardening that `_push_branch` applies is unnecessary
    here because nothing in `target_root` is agent-writable.

    Serialized by the per-target_root lock for the same `.git/config.lock`
    reason described on `_ensure_worktree`.
    """
    with _target_root_lock(spec.target_root):
        _repo_worktrees_root(spec).mkdir(parents=True, exist_ok=True)
        wt = _worktree_path(spec, issue_number)
        branch = _branch_name(issue_number)

        if wt.exists():
            if _has_new_commits(spec, wt):
                log.info(
                    "issue=#%d worktree has unpushed commits; reusing",
                    issue_number,
                )
                return wt
            _git(
                "worktree", "remove", "--force", str(wt),
                cwd=spec.target_root,
            )

        # Fetch both base and the PR's remote branch so either path
        # below has a fresh ref to anchor on. The PR branch fetch is
        # best-effort: a freshly created PR may not have a remote ref
        # yet (the orchestrator's own push opened it), but in that case
        # the local branch must already exist (we just pushed it). Treat
        # fetch failure as non-fatal and let the local ref check below
        # decide. `_authed_target_fetch` already uses the explicit
        # `+refs/heads/<branch>:refs/remotes/<remote>/<branch>` refspec
        # so single-branch / narrowed clones still create the
        # remote-tracking ref the `worktree add ... <remote>/<branch>`
        # fallback anchors on; the `+` prefix forces non-fast-forward
        # update against `--force-with-lease`-rewritten remote tips.
        _authed_target_fetch(spec, spec.base_branch)
        _authed_target_fetch(spec, branch)

        have_local = _git(
            "rev-parse", "--verify", branch, cwd=spec.target_root,
        ).returncode == 0
        if have_local:
            result = _git(
                "worktree", "add", str(wt), branch, cwd=spec.target_root,
            )
        else:
            # Restore the local branch from the PR's remote head, NOT
            # from `<remote>/<base>` -- the dev's commits live on
            # `<remote>/<branch>` and rebuilding from base would discard
            # them.
            result = _git(
                "worktree", "add", "-b", branch, str(wt),
                f"{spec.remote_name}/{branch}",
                cwd=spec.target_root,
            )
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr}")
        return wt


def _has_new_commits(spec: RepoSpec, worktree: Path) -> bool:
    r = _git(
        "rev-list", "--count",
        f"{spec.remote_name}/{spec.base_branch}..HEAD",
        cwd=worktree,
    )
    if r.returncode != 0:
        return False
    return int((r.stdout or "0").strip() or "0") > 0


# The decomposer needs a working directory to run `git ls-files` / `wc -l`
# against, but it must never touch the implementer's per-issue branch. If
# it shared `_worktree_path(issue)`, the local `orchestrator/issue-<n>`
# branch would get anchored at whatever `origin/<base>` snapshot the
# decomposer saw -- and a `split` decision parks the parent on `blocked`
# for the duration of its children's lifecycle. By the time the parent
# flips to `ready` and the implementer takes over, `origin/<base>` has
# advanced (children's PRs merged), but `_ensure_worktree` would re-add
# the worktree pointing at that stale branch and the implementer would
# commit on the old base. A separate detached-HEAD checkout sidesteps the
# problem entirely: the implementer's `_ensure_worktree` always sees a
# fresh per-issue branch created from the current `origin/<base>`.
def _decompose_worktree_path(spec: RepoSpec, issue_number: int) -> Path:
    return _repo_worktrees_root(spec) / f"decompose-{issue_number}"


def _ensure_decompose_worktree(spec: RepoSpec, issue_number: int) -> Path:
    """Create the decomposer's worktree fresh from current origin/<base>.

    Force-removes any existing decomposer worktree first; the decomposer
    is read-only and stateless across runs, so we always want it to see
    the current base, not whatever was left over from a prior run.

    Serialized by the per-target_root lock for the same `.git/config.lock`
    reason described on `_ensure_worktree`.
    """
    with _target_root_lock(spec.target_root):
        _repo_worktrees_root(spec).mkdir(parents=True, exist_ok=True)
        wt = _decompose_worktree_path(spec, issue_number)
        if wt.exists():
            _git(
                "worktree", "remove", "--force", str(wt),
                cwd=spec.target_root,
            )
        _authed_target_fetch(spec, spec.base_branch)
        result = _git(
            "worktree", "add", "--detach", str(wt),
            f"{spec.remote_name}/{spec.base_branch}",
            cwd=spec.target_root,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr}")
        return wt


def _cleanup_decompose_worktree(spec: RepoSpec, issue_number: int) -> None:
    """Remove the decomposer's worktree if it exists.

    Called at every `_handle_decomposing` exit except the dirty/commits
    park (where the operator may want to inspect before resuming). Failures
    are logged but never raised -- cleanup must not mask the real exit.

    Serialized by the per-target_root lock because `worktree remove`
    rewrites the parent clone's `.git/config` and its `worktrees/<name>/`
    metadata directory; without it, a concurrent worker doing
    `_ensure_worktree` against the same target_root can collide on
    `.git/config.lock`.
    """
    try:
        wt = _decompose_worktree_path(spec, issue_number)
        if wt.exists():
            with _target_root_lock(spec.target_root):
                _git(
                    "worktree", "remove", "--force", str(wt),
                    cwd=spec.target_root,
                )
    except Exception:
        log.exception(
            "issue=#%d failed to clean up decomposer worktree", issue_number,
        )


def _branch_has_unpushed_commits(
    spec: RepoSpec, issue_number: int,
) -> bool:
    """True if the local `orchestrator/issue-N` branch exists in
    `spec.target_root` and carries commits beyond
    `<remote>/<base>`.

    Inspects the parent clone directly so the answer does not
    depend on a per-issue worktree existing on disk. The question-
    stage relabel guard in `_handle_implementing` needs this: if
    the operator manually removes the worktree (or
    `_cleanup_question_worktree` partially failed) but the local
    branch survives with question-agent commits, the
    worktree-only `_has_new_commits` / `_worktree_dirty_files`
    checks would report "clean" and the relabel-clear would let
    `_ensure_worktree` restore the branch in a fresh worktree;
    the recovered-worktree shortcut would then push those commits
    as if a dev session authored them.

    Returns False when:

    * the local branch does not exist (no state to inspect);
    * the local branch exists at exactly `<remote>/<base>` (a
      fresh-from-base reset);
    * the `rev-list` itself errors (transient git failure -- the
      caller's later steps will surface the underlying problem if
      it persists).

    Returns True only when the branch has at least one commit
    that is not in `<remote>/<base>`, which is the exact
    condition the recovered-worktree shortcut would treat as
    "unpushed dev work" -- the read-only-violation we are trying
    to prevent.

    Serialized via `_target_root_lock` for the same
    `.git/config.lock` reason described on `_ensure_worktree`;
    `RLock` re-entry keeps callers that already hold the lock
    safe.
    """
    branch = _branch_name(issue_number)
    with _target_root_lock(spec.target_root):
        have_local = _git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
            cwd=spec.target_root,
        ).returncode == 0
        if not have_local:
            return False
        r = _git(
            "rev-list", "--count",
            f"refs/remotes/{spec.remote_name}/{spec.base_branch}"
            f"..refs/heads/{branch}",
            cwd=spec.target_root,
        )
    if r.returncode != 0:
        return False
    try:
        return int((r.stdout or "0").strip() or "0") > 0
    except ValueError:
        return False


def _cleanup_question_worktree(spec: RepoSpec, issue_number: int) -> None:
    """Tear down the per-issue worktree and local branch after a
    `_handle_question` tick.

    The question stage spawns the agent in the same `issue-N`
    worktree the implementing stage uses, but the agent is read-only
    -- it never commits or pushes. Leaving the worktree on disk
    between ticks lets the per-tick `_refresh_base_and_worktrees`
    treat it as a pre-PR worktree behind base and merge
    `origin/<base>` into it, accreting local commits on a read-only
    question branch. A later relabel to `implementing` then either
    trips the `question_unsafe_relabel` guard (worktree still on
    disk) or, if a stale local branch survives a worktree GC, falls
    through to the recovered-worktree push path. Either way the
    "question responses without PRs / read-only" contract breaks.

    Called from every safe-exit of `_handle_question` (answer,
    silent, no-resume return). Skipped for the parks that
    explicitly KEEP the worktree so the operator can inspect what
    the misbehaving agent did (`question_commits`, `question_dirty`,
    `question_timeout`); the workflow-label skip in
    `_sync_worktree_with_base` then prevents base sync from
    mutating those kept worktrees behind the operator's back.

    Removes the worktree AND the local branch. The next answer /
    resume / relabel rebuilds the worktree from a fresh
    `origin/<base>`; agent session state lives in pinned state, not
    in the worktree, so resume across the cleanup works.

    No remote-side step -- the question stage never pushed, so
    there is no remote branch to delete. Best-effort: each step
    swallows its own error so cleanup never raises out of the
    handler. Serialized via `_target_root_lock` for the same
    `.git/config.lock` reason described on `_ensure_worktree`.
    """
    branch = _branch_name(issue_number)
    try:
        wt = _worktree_path(spec, issue_number)
        if wt.exists():
            with _target_root_lock(spec.target_root):
                r = _git(
                    "worktree", "remove", "--force", str(wt),
                    cwd=spec.target_root,
                )
            if r.returncode != 0:
                log.warning(
                    "issue=#%d question worktree remove failed: %s",
                    issue_number, (r.stderr or "").strip(),
                )
    except Exception:
        log.exception(
            "issue=#%d question worktree remove raised", issue_number,
        )

    try:
        with _target_root_lock(spec.target_root):
            have_local = _git(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                cwd=spec.target_root,
            ).returncode == 0
            if have_local:
                r = _git("branch", "-D", branch, cwd=spec.target_root)
                if r.returncode != 0:
                    log.warning(
                        "issue=#%d question local branch %r delete failed: %s",
                        issue_number, branch, (r.stderr or "").strip(),
                    )
    except Exception:
        log.exception(
            "issue=#%d question local branch %r delete raised",
            issue_number, branch,
        )


def _cleanup_terminal_branch(
    gh: GitHubClient, spec: RepoSpec, issue_number: int
) -> None:
    """Remove the per-issue worktree and delete the local + remote branches.

    Called after the PR for `issue_number` reached a terminal state -- either
    merged externally by a human (the orchestrator is permanently manual-
    merge-only and never calls `gh.merge_pr`) or closed without merge.
    Best-effort: each step swallows its own error so a leftover
    worktree or branch never raises out of the terminal handler -- by the
    time we reach here the issue has already flipped to `done` or
    `rejected`, and a stale ref is tidiness, not correctness.

    The branch name is derived from the issue number and constrained to the
    orchestrator-owned `orchestrator/issue-<n>` namespace, so this cleanup
    cannot touch an arbitrary branch.

    Order matters: the worktree must come down before `git branch -D`,
    because git refuses to delete a branch that's still checked out in a
    worktree. Remote delete is last so a local-side failure does not block
    cleaning up the GitHub side (which is what the operator actually sees
    in the repo's branch list). All local `_git` calls run from
    `spec.target_root` so the multi-repo loop tidies the right clone.

    Both local-side steps are serialized by the per-target_root lock
    because `worktree remove` and `branch -D` write to the parent
    `.git/config` and `.git/refs`; without the lock a concurrent
    `_ensure_worktree` on another worker thread races on
    `.git/config.lock`. The remote delete is a GitHub-side HTTP call
    (no local git plumbing) and stays outside the lock.
    """
    branch = _branch_name(issue_number)

    # Each step is wrapped individually: a raise from `_git` (missing
    # `spec.target_root`, missing `git` binary, OSError) or from the
    # `Path.exists()` probe must not skip the later steps, since the
    # caller has already written the terminal pinned state and expects
    # cleanup to never propagate.
    try:
        wt = _worktree_path(spec, issue_number)
        if wt.exists():
            with _target_root_lock(spec.target_root):
                r = _git(
                    "worktree", "remove", "--force", str(wt),
                    cwd=spec.target_root,
                )
            if r.returncode != 0:
                log.warning(
                    "issue=#%d worktree remove failed: %s",
                    issue_number, (r.stderr or "").strip(),
                )
    except Exception:
        log.exception(
            "issue=#%d worktree remove raised", issue_number,
        )

    try:
        with _target_root_lock(spec.target_root):
            have_local = _git(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                cwd=spec.target_root,
            ).returncode == 0
            if have_local:
                r = _git("branch", "-D", branch, cwd=spec.target_root)
                if r.returncode != 0:
                    log.warning(
                        "issue=#%d local branch %r delete failed: %s",
                        issue_number, branch, (r.stderr or "").strip(),
                    )
    except Exception:
        log.exception(
            "issue=#%d local branch %r delete raised", issue_number, branch,
        )

    try:
        gh.delete_remote_branch(branch)
    except Exception:
        log.exception(
            "issue=#%d remote branch %r delete raised", issue_number, branch,
        )
