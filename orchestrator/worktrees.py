# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git, branch, and worktree plumbing shared by stage handlers.

Centralizes every direct shell-out to `git` plus the per-spec worktree
layout helpers. Stage handlers in `workflow.py` continue to drive the
state machine; this module owns:

* Branch naming and slug-safe per-repo worktree paths.
* Worktree creation, restoration, and cleanup for both the implementer
  and decomposer roles, plus terminal-state cleanup.
* Hardened git invocations (`_git`, `_git_hardened`) and authenticated
  fetch/push helpers (`_authed_fetch`, `_push_branch`) that keep the
  GitHub PAT off `argv` and detach from any agent-writable git config.
* Squash-on-approval and per-tick base refresh / per-worktree base sync.

Each helper preserves the existing security hardening and crash-recovery
semantics; downstream behavior is unchanged by this extraction. Helpers
remain prefixed with `_` because they are module-internal contracts -- the
public surface (the dispatcher entry points) still lives in `workflow.py`.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue

from . import config
from .config import RepoSpec
from .github import (
    BASE_SYNC_HOLD_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)
from .workflow_messages import _post_pr_comment

log = logging.getLogger(__name__)

# Disable git's /dev/tty fallback prompts in any subprocess we spawn.
_GIT_NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0"}

# Conventional Commits subject: `<type>[(scope)][!]: <subject>`. The type
# allowlist matches the ones the implement/fix prompts teach plus the broader
# Conventional Commits set, so an agent that picks `perf:` or `ci:` still
# gets credited as conventional.
_CONVENTIONAL_RE = re.compile(
    r"^(?:feat|fix|chore|docs|refactor|test|perf|build|ci|style|revert)"
    r"(?:\([^)]+\))?!?:\s+\S",
)


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


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_NO_PROMPT_ENV},
    )


def _ensure_worktree(spec: RepoSpec, issue_number: int) -> Path:
    """Return a worktree on a per-issue branch, reusing one with unpushed work.

    The reuse is what lets the orchestrator survive a crash between codex
    committing and the orchestrator pushing -- without it, the next tick would
    wipe the worktree and we'd burn another codex run on the same prompt.
    """
    _repo_worktrees_root(spec).mkdir(parents=True, exist_ok=True)
    wt = _worktree_path(spec, issue_number)
    branch = _branch_name(issue_number)

    if wt.exists():
        if _has_new_commits(spec, wt):
            log.info("issue=#%d worktree has unpushed commits; reusing", issue_number)
            return wt
        _git("worktree", "remove", "--force", str(wt), cwd=spec.target_root)

    _git(
        "fetch", "--quiet", spec.remote_name, spec.base_branch,
        cwd=spec.target_root,
    )

    have_branch = _git(
        "rev-parse", "--verify", branch, cwd=spec.target_root
    ).returncode == 0
    if have_branch:
        result = _git("worktree", "add", str(wt), branch, cwd=spec.target_root)
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
    """
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
        _git("worktree", "remove", "--force", str(wt), cwd=spec.target_root)

    # Fetch both base and the PR's remote branch so either path
    # below has a fresh ref to anchor on.
    _git(
        "fetch", "--quiet", spec.remote_name, spec.base_branch,
        cwd=spec.target_root,
    )
    # The PR branch fetch is best-effort: a freshly created PR may not
    # have a remote ref yet (the orchestrator's own push opened it),
    # but in that case the local branch must already exist (we just
    # pushed it). Treat fetch failure as non-fatal and let the local
    # ref check below decide.
    #
    # Use an explicit refspec so single-branch / narrowed clones still
    # create the `refs/remotes/<remote>/<branch>` ref. A bare `git fetch
    # <remote> <branch>` on a single-branch clone only updates FETCH_HEAD
    # and leaves no `<remote>/<branch>` for the
    # `worktree add ... <remote>/<branch>` fallback to anchor on. The `+`
    # prefix forces non-fast-forward update, which we want because the
    # orchestrator pushes with `--force-with-lease` and the local
    # remote-tracking ref may be stale relative to the just-rewritten
    # remote tip.
    _git(
        "fetch", "--quiet", spec.remote_name,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=spec.target_root,
    )

    have_local = _git(
        "rev-parse", "--verify", branch, cwd=spec.target_root,
    ).returncode == 0
    if have_local:
        result = _git(
            "worktree", "add", str(wt), branch, cwd=spec.target_root,
        )
    else:
        # Restore the local branch from the PR's remote head, NOT from
        # `<remote>/<base>` -- the dev's commits live on `<remote>/<branch>`
        # and rebuilding from base would discard them.
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
    (the merge attempt, the push) surface the underlying problem.
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


def _head_sha(worktree: Path) -> str:
    """HEAD commit SHA of the worktree, or '' if it cannot be read.

    Used by the validating handler to detect whether a dev-fix codex run
    produced a new commit. _has_new_commits compares against origin/<base>,
    which is already true throughout validating, so we need an absolute SHA
    snapshot instead.
    """
    r = _git("rev-parse", "HEAD", cwd=worktree)
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _worktree_dirty_files(worktree: Path) -> list[str]:
    """Paths git considers modified or untracked in the worktree.

    Used to refuse opening a PR when codex committed only part of its work and
    left other modifications behind -- the push would publish an incomplete
    branch. The orchestrator's own scratch (codex's `-o` file) lives outside
    the worktree (a per-spawn tempfile in `_run_codex`), so it never surfaces
    here regardless of the target repo's .gitignore.
    """
    r = _git("status", "--porcelain", cwd=worktree)
    if r.returncode != 0:
        return []
    paths: list[str] = []
    for line in (r.stdout or "").splitlines():
        if len(line) < 4:
            continue
        # porcelain v1: "XY <path>" with optional " -> dest" for renames.
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        path = rest.strip().strip('"')
        if path:
            paths.append(path)
    return paths


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
    """
    _repo_worktrees_root(spec).mkdir(parents=True, exist_ok=True)
    wt = _decompose_worktree_path(spec, issue_number)
    if wt.exists():
        _git("worktree", "remove", "--force", str(wt), cwd=spec.target_root)
    _git(
        "fetch", "--quiet", spec.remote_name, spec.base_branch,
        cwd=spec.target_root,
    )
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
    """
    try:
        wt = _decompose_worktree_path(spec, issue_number)
        if wt.exists():
            _git("worktree", "remove", "--force", str(wt), cwd=spec.target_root)
    except Exception:
        log.exception(
            "issue=#%d failed to clean up decomposer worktree", issue_number,
        )


def _cleanup_terminal_branch(
    gh: GitHubClient, spec: RepoSpec, issue_number: int
) -> None:
    """Remove the per-issue worktree and delete the local + remote branches.

    Called after the PR for `issue_number` reached a terminal state -- either
    merged (via AUTO_MERGE or an external human merge) or closed without
    merge. Best-effort: each step swallows its own error so a leftover
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


def _push_branch(
    spec: RepoSpec, worktree: Path, branch: str,
    *,
    force_with_lease: Optional[str] = None,
) -> bool:
    """Push via GIT_ASKPASS so the token never appears in argv.

    `force_with_lease`, when provided, is the SHA the caller expects the
    remote ref to be at. The push then uses
    `--force-with-lease=refs/heads/<branch>:<sha>` against that exact SHA,
    so a concurrent update to the remote rejects the push instead of being
    silently clobbered. This is the squash/rewrite path: pinning the lease
    to the caller-supplied pre-rewrite HEAD (rather than reading it from
    the live remote) prevents the "out-of-band update happened in the
    window between approval and push" race -- a fresh `ls-remote` would
    treat the unexpected new remote SHA as the lease value and silently
    overwrite it.

    When `force_with_lease` is None (the default), the function reads the
    current remote SHA via `ls-remote` and uses that as the lease. This is
    the normal-push path: the orchestrator owns the
    `orchestrator/issue-<n>` namespace, but a self-restart between commit
    and push can leave the worktree on a different SHA than what was
    already pushed -- e.g. a `resume=False` rerun of codex amending
    equivalent work. A plain push then fails non-fast-forward and parks
    the issue. The lease lets the retry succeed while still refusing to
    clobber a concurrent foreign update (the lease check compares against
    what we observed, not a stale remote-tracking ref).

    The push target URL carries only the username (`x-access-token`); the
    token itself is read from the GIT_TOKEN env var by a tempfile askpass
    script. This keeps the PAT out of `/proc/<pid>/cmdline`, which is
    world-readable on Linux. We also use an explicit `HEAD:refs/heads/<branch>`
    refspec so no upstream is set and no remote URL is stored in .git/config.

    The worktree is shared with the codex agent, so anything in `.git/hooks/`
    or `.git/config` is attacker-controlled. The agent also writes as the same
    OS user, so it can plant `~/.gitconfig` (or anything pointed at by
    XDG_CONFIG_HOME) before we push. We harden the push so a planted pre-push
    hook, credential helper, fsmonitor, or url-rewrite rule cannot observe
    GIT_TOKEN or redirect the push to an attacker-controlled host:
      * `core.hooksPath=/dev/null` disables `.git/hooks/*` and any hooksPath
        override the agent set in the local config.
      * `credential.helper=` (empty) clears all inherited credential helpers
        so a repo-local helper script never executes with GIT_TOKEN in env.
      * `core.fsmonitor=` disables any fsmonitor program git would otherwise
        spawn for index-touching operations.
      * `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_SYSTEM=/dev/null` block
        global/system config entirely, so url.<host>.insteadOf or
        pushInsteadOf rules planted in `~/.gitconfig` (or `/etc/gitconfig`)
        cannot rewrite our auth URL and exfiltrate the askpass token.
      * We also refuse to push if the local config contains any url
        insteadOf/pushInsteadOf rewrite, since those rewrite our auth URL
        and would deliver the token to whatever host the agent picked.
    """
    # Resolve the token from `spec.slug` rather than the cached
    # `config.GITHUB_TOKEN` (which was looked up once for `config.REPO`),
    # so a multi-repo deployment with one token file per slug under
    # `~/.config/<owner>/<repo>/token` pushes with the right repo's token.
    # Single-repo deployments see identical behavior because
    # `_resolve_github_token(REPO)` returns the same value.
    token = config._resolve_github_token(spec.slug)
    if not token:
        log.error("GITHUB_TOKEN missing for %s; cannot push", spec.slug)
        return False
    rewrite = subprocess.run(
        ["git", "config", "--local", "--get-regexp",
         r"^url\..*\.(insteadof|pushinsteadof)$"],
        cwd=str(worktree), capture_output=True, text=True,
    )
    if rewrite.returncode == 0 and rewrite.stdout.strip():
        log.error(
            "refusing to push %s: worktree .git/config has url rewrite rules: %s",
            branch, rewrite.stdout.strip(),
        )
        return False
    auth_url = f"https://x-access-token@github.com/{spec.slug}.git"
    with tempfile.TemporaryDirectory(prefix="orch-askpass-") as td:
        askpass = Path(td) / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf %s "$GIT_TOKEN"\n')
        askpass.chmod(0o700)
        env = {
            **os.environ,
            **_GIT_NO_PROMPT_ENV,
            "GIT_ASKPASS": str(askpass),
            "GIT_TOKEN": token,
            # Detach from any agent-writable global/system git config; the
            # only config that applies is the local worktree config (already
            # checked above) plus our explicit -c overrides below.
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        git_prefix = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "core.fsmonitor=",
        ]
        ref = f"refs/heads/{branch}"
        if force_with_lease is not None:
            remote_sha = force_with_lease
        else:
            ls = subprocess.run(
                [*git_prefix, "ls-remote", auth_url, ref],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                env=env,
            )
            if ls.returncode != 0:
                scrubbed = (ls.stderr or "").replace(token, "***")
                log.error("git ls-remote failed for %s: %s", branch, scrubbed)
                return False
            remote_sha = ""
            for line in (ls.stdout or "").splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == ref:
                    remote_sha = parts[0]
                    break
        # An empty <expected> in --force-with-lease means "expect the ref to
        # not exist", which is the right lease for the create-branch case.
        r = subprocess.run(
            [
                *git_prefix,
                "push",
                f"--force-with-lease={ref}:{remote_sha}",
                auth_url, f"HEAD:{ref}",
            ],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            env=env,
        )
    if r.returncode != 0:
        # Scrub the token out of any error output before logging.
        scrubbed = (r.stderr or "").replace(token, "***")
        log.error("git push failed for %s: %s", branch, scrubbed)
        return False
    return True


def _squash_and_force_push(
    spec: RepoSpec, worktree: Path, branch: str, issue: Issue,
) -> Tuple[bool, Optional[str], int, Optional[str]]:
    """Squash all commits since `origin/<base>` into one, force-push with lease.

    Returns `(success, new_head_sha, squashed_count, error_message)`:
      * `(True, sha, 0, None)` — nothing to squash (zero or one commit on top
        of base). Caller should leave state alone; agent_approved_sha keeps
        pointing at the SHA the reviewer ran against.
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
    # invisible would let AUTO_MERGE land an incomplete head.
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
        # Nothing to squash; the caller can still record original_head
        # as agent_approved_sha if it wants.
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


def _git_hardened(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """`_git` plus the agent-hostile-environment hardening from `_push_branch`.

    Used for `git merge` inside a worktree the agent can write to: a
    planted `core.hooksPath`, `core.fsmonitor`, or url rewrite rule in
    the worktree's `.git/config` (or in `~/.gitconfig`) would otherwise
    execute attacker code mid-merge or redirect a transient fetch to an
    attacker-controlled host. Drops global/system git config so url
    `insteadOf` rewrites and host-wide hooks cannot apply, and disables
    repo-local hooks / fsmonitor / credential helpers via `-c` overrides.
    No askpass is wired in -- this helper is for local-only operations
    (merge); push remains the only call site that handles GIT_TOKEN.

    Injects `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env vars (matching the
    agent spawn's `_agent_env`) so a `git merge --no-edit` that needs
    to create a merge commit doesn't fail with "Committer identity
    unknown" -- stripping global config also strips any `user.name` /
    `user.email` set there, and env vars take precedence over config
    so the orchestrator's identity stamps the merge commit cleanly.
    """
    git_prefix = [
        "git",
        "-c", "core.hooksPath=/dev/null",
        "-c", "credential.helper=",
        "-c", "core.fsmonitor=",
    ]
    env = {
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
    return subprocess.run(
        [*git_prefix, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


def _merge_base_into_worktree(
    spec: RepoSpec, worktree: Path
) -> Tuple[bool, list[str]]:
    """Run `git merge --no-edit origin/<base>` in the worktree.

    Returns `(succeeded, conflicted_files)`. On success, `conflicted_files`
    is empty -- whether the merge fast-forwarded (no commit) or produced a
    merge commit is the caller's job to detect via the HEAD-SHA delta.
    On failure, the conflicted-file list is the unmerged paths from
    `git diff --name-only --diff-filter=U`; an empty list means the merge
    failed for a non-conflict reason (hooks, permissions, etc.) and the
    caller should park rather than ask the agent to resolve nothing.

    Both subprocess calls run under `_git_hardened`: the diff is
    read-only but still executes inside an agent-writable worktree, so
    a planted hooksPath / fsmonitor would otherwise execute attacker
    code under the orchestrator's UID at diff time.
    """
    r = _git_hardened(
        "merge", "--no-edit",
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


def _authed_fetch(
    spec: RepoSpec, refspec: str, *, cwd: Path
) -> subprocess.CompletedProcess:
    """Authenticated, hardened `git fetch` -- the same security envelope as
    `_push_branch`.

    Used for fetches from inside an agent-writable worktree where any
    of the following vectors could leak GIT_TOKEN to an attacker host:
      * a planted credential helper in the worktree's `.git/config`,
      * a planted `core.hooksPath` / `core.fsmonitor` that runs an
        attacker-controlled binary with GIT_TOKEN in env,
      * a planted `url.<host>.insteadOf` rewrite in the worktree's
        local config OR in `~/.gitconfig` redirecting fetch to an
        attacker-controlled host.

    The auth URL carries only the username (`x-access-token`); the
    token itself is read from $GIT_TOKEN by a tempfile askpass script
    so it never appears in argv. Global/system git config is detached
    via `GIT_CONFIG_GLOBAL=/dev/null` / `GIT_CONFIG_SYSTEM=/dev/null`
    so url-rewrite rules planted there cannot apply. We also refuse
    to run if the worktree's local config already carries any url
    rewrite rule, mirroring `_push_branch`'s pre-flight check.

    `refspec` is the fetch refspec; pass an explicit form like
    `+refs/heads/<branch>:refs/remotes/origin/<branch>` so single-branch
    clones still update the remote-tracking ref instead of leaving the
    fetched payload only in FETCH_HEAD.
    """
    # Resolve the token from `spec.slug` rather than the cached
    # `config.GITHUB_TOKEN` (which was looked up once for `config.REPO`),
    # so a multi-repo deployment with one token file per slug under
    # `~/.config/<owner>/<repo>/token` fetches with the right repo's token.
    # Mirrors `_push_branch`'s per-spec token resolution; without this,
    # `_handle_resolving_conflict` would fail conflict resolution for any
    # repo other than the legacy `REPO` (or use the wrong token).
    token = config._resolve_github_token(spec.slug)
    if not token:
        log.error("GITHUB_TOKEN missing for %s; cannot fetch", spec.slug)
        return subprocess.CompletedProcess(
            args=["git", "fetch"], returncode=1, stdout="",
            stderr="GITHUB_TOKEN missing",
        )
    rewrite = subprocess.run(
        ["git", "config", "--local", "--get-regexp",
         r"^url\..*\.(insteadof|pushinsteadof)$"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if rewrite.returncode == 0 and rewrite.stdout.strip():
        log.error(
            "refusing to fetch into %s: worktree .git/config has url "
            "rewrite rules: %s", cwd, rewrite.stdout.strip(),
        )
        return subprocess.CompletedProcess(
            args=["git", "fetch"], returncode=1, stdout="",
            stderr="url rewrite rules in worktree .git/config",
        )
    auth_url = f"https://x-access-token@github.com/{spec.slug}.git"
    with tempfile.TemporaryDirectory(prefix="orch-askpass-") as td:
        askpass = Path(td) / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf %s "$GIT_TOKEN"\n')
        askpass.chmod(0o700)
        env = {
            **os.environ,
            **_GIT_NO_PROMPT_ENV,
            "GIT_ASKPASS": str(askpass),
            "GIT_TOKEN": token,
            "GIT_AUTHOR_NAME": config.AGENT_GIT_NAME,
            "GIT_AUTHOR_EMAIL": config.AGENT_GIT_EMAIL,
            "GIT_COMMITTER_NAME": config.AGENT_GIT_NAME,
            "GIT_COMMITTER_EMAIL": config.AGENT_GIT_EMAIL,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        git_prefix = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "core.fsmonitor=",
        ]
        return subprocess.run(
            [*git_prefix, "fetch", "--quiet", auth_url, refspec],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
        )


def _refresh_base_and_worktrees(gh: GitHubClient, spec: RepoSpec) -> None:
    """Fetch `origin/<base>` once for the spec and bring every existing
    per-issue worktree up to date.

    Runs at the start of each tick so a base-branch update on the remote
    propagates into in-flight issue worktrees without waiting for an
    AUTO_MERGE mergeability check. The per-stage `_ensure_*_worktree`
    helpers only fetch base on (re)creation, so a worktree that survives
    across ticks would otherwise stay anchored at whatever `origin/<base>`
    looked like when it was first added.

    Two paths depending on whether a PR already exists for the issue:

    * **Pre-PR worktrees** (no `pr_number` in pinned state): merge
      `origin/<base>` directly into the local worktree -- no remote yet,
      so a local-only merge commit is the right outcome and there is
      nothing to push.

    * **PR-having worktrees** (validating / in_review): merging locally
      WITHOUT pushing would diverge local HEAD from `pr.head.sha` and
      break the validating reviewer (it reads local HEAD, so it would
      snapshot `agent_approved_sha` to a SHA that isn't on the PR),
      `_squash_and_force_push`'s `--force-with-lease=<original_head>`
      (the lease compares against the un-merged remote tip), and
      AUTO_MERGE's `agent_approved_sha == pr.head.sha` gate. So instead
      we route the issue to `resolving_conflict`: the existing handler
      does the merge, pushes, and flips back to `validating` so the
      reviewer re-runs on the merged head. Applying the `hold_base_sync`
      label to an issue pauses both the pre-PR local merge and the PR
      detour until the label is removed. This works under
      `AUTO_MERGE=off` too -- `_handle_resolving_conflict` never reads
      AUTO_MERGE, it just does the merge+push+relabel cycle. Issues
      already labeled `resolving_conflict` are left alone (the handler
      runs this tick anyway); other labels are skipped (no PR worktree
      to refresh in those states).

    Merge over rebase is the codebase's standing contract (see
    `_handle_resolving_conflict`'s docstring): rebase rewrites every
    commit's SHA, which would invalidate any stored `agent_approved_sha`
    in surprising ways and force the reviewer to re-approve the entire
    branch even when only the base content changed.

    Conflicts on the pre-PR path abort the merge so the worktree stays
    on its original SHA -- conflict resolution still belongs to
    `_handle_resolving_conflict`. Dirty worktrees are skipped so a
    crash-recovered tree with uncommitted edits is never disturbed
    (mirrors `_on_dirty_worktree`'s rule). All failures are logged at
    info/warning and swallowed: keeping every issue moving matters more
    than perfect base sync.
    """
    fetch_r = _git(
        "fetch", "--quiet", spec.remote_name, spec.base_branch,
        cwd=spec.target_root,
    )
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
        try:
            _sync_worktree_with_base(gh, spec, wt, issue_number)
        except Exception:
            log.exception(
                "repo=%s issue=#%d base sync failed; continuing",
                spec.slug, issue_number,
            )


# Workflow labels the pre-tick refresh is willing to detour into
# `resolving_conflict` when the PR worktree is behind base. Validating and
# in_review are the long-lived PR-stage labels: validating may run the
# reviewer again, in_review is parked waiting for AUTO_MERGE / human merge.
# `resolving_conflict` itself is excluded -- the handler runs this tick
# regardless and will do the merge anyway. Other labels mean either no PR
# yet (pre-PR path applies instead) or terminal (done/rejected, nothing to
# refresh).
_PR_REFRESH_DETOUR_LABELS = frozenset({"validating", "in_review"})


def _sync_worktree_with_base(
    gh: GitHubClient, spec: RepoSpec, worktree: Path, issue_number: int,
) -> None:
    """Bring a single per-issue worktree up to date with `origin/<base>`.

    Pre-PR: merge `origin/<base>` directly. PR-having + behind base +
    label in {validating, in_review}: detour the issue to
    `resolving_conflict` so the existing handler does merge + push +
    relabel-to-validating in one consistent flow. Skips a dirty worktree
    or a worktree already up to date (no pre-PR merge attempted, no PR
    detour fired). On a pre-PR content conflict, aborts the merge so the
    worktree stays on its pre-merge SHA -- conflict resolution lives in
    `_handle_resolving_conflict`, not here.
    """
    try:
        issue = gh.get_issue(issue_number)
    except Exception:
        log.debug(
            "issue=#%d not retrievable; skipping base sync", issue_number,
        )
        return
    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        log.debug(
            "issue=#%d has %r; skipping base sync",
            issue_number, BASE_SYNC_HOLD_LABEL,
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

    succeeded, conflicted = _merge_base_into_worktree(spec, worktree)
    if succeeded:
        log.info(
            "issue=#%d merged %s into worktree (was %d commit(s) behind)",
            issue_number, base_ref, behind,
        )
        return

    abort = _git_hardened("merge", "--abort", cwd=worktree)
    if abort.returncode != 0:
        log.warning(
            "issue=#%d base merge failed and abort failed: %s",
            issue_number, (abort.stderr or "").strip(),
        )
    if conflicted:
        log.info(
            "issue=#%d base merge has %d conflict(s); aborted -- "
            "resolving_conflict will handle it once a PR exists",
            issue_number, len(conflicted),
        )
    else:
        log.warning(
            "issue=#%d base merge failed without conflicted files; aborted",
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
    then runs `git merge origin/<base>` in the worktree, pushes, and
    relabels to `validating` so the reviewer re-runs on the merged head
    -- the only safe pattern for PR-having worktrees, since a local-only
    merge commit would diverge local HEAD from `pr.head.sha` and break
    every downstream gate that compares the two. Works under both
    AUTO_MERGE on (replaces the unmergeable trigger) and AUTO_MERGE off
    (the only auto-rebase path under that mode -- `_handle_in_review`'s
    AUTO_MERGE-off path otherwise just sits in `in_review`).

    Skips the detour when:

    * The label is not one this refresh knows how to drive into
      `resolving_conflict` (only `validating` / `in_review`); the
      `resolving_conflict` label itself is also skipped because the
      handler runs this tick anyway and will do the merge regardless.

    * `awaiting_human=True`. `_handle_resolving_conflict`'s awaiting-human
      branch returns early without merging unless a new human comment
      arrived; relabeling here would just hide the existing park behind a
      `resolving_conflict` label without making any progress, including
      the documented `AUTO_MERGE=off` unmergeable park path. The park
      already invites the human to comment, and once they do, the existing
      handler's resume-on-human-reply branch picks the work up. Auto-
      unparking here would also undermine `_handle_validating`'s
      `MAX_REVIEW_ROUNDS` / `_handle_resolving_conflict`'s
      `MAX_CONFLICT_ROUNDS` caps, which exist precisely to require human
      intervention after repeated failures.

    * The issue has `hold_base_sync`, which is an explicit operator hold for
      series work where base should be merged once after prerequisite PRs
      land, not after every intermediate base advance.

    * The PR is no longer open. A merged PR advances `origin/<base>`, so
      the still-validating / still-in_review worktree pointed at the now-
      stale branch is naturally behind base; without this gate the detour
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
        "resolving_conflict so the handler can merge, push, and re-review",
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
            "orchestrator is attempting auto-resolution by merging it into "
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
