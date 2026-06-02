# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git, branch, and worktree plumbing shared by stage handlers.

Owns the workflow-aware helpers (`_squash_and_force_push`,
`_refresh_base_and_worktrees`, `_sync_worktree_with_base`) plus the
conventional-commit / branch-state probes
(`_first_commit_subject`, `_is_conventional_subject`,
`_pr_title_from_commit_or_issue`, `_branch_ahead_behind`,
`_rebase_base_into_worktree`, `_rebase_in_progress`). Stage handlers
live under `orchestrator/stages/` (decomposition.py, implementing.py,
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
`_worktree_dirty_files` -- live in `verify.py`. All three modules'
names are re-exported here so existing call sites (`workflow.py`
re-exports and `patch.object(worktrees, "_foo", ...)` test patches)
keep working without touching the new modules.

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
from .github import (
    BACKLOG_LABEL,
    BASE_SYNC_HOLD_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)
from .scheduler import IssueScheduler
from .verify import VerifyResult as VerifyResult
from .verify import _head_sha as _head_sha
from .verify import _run_verify_commands as _run_verify_commands
from .verify import _truncate_verify_output as _truncate_verify_output
from .verify import _worktree_dirty_files as _worktree_dirty_files
from .workflow_messages import _post_pr_comment
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


def _rebase_base_into_worktree(
    spec: RepoSpec, worktree: Path
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
    r = _git_hardened(
        "rebase",
        f"{spec.remote_name}/{spec.base_branch}", cwd=worktree,
    )
    if r.returncode == 0:
        return True, []
    conflicted = _git_hardened(
        "diff", "--name-only", "--diff-filter=U", cwd=worktree,
    )
    files = [
        line.strip() for line in (conflicted.stdout or "").splitlines()
        if line.strip()
    ]
    return False, files


def _merge_base_into_worktree(
    spec: RepoSpec, worktree: Path
) -> Tuple[bool, list[str]]:
    """Compatibility alias for older patches/imports.

    TODO(remove after 2026-08-24): drop once out-of-repo patches have moved
    to `_rebase_base_into_worktree`.
    """
    return _rebase_base_into_worktree(spec, worktree)


def _rebase_in_progress(worktree: Path) -> bool:
    """Return True when the worktree still has an unfinished rebase."""
    for state_dir in ("rebase-merge", "rebase-apply"):
        r = _git_hardened("rev-parse", "--git-path", state_dir, cwd=worktree)
        if r.returncode != 0:
            continue
        path = (r.stdout or "").strip()
        if not path:
            continue
        state_path = Path(path)
        if not state_path.is_absolute():
            state_path = worktree / state_path
        if state_path.exists():
            return True
    return False


def _refresh_base_and_worktrees(
    gh: GitHubClient,
    spec: RepoSpec,
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
      (the lease compares against the un-rebased remote tip). So instead
      we route the issue to `resolving_conflict`: the existing handler
      does the rebase, pushes, and flips back to `validating` (the same
      target as the base-up-to-date no-op exit) so the reviewer re-runs
      against the rewritten branch directly; the single docs pass is
      deferred to the post-approval handoff to `documenting` in
      `_handle_validating`. Applying the `hold_base_sync` label to
      an issue pauses both the pre-PR local rebase and the PR detour
      until the label is removed.
      Issues already labeled `resolving_conflict` are left alone (the
      handler runs this tick anyway); other labels are skipped (no PR
      worktree to refresh in those states).

    Rebase keeps the PR history linear after sibling PRs land. The handler
    resets `review_round` on every pushed rebase, so the reviewer re-runs
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
    fetch_r = _authed_target_fetch(spec, spec.base_branch)
    if fetch_r.returncode != 0:
        log.warning(
            "repo=%s base fetch of %s/%s failed: %s",
            spec.slug, spec.remote_name, spec.base_branch,
            (fetch_r.stderr or "").strip(),
        )
        return

    root = _repo_worktrees_root(spec)
    if not root.exists():
        return

    for wt in sorted(root.iterdir()):
        if not wt.is_dir() or not wt.name.startswith("issue-"):
            continue
        try:
            issue_number = int(wt.name[len("issue-"):])
        except ValueError:
            continue
        if scheduler is not None and scheduler.is_active(
            spec.slug, issue_number,
        ):
            # The handler for this issue is still running on a
            # scheduler worker thread. Rebasing the pre-PR worktree
            # would race the agent's working copy; the PR-having
            # detour would relabel / write pinned state while the
            # handler is mid-write. Skip the sync this tick -- the
            # next polling pass picks it up once the worker exits.
            log.debug(
                "repo=%s issue=#%d active in scheduler; skipping base "
                "sync until the worker completes", spec.slug, issue_number,
            )
            continue
        try:
            _sync_worktree_with_base(gh, spec, wt, issue_number)
        except Exception:
            log.exception(
                "repo=%s issue=#%d base sync failed; continuing",
                spec.slug, issue_number,
            )


# Workflow labels the pre-tick refresh is willing to detour into
# `resolving_conflict` when the PR worktree is behind base. Validating,
# documenting, in_review, and fixing are the PR-stage labels: validating
# may run the reviewer again, documenting is the brief final-docs hop
# between reviewer approval and `in_review`, in_review is parked waiting
# for the HITL ready-ping and the human's manual merge, and fixing is
# between in_review and validating while a PR feedback round is being
# addressed. Documenting only checks ahead/behind vs. the PR branch
# (not the base) itself, so
# without this detour a sibling-PR merge during the docs pass would
# leave the docs commit on a stale base and only the next in_review
# tick would catch it; including the label here means only the
# `hold_base_sync` control label gates a PR-stage worktree's auto-
# rebase. `resolving_conflict` itself is excluded -- the handler runs
# this tick regardless and will do the rebase anyway. Other labels
# mean either no PR yet (pre-PR path applies instead) or terminal
# (done/rejected, nothing to refresh).
_PR_REFRESH_DETOUR_LABELS = frozenset(
    {"validating", "documenting", "in_review", "fixing"},
)


def _sync_worktree_with_base(
    gh: GitHubClient, spec: RepoSpec, worktree: Path, issue_number: int,
) -> None:
    """Bring a single per-issue worktree up to date with `origin/<base>`.

    Pre-PR: rebase onto `origin/<base>` directly. PR-having + behind base +
    label in {validating, documenting, in_review, fixing}: detour the
    issue to
    `resolving_conflict` so the existing handler does rebase + push +
    relabel back to `validating` (every pushed conflict-resolution path
    hands straight back to `validating` so the reviewer re-runs against
    the rebased branch directly; docs do not run here, the single docs
    pass runs after reviewer approval before `in_review` via the
    final-docs handoff to `documenting` in `_handle_validating`) in one
    consistent flow. Skips a dirty worktree
    or a worktree already up to date (no pre-PR rebase attempted, no PR
    detour fired). On a pre-PR content conflict, aborts the rebase so
    the worktree stays on its pre-rebase SHA -- conflict resolution
    lives in `_handle_resolving_conflict`, not here.
    """
    try:
        issue = gh.get_issue(issue_number)
    except Exception:
        log.debug(
            "issue=#%d not retrievable; skipping base sync", issue_number,
        )
        return
    if issue_has_label(issue, BACKLOG_LABEL):
        # Match the dispatcher's hard-skip: `backlog` means "the orchestrator
        # should not touch this issue at all", so refresh must not rebase
        # base, post a PR comment, or detour the issue to
        # `resolving_conflict` before `_process_issue` would have skipped it.
        log.debug(
            "issue=#%d has %r; skipping base sync",
            issue_number, BACKLOG_LABEL,
        )
        return
    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        log.debug(
            "issue=#%d has %r; skipping base sync",
            issue_number, BASE_SYNC_HOLD_LABEL,
        )
        return
    # `question`-labeled issues are read-only: the question agent
    # must not commit, and `_handle_question` already tears down the
    # per-issue worktree on every safe exit. The only worktrees that
    # survive across ticks under this label are the unsafe-park
    # cases (`question_commits`, `question_dirty`, `question_timeout`)
    # where the operator is supposed to inspect what the agent did
    # before resetting; merging `origin/<base>` over that inspection
    # state would mask it. Skip base sync entirely while the label
    # is `question` so the read-only contract holds even if the
    # handler ever leaves the worktree on disk unexpectedly.
    if issue_has_label(issue, "question"):
        log.debug(
            "issue=#%d has 'question' label; skipping base sync "
            "(read-only stage)", issue_number,
        )
        return
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if _worktree_dirty_files(worktree):
        log.debug(
            "issue=#%d skipping base sync: worktree has uncommitted changes",
            issue_number,
        )
        return

    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    behind_r = _git(
        "rev-list", "--count", f"HEAD..{base_ref}", cwd=worktree,
    )
    if behind_r.returncode != 0:
        log.debug(
            "issue=#%d skipping base sync: rev-list failed: %s",
            issue_number, (behind_r.stderr or "").strip(),
        )
        return
    try:
        behind = int((behind_r.stdout or "0").strip() or "0")
    except ValueError:
        return
    if behind == 0:
        return

    if pr_number is not None:
        _route_pr_worktree_to_resolving_conflict(
            gh, spec, issue, state, int(pr_number), behind,
        )
        return

    succeeded, conflicted = _rebase_base_into_worktree(spec, worktree)
    if succeeded:
        log.info(
            "issue=#%d rebased worktree onto %s (was %d commit(s) behind)",
            issue_number, base_ref, behind,
        )
        return

    abort = _git_hardened("rebase", "--abort", cwd=worktree)
    if abort.returncode != 0:
        log.warning(
            "issue=#%d base rebase failed and abort failed: %s",
            issue_number, (abort.stderr or "").strip(),
        )
    if conflicted:
        log.info(
            "issue=#%d base rebase has %d conflict(s); aborted -- "
            "resolving_conflict will handle it once a PR exists",
            issue_number, len(conflicted),
        )
    else:
        log.warning(
            "issue=#%d base rebase failed without conflicted files; aborted",
            issue_number,
        )


def _route_pr_worktree_to_resolving_conflict(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    pr_number: int,
    behind: int,
) -> None:
    """Flip a behind-base PR-having issue to `resolving_conflict`.

    Mirrors `_handle_in_review`'s unmergeable detour, just driven by a
    base advance instead of a PyGithub `mergeable=False`. The handler
    then runs `git rebase origin/<base>` in the worktree, pushes, and
    relabels back to `validating` so the reviewer re-runs against the
    rebased branch directly (a base-up-to-date no-op with no diff
    targets `validating` too). Docs do not run here -- the single docs
    pass runs after reviewer approval before `in_review` via the
    final-docs handoff to `documenting` in `_handle_validating`. This is the only safe pattern for PR-having
    worktrees, since a local-only rebase would diverge local HEAD from
    `pr.head.sha` and break every downstream gate that compares the
    two. This is the only auto-rebase path for PR-having worktrees --
    `_handle_in_review` is permanently manual-merge-only and just parks
    awaiting human attention on an unmergeable PR otherwise.

    Skips the detour when:

    * The label is not one this refresh knows how to drive into
      `resolving_conflict` (only `validating` / `documenting` /
      `in_review` / `fixing`);
      the `resolving_conflict` label itself is also skipped because the
      handler runs this tick anyway and will do the rebase regardless.

    * `awaiting_human=True`. `_handle_resolving_conflict`'s awaiting-human
      branch returns early without rebasing unless a new human comment
      arrived; relabeling here would just hide the existing park behind a
      `resolving_conflict` label without making any progress, including
      the documented `in_review` unmergeable park path. The park
      already invites the human to comment, and once they do, the existing
      handler's resume-on-human-reply branch picks the work up. Auto-
      unparking here would also undermine `_handle_validating`'s
      `MAX_REVIEW_ROUNDS` / `_handle_resolving_conflict`'s
      `MAX_CONFLICT_ROUNDS` caps, which exist precisely to require human
      intervention after repeated failures.

    * The issue has `hold_base_sync`, which is an explicit operator hold for
      series work where base should be integrated once after prerequisite PRs
      land, not after every intermediate base advance.

    * The PR is no longer open. A merged PR advances `origin/<base>`, so
      the still-validating / still-in_review / still-fixing worktree pointed
      at the now-stale branch is naturally behind base; without this gate
      the detour
      would post an "auto-resolution" notice and relabel to
      `resolving_conflict` on a PR the next handler call would finalize to
      `done`. Same for closed-without-merge if base advanced concurrently
      (handler would finalize to `rejected`). Leave terminal PR state to
      the existing stage logic. A `gh.get_pr` failure is treated as
      "leave it alone" -- the handler can retry on the next tick from a
      stable label rather than racing a half-known PR state from refresh.

    The watermark bump in `_handle_in_review`'s analogous detour is
    deliberately NOT replicated here. That bump is safe in_review-side
    because `_handle_in_review` has already scanned new comments before
    the relabel (anything past the watermark has been consumed by the
    fix-loop or filtered as orchestrator-authored). The refresh-time
    detour runs BEFORE any handler scans comments, so `latest_comment_id`
    may include unread human "do not merge" / fix-request comments;
    advancing the watermark here would silently mark them consumed and
    later validation / merge would skip them. The orchestrator's own
    PR notice we just posted is filtered out via `orchestrator_comment_ids`
    on the next `_handle_in_review` scan, so leaving the watermark alone
    does not cause the orchestrator to "see" its own message as fresh
    feedback.
    """
    label = gh.workflow_label(issue)
    if label not in _PR_REFRESH_DETOUR_LABELS:
        log.debug(
            "issue=#%d behind %s/%s by %d but label=%r; not detouring",
            issue.number, spec.remote_name, spec.base_branch, behind, label,
        )
        return

    if state.get("awaiting_human"):
        log.debug(
            "issue=#%d behind %s/%s by %d but awaiting_human=True; "
            "leaving park intact rather than relabeling without progress",
            issue.number, spec.remote_name, spec.base_branch, behind,
        )
        return

    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        log.debug(
            "issue=#%d behind %s/%s by %d but has %r; not detouring",
            issue.number, spec.remote_name, spec.base_branch, behind,
            BASE_SYNC_HOLD_LABEL,
        )
        return

    try:
        pr = gh.get_pr(pr_number)
    except Exception:
        log.debug(
            "issue=#%d could not fetch PR #%d for refresh detour; "
            "leaving label alone, handler will retry next tick",
            issue.number, pr_number,
        )
        return
    pr_status = gh.pr_state(pr)
    if pr_status != "open":
        # Merged / closed PR: the next handler call finalizes to done /
        # rejected. The base advance that put us "behind" is exactly the
        # merge that closed this PR -- there is nothing to auto-resolve.
        log.debug(
            "issue=#%d PR #%d is %s; not detouring (handler will finalize)",
            issue.number, pr_number, pr_status,
        )
        return

    log.info(
        "issue=#%d behind %s/%s by %d commit(s); routing %r -> "
        "resolving_conflict so the handler can rebase, push, and re-review",
        issue.number, spec.remote_name, spec.base_branch, behind, label,
    )

    # Match `_handle_in_review`'s seeding: only initialize `conflict_round`
    # when absent, so a re-entry preserves the cap counter and a
    # perpetually-stuck PR can't ping-pong between handlers indefinitely.
    if state.get("conflict_round") is None:
        state.set("conflict_round", 0)

    try:
        _post_pr_comment(
            gh, pr_number, state,
            f":mag: PR is {behind} commit(s) behind "
            f"`{spec.remote_name}/{spec.base_branch}`; "
            "orchestrator is attempting auto-resolution by rebasing "
            "the branch (label: `resolving_conflict`).",
        )
    except Exception:
        log.exception(
            "issue=#%s could not post auto-rebase notice to PR #%s",
            issue.number, pr_number,
        )

    gh.emit_event(
        "conflict_round",
        issue_number=issue.number,
        stage=label,
        pr_number=pr_number,
        sha=getattr(pr.head, "sha", None) or None,
        action="entered",
        conflict_round=int(state.get("conflict_round") or 0),
        review_round=int(state.get("review_round") or 0),
        retry_count=state.get("retry_count"),
    )
    gh.set_workflow_label(issue, "resolving_conflict")
    gh.write_pinned_state(issue, state)
