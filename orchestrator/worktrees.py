# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git, branch, and worktree plumbing shared by stage handlers.

Owns `_squash_and_force_push` plus the conventional-commit /
branch-state probes (`_first_commit_subject`,
`_is_conventional_subject`, `_pr_title_from_commit_or_issue`,
`_branch_ahead_behind`). Stage handlers live under
`orchestrator/stages/` (decomposition.py, implementing.py,
documenting.py, validating.py, in_review.py, fixing.py, conflicts.py,
question.py) and drive the state machine; the compatibility facade in
`workflow.py` re-exports each helper below under its original name for
backward compatibility with direct test references and
`patch.object(workflow, ...)` patches.

The hardened git subprocess layer -- `_GIT_NO_PROMPT_ENV`,
`_target_root_lock` / `_TARGET_ROOT_LOCKS` / `_TARGET_ROOT_LOCKS_LOCK`,
`_git`, `_git_hardened`, `_authed_fetch`, `_authed_target_fetch`, and
`_push_branch` -- lives in `git_plumbing.py`. The worktree naming /
layout / creation / restoration / cleanup helpers --
`_branch_name`, `_sanitize_slug`, `_repo_worktrees_root`,
`_worktree_path`, `_decompose_worktree_path`, `_ensure_worktree`,
`_ensure_pr_worktree`, `_ensure_decompose_worktree`,
`_cleanup_decompose_worktree`, `_branch_has_unpushed_commits`,
`_cleanup_question_worktree`, `_cleanup_terminal_branch`, and
`_has_new_commits` -- live in `worktree_lifecycle.py`. The local-verify
runner and its worktree-state probes -- `VerifyResult`,
`_run_verify_commands`, `_truncate_verify_output`, `_head_sha`,
`_worktree_dirty_files` -- live in `verify.py`. The per-tick base
refresh and rebase routing -- `_rebase_base_into_worktree`,
`_merge_base_into_worktree`, `_rebase_in_progress`,
`_refresh_base_and_worktrees`, `_PR_REFRESH_DETOUR_LABELS`,
`_sync_worktree_with_base`, `_route_pr_worktree_to_resolving_conflict`
-- live in `base_sync.py`. Every name from all four modules is
re-exported here so existing call sites (`workflow.py` re-exports and
`patch.object(worktrees, "_foo", ...)` test patches that resolve the
symbol against the worktrees module) keep working without touching the
new modules. Test patches that need to INTERCEPT a call from inside
`_refresh_base_and_worktrees` / `_sync_worktree_with_base` must target
`base_sync` directly because the call graph lives there.

Each helper preserves the existing security hardening and crash-recovery
semantics; downstream behavior is unchanged by this extraction. Helpers
remain prefixed with `_` because they are module-internal contracts --
the public surface (the dispatcher entry points and the stage handlers
they route to) still lives in `workflow.py` and `orchestrator/stages/`.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue

from . import config
from .base_sync import _PR_REFRESH_DETOUR_LABELS as _PR_REFRESH_DETOUR_LABELS
from .base_sync import _merge_base_into_worktree as _merge_base_into_worktree
from .base_sync import _rebase_base_into_worktree as _rebase_base_into_worktree
from .base_sync import _rebase_in_progress as _rebase_in_progress
from .base_sync import (
    _refresh_base_and_worktrees as _refresh_base_and_worktrees,
)
from .base_sync import (
    _route_pr_worktree_to_resolving_conflict as _route_pr_worktree_to_resolving_conflict,
)
from .base_sync import _sync_worktree_with_base as _sync_worktree_with_base
from .config import RepoSpec
from .git_plumbing import _GIT_NO_PROMPT_ENV as _GIT_NO_PROMPT_ENV
from .git_plumbing import _TARGET_ROOT_LOCKS as _TARGET_ROOT_LOCKS
from .git_plumbing import _TARGET_ROOT_LOCKS_LOCK as _TARGET_ROOT_LOCKS_LOCK
from .git_plumbing import _authed_fetch as _authed_fetch
from .git_plumbing import _authed_target_fetch as _authed_target_fetch
from .git_plumbing import _git as _git
from .git_plumbing import _git_hardened as _git_hardened
from .git_plumbing import _push_branch as _push_branch
from .git_plumbing import _target_root_lock as _target_root_lock
from .verify import VerifyResult as VerifyResult
from .verify import _head_sha as _head_sha
from .verify import _run_verify_commands as _run_verify_commands
from .verify import _truncate_verify_output as _truncate_verify_output
from .verify import _worktree_dirty_files as _worktree_dirty_files
from .worktree_lifecycle import _SLUG_SAFE_RE as _SLUG_SAFE_RE
from .worktree_lifecycle import _branch_has_unpushed_commits as _branch_has_unpushed_commits
from .worktree_lifecycle import _branch_name as _branch_name
from .worktree_lifecycle import _cleanup_decompose_worktree as _cleanup_decompose_worktree
from .worktree_lifecycle import _cleanup_question_worktree as _cleanup_question_worktree
from .worktree_lifecycle import _cleanup_terminal_branch as _cleanup_terminal_branch
from .worktree_lifecycle import _decompose_worktree_path as _decompose_worktree_path
from .worktree_lifecycle import _ensure_decompose_worktree as _ensure_decompose_worktree
from .worktree_lifecycle import _ensure_pr_worktree as _ensure_pr_worktree
from .worktree_lifecycle import _ensure_worktree as _ensure_worktree
from .worktree_lifecycle import _has_new_commits as _has_new_commits
from .worktree_lifecycle import _repo_worktrees_root as _repo_worktrees_root
from .worktree_lifecycle import _sanitize_slug as _sanitize_slug
from .worktree_lifecycle import _worktree_path as _worktree_path

log = logging.getLogger(__name__)

# Conventional Commits subject: `<type>[(scope)][!]: <subject>`. The type
# allowlist matches the ones the implement/fix prompts teach plus the broader
# Conventional Commits set, so an agent that picks `perf:` or `ci:` still
# gets credited as conventional.
_CONVENTIONAL_RE = re.compile(
    r"^(?:feat|fix|chore|docs|refactor|test|perf|build|ci|style|revert)"
    r"(?:\([^)]+\))?!?:\s+\S",
)


def _branch_ahead_behind(
    spec: RepoSpec, worktree: Path, branch: str
) -> Tuple[int, int]:
    """Return `(ahead, behind)` commit counts for HEAD relative to
    `<remote>/<branch>` in `worktree`.

    `ahead` = commits in HEAD not in `<remote>/<branch>` (unpushed local
    work). `behind` = commits in `<remote>/<branch>` not in HEAD (the
    local branch is stale relative to the remote PR head). `(0, 0)`
    means HEAD and the remote-tracking ref are identical.

    The caller must have fetched `<remote>/<branch>` immediately before
    calling so the comparison is against the current remote tip.
    Returns `(0, 0)` on git error so a transient failure does not
    silently re-route the workflow; the caller's subsequent steps
    (the rebase attempt, the push) surface the underlying problem.
    """
    r = _git_hardened(
        "rev-list", "--left-right", "--count",
        f"refs/remotes/{spec.remote_name}/{branch}...HEAD",
        cwd=worktree,
    )
    if r.returncode != 0:
        return (0, 0)
    parts = (r.stdout or "").strip().split()
    if len(parts) != 2:
        return (0, 0)
    try:
        behind = int(parts[0])
        ahead = int(parts[1])
    except ValueError:
        return (0, 0)
    return (ahead, behind)


def _first_commit_subject(spec: RepoSpec, worktree: Path) -> str:
    """Subject line of the oldest commit in `origin/<base>..HEAD`, or ''.

    Used by `_on_commits` to derive a Conventional-Commits PR title from what
    the agent actually wrote, so the PR title matches the commit history
    when the agent followed the convention. Reads the base branch from the
    spec so a multi-repo deployment with mixed default branches (e.g. one
    repo on `main`, another on `master`) compares against the right remote.
    """
    r = _git(
        "log", "--reverse", "--format=%s",
        f"{spec.remote_name}/{spec.base_branch}..HEAD",
        cwd=worktree,
    )
    if r.returncode != 0:
        return ""
    lines = (r.stdout or "").splitlines()
    return lines[0].strip() if lines else ""


def _is_conventional_subject(subject: str) -> bool:
    return bool(_CONVENTIONAL_RE.match(subject or ""))


def _pr_title_from_commit_or_issue(issue: Issue, first_subject: str) -> str:
    """Pick a Conventional-Commits PR title.

    Prefer the agent's first commit subject when it already follows the
    convention (so the PR title matches the commit history). Otherwise
    fall back to `<type>: <issue title>`, choosing `fix` for bug-labelled
    issues and `feat` everywhere else. Traceability is preserved by the
    `Resolves #<n>` line in the PR body, so the title stays clean.
    """
    subject = (first_subject or "").strip()
    if _is_conventional_subject(subject):
        return subject
    issue_title = (issue.title or "").strip()
    if _is_conventional_subject(issue_title):
        return issue_title
    label_names = {(getattr(l, "name", "") or "").lower() for l in (issue.labels or [])}
    if {"bug", "fix"} & label_names:
        ctype = "fix"
    else:
        ctype = "feat"
    body = issue_title or f"address issue #{issue.number}"
    return f"{ctype}: {body}"


def _squash_and_force_push(
    spec: RepoSpec, worktree: Path, branch: str, issue: Issue,
) -> Tuple[bool, Optional[str], int, Optional[str]]:
    """Squash all commits since `origin/<base>` into one, force-push with lease.

    Returns `(success, new_head_sha, squashed_count, error_message)`:
      * `(True, sha, 0, None)` — nothing to squash (zero or one commit on top
        of base). Caller should leave state alone.
      * `(True, sha, N, None)` — squashed N>1 commits into one. `sha` is the
        new local HEAD; the remote was force-pushed to match.
      * `(False, _, _, error)` — squash or push failed. Caller parks
        awaiting_human; the original commits remain on the local branch
        (we abort before resetting if any check fails) and the remote was
        not updated.

    The squash commit subject reuses the first commit's subject when it
    already matches conventional-commit form; otherwise it builds one from
    the issue title with a `feat:` prefix. Body aggregates the prior
    subjects so reviewers see what landed in the squash. The commit is
    authored under the AGENT_GIT_* identity (via env vars) so attribution
    matches the per-step commits this squash replaces.
    """
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    mb = _git("merge-base", base_ref, "HEAD", cwd=worktree)
    if mb.returncode != 0:
        return False, None, 0, f"merge-base failed: {(mb.stderr or '').strip()}"
    base_sha = (mb.stdout or "").strip()
    if not base_sha:
        return False, None, 0, "merge-base returned empty"

    # Snapshot the original HEAD BEFORE any destructive step. Every
    # post-reset failure path below restores the branch to this SHA so
    # the original commits are still on the branch (as the issue spec
    # requires), and we use it as the pinned lease value for the
    # force-push (the remote was last set to this SHA by the dev's plain
    # push, so a remote drift between then and now means an out-of-band
    # update we must NOT clobber).
    original_head = _head_sha(worktree)
    if not original_head:
        return False, None, 0, "could not read original HEAD"

    # Dirty-tree refusal is a hard precondition for the whole helper, NOT
    # just the rewrite path: the issue spec lists "dirty tree" alongside
    # push rejection / lease violation as a failure that must park
    # awaiting_human and leave the original commits in place. A
    # one-commit branch whose worktree happens to carry uncommitted
    # changes (operator scratch, agent side-effect) must still surface
    # to a human -- handing off to in_review with the dirty state
    # invisible would let the merge land an incomplete head.
    if _worktree_dirty_files(worktree):
        return False, None, 0, "worktree has uncommitted changes"

    log_r = _git(
        "log", "--reverse", "--pretty=%s", f"{base_sha}..HEAD",
        cwd=worktree,
    )
    if log_r.returncode != 0:
        return (
            False, None, 0,
            f"git log failed: {(log_r.stderr or '').strip()}",
        )
    subjects = [
        line for line in (log_r.stdout or "").splitlines() if line.strip()
    ]
    if len(subjects) <= 1:
        # Nothing to squash.
        return True, original_head, 0, None

    if _is_conventional_subject(subjects[0]):
        subject = subjects[0]
    else:
        title = (issue.title or "").strip() or f"resolve issue #{issue.number}"
        subject = f"feat: {title}"

    body_lines = [
        "Squashed commits:",
        *(f"- {s}" for s in subjects),
    ]
    message = subject + "\n\n" + "\n".join(body_lines) + "\n"

    reset_r = _git("reset", "--soft", base_sha, cwd=worktree)
    if reset_r.returncode != 0:
        return (
            False, None, 0,
            f"reset --soft failed: {(reset_r.stderr or '').strip()}",
        )

    def _rollback(reason: str) -> None:
        """Restore the branch to original_head after a post-reset failure.
        Best-effort: a rollback failure leaves the worktree in an
        inconsistent state; logged loudly so an operator notices.
        """
        rb = _git("reset", "--hard", original_head, cwd=worktree)
        if rb.returncode != 0:
            log.error(
                "issue=#%s rollback to %s after %s failed; worktree may be "
                "in an inconsistent state: %s",
                issue.number, original_head, reason,
                (rb.stderr or "").strip(),
            )

    # Hardening for the orchestrator-owned squash commit. The agent has
    # write access to .git/hooks, .git/config (templatedir), and any
    # global/system git config the host user owns. Without these flags a
    # planted pre-commit hook or commit-msg hook would run during this
    # commit and could exfiltrate secrets we hold (no GIT_TOKEN here, but
    # ANTHROPIC_API_KEY etc. live in os.environ for the agent to use).
    # Mirrors the same hardening _push_branch applies.
    commit_env = {
        **os.environ,
        **_GIT_NO_PROMPT_ENV,
        "GIT_AUTHOR_NAME": config.AGENT_GIT_NAME,
        "GIT_AUTHOR_EMAIL": config.AGENT_GIT_EMAIL,
        "GIT_COMMITTER_NAME": config.AGENT_GIT_NAME,
        "GIT_COMMITTER_EMAIL": config.AGENT_GIT_EMAIL,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    commit_r = subprocess.run(
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
        env=commit_env,
    )
    if commit_r.returncode != 0:
        _rollback("squash commit")
        return (
            False, None, 0,
            f"squash commit failed: {(commit_r.stderr or '').strip()}",
        )

    new_sha = _head_sha(worktree)
    if not new_sha:
        _rollback("post-commit head read")
        return False, None, 0, "could not read new HEAD after squash"

    if not _push_branch(spec, worktree, branch, force_with_lease=original_head):
        _rollback("force-push")
        return False, None, 0, (
            "force-push with lease rejected (concurrent update on the "
            "remote, or lease violation); see orchestrator logs"
        )

    return True, new_sha, len(subjects), None
