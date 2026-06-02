# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Git, branch, and worktree plumbing shared by stage handlers.

Centralizes every direct shell-out to `git` plus the per-spec worktree
layout helpers. Stage handlers live under `orchestrator/stages/`
(decomposition.py, implementing.py, documenting.py, validating.py,
in_review.py, fixing.py, conflicts.py, question.py) and drive the
state machine; the compatibility facade in `workflow.py` re-exports
each helper below under its original name for backward compatibility
with direct test references and `patch.object(workflow, ...)` patches.
This module owns:

* Branch naming and slug-safe per-repo worktree paths.
* Worktree creation, restoration, and cleanup for both the implementer
  and decomposer roles, plus terminal-state cleanup.
* Hardened git invocations (`_git`, `_git_hardened`) and authenticated
  fetch/push helpers (`_authed_fetch`, `_push_branch`) that keep the
  GitHub PAT off `argv` and detach from any agent-writable git config.
* Squash-on-approval and per-tick base refresh / per-worktree base sync.

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
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue

from . import config
from .agents import _filter_agent_env
from .config import RepoSpec
from .github import (
    BACKLOG_LABEL,
    BASE_SYNC_HOLD_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)
from .workflow_messages import _post_pr_comment, _redact_secrets

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


# Per-target_root locks that serialize git plumbing against the parent
# clone. `git worktree add` / `worktree remove` / `branch -D` / authenticated
# `fetch` all write to the parent repo's `.git/config` and grab its
# `.git/config.lock`. Two `workflow.tick` worker threads driving
# `_ensure_worktree` against the same `spec.target_root` can race that
# lock file and surface as `error: could not lock config file .git/config:
# File exists`, failing the worker before the agent even spawns. The
# long-running agent work itself runs in the per-issue worktree (with its
# own per-worktree config under `<git-dir>/worktrees/<name>/`) so we
# release the lock as soon as the plumbing finishes -- agents stay
# concurrent, only the parent-repo writes are serialized.
#
# Locks are keyed by the string form of `spec.target_root` so two
# `RepoSpec` instances pointing at the same on-disk clone share a lock
# (the case `_run_tick` produces when one Python process drives several
# RepoSpec entries that happen to point at the same target_root for
# operator convenience). `_TARGET_ROOT_LOCKS_LOCK` only guards the dict
# lookup/insert; the per-key lock is acquired outside that guard so a
# slow git operation can't block lookup for other repos.
#
# Per-key locks are `RLock` so a caller that already holds the lock can
# re-enter via a helper that also acquires it -- specifically,
# `_authed_target_fetch` acquires the lock internally to keep its
# critical section honest in isolation, and the worktree creators
# (`_ensure_worktree` / `_ensure_pr_worktree` / `_ensure_decompose_worktree`)
# also hold the lock for the whole add sequence. Cross-thread serialization
# is unchanged.
_TARGET_ROOT_LOCKS_LOCK = threading.Lock()
_TARGET_ROOT_LOCKS: dict[str, threading.RLock] = {}


def _target_root_lock(target_root: Path) -> threading.RLock:
    """Return the lock that serializes git plumbing against `target_root`.

    Created lazily on first use so a single-repo deployment never pays
    for a lock it doesn't need. The dict is process-global; clearing it
    is a test-only concern handled inline (no public API). Re-entrant
    so a caller already holding the lock can call into a helper that
    also acquires it.
    """
    key = str(target_root)
    with _TARGET_ROOT_LOCKS_LOCK:
        lock = _TARGET_ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _TARGET_ROOT_LOCKS[key] = lock
        return lock


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


# Trim long verify command output to a budget compatible with GitHub's
# issue body limit -- a chatty test runner can otherwise overflow the
# park comment. Matches the stderr-tail budget used by
# `_format_stderr_diagnostics` so both surfaces enforce the same cap.
_VERIFY_OUTPUT_BUDGET = 4096


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of running the configured `VERIFY_COMMANDS`.

    `status` is one of:

    * ``"ok"``           -- every command exited 0 and the worktree was clean.
    * ``"failed"``       -- a command exited non-zero.
    * ``"timeout"``      -- a command hit the per-command wall-clock cap.
    * ``"dirty"``        -- every command exited 0 but the worktree carried
                            uncommitted changes afterwards; treated as a
                            verify failure because handing off a dirty tree
                            to in_review would let AUTO_MERGE land state the
                            dev never committed.
    * ``"head_changed"`` -- a command moved `HEAD` (it ran `git commit` or
                            `git reset` etc.) while leaving the tree clean.
                            Treated as a verify failure because the squash-
                            on-approval + force-push that follows would
                            otherwise publish an unreviewed verify-created
                            commit. `head_before` / `head_after` record the
                            SHAs so the operator can identify which commit
                            the verify produced.

    The non-ok fields (`command`, `exit_code`, `output`, `dirty_files`,
    `head_before` / `head_after`) are populated only for the case they
    describe and are otherwise None / empty so the formatter does not
    have to know the variant.

    `output` is already redacted (via `_redact_secrets`) AND truncated to
    `_VERIFY_OUTPUT_BUDGET` bytes -- callers can post it verbatim. The
    redact pass runs before truncation so a secret straddling the cut
    cannot leak a partial value (see `_truncate_verify_output`).
    """

    status: str
    command: Optional[str] = None
    exit_code: Optional[int] = None
    output: str = ""
    dirty_files: tuple[str, ...] = ()
    head_before: Optional[str] = None
    head_after: Optional[str] = None


def _run_verify_commands(
    worktree: Path,
    commands: tuple[str, ...],
    timeout: int,
) -> VerifyResult:
    """Run each command sequentially in `worktree` with a bounded timeout.

    Empty `commands` (the default) short-circuits to ``status="ok"`` so the
    legacy "no verification" behaviour is a single boolean check at the
    call site. Commands are spawned via the shell so quoting / pipes /
    `&&` work the way an operator would type them; stdout and stderr are
    merged so a failing build with all its diagnostics on stderr surfaces
    in one block in the park comment. The shell runs with a child
    environment stripped of GitHub credentials, production-secret-shaped
    variables, AND the agent's own provider-auth keys (`_filter_agent_env`
    with `allow_provider_auth=False`) -- stricter than the agent-subprocess
    strip, because a verify command is operator-configured shell that
    executes the agent-produced code and a hostile dependency reading
    `$ANTHROPIC_API_KEY` would gain billable access to the operator's
    model account.

    The first non-zero exit, timeout, post-run dirty tree, or HEAD
    advance wins -- later commands are not run, since the gate is
    "everything passed" and the operator only needs the first failure
    to triage. Dirtiness and HEAD-movement are checked AFTER EACH
    command so a failure can be attributed to the actual command that
    caused it, with that command's captured stdout/stderr preserved in
    `output` for the park comment. The HEAD check guards against a
    verify command that `git commit`s its own fixups: without it, a
    clean tree + zero exit would look like `ok`, and the squash-on-
    approval + force-push that follows would publish an unreviewed
    verify-created commit.
    """
    if not commands:
        return VerifyResult(status="ok")
    # Snapshot HEAD so we can refuse any verify command that moves it.
    # An empty snapshot (an uninitialized repo or a `git rev-parse`
    # failure) means we cannot prove HEAD stability, so a later
    # commit-by-the-verify-command would look identical to the
    # missing baseline -- treat the unknown baseline as "" and accept
    # only an unchanged "" afterwards (which means no HEAD ever
    # existed). Anything else is a fail-closed park.
    head_before = _head_sha(worktree)
    # Strip GitHub credentials, production-secret-shaped variables,
    # write-credential locators (SSH-agent / askpass), AND the agent's
    # own provider-auth keys from the child environment. Verify commands
    # run operator-configured shell against code the agent just produced;
    # without this, a prompt-injected `pytest` plugin (or a hostile
    # dependency the agent pulled in) could read `$GITHUB_TOKEN` /
    # `$STRIPE_API_KEY` / `$ANTHROPIC_API_KEY` / `$SSH_AUTH_SOCK` / ...
    # straight out of the orchestrator's environment and exfiltrate or
    # push as the operator. `allow_provider_auth=False` is stricter than
    # the agent subprocess case: the agent CLI needs its provider key to
    # reach its model, but the verify shell does not. An operator who
    # legitimately needs a secret in a verify command must load it from
    # disk inside a wrapper script (`VERIFY_COMMANDS=./run-verify.sh`);
    # inline `KEY=value pytest ...` is unsafe because the failure park
    # comment publishes `verify.command` verbatim on the issue.
    child_env = _filter_agent_env(dict(os.environ), allow_provider_auth=False)
    for command in commands:
        # `start_new_session=True` puts the shell in its own process
        # group (and session) so a timeout-kill can tear down EVERY
        # descendant in one `killpg` call. Without this, the
        # `subprocess.run(..., shell=True, timeout=...)` shape only
        # SIGKILLs the shell; a `make -j` worker, a `pytest-xdist`
        # forker, or a backgrounded `&` subprocess survives the shell
        # and can keep mutating the worktree AFTER the orchestrator has
        # already posted `verify_timeout` and parked the issue. That
        # silently violates the bounded-timeout gate the operator
        # configured and can race the orchestrator's own next-tick
        # reads.
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(worktree),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=child_env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # SIGKILL the WHOLE group so the shell AND every descendant
            # die together. `os.getpgid(proc.pid)` reads the new group
            # id we just created; ProcessLookupError covers the narrow
            # race where the shell exited between TimeoutExpired and
            # this call (then there is nothing left to kill, which is
            # fine -- a survivor would still be inside the group and
            # caught had it existed).
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            # Drain whatever the shell wrote before it was killed. A
            # bounded second-stage timeout guards against a wedged pipe
            # (e.g. a descendant that escaped the group via its own
            # `setsid` and is still holding the fd open); the fallback
            # `proc.kill()` covers that hostile case.
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
            partial = stdout or ""
            if stderr:
                if partial and not partial.endswith("\n"):
                    partial += "\n"
                partial += stderr
            return VerifyResult(
                status="timeout",
                command=command,
                exit_code=None,
                output=_truncate_verify_output(partial),
            )
        combined = stdout or ""
        if stderr:
            if combined and not combined.endswith("\n"):
                combined += "\n"
            combined += stderr
        if proc.returncode != 0:
            return VerifyResult(
                status="failed",
                command=command,
                exit_code=proc.returncode,
                output=_truncate_verify_output(combined),
            )
        # Check dirtiness PER COMMAND so a dirty failure can be
        # attributed to the actual command that produced the
        # untracked/modified files and surface that command's stdout/
        # stderr in the park comment. A single end-of-loop check would
        # always blame `commands[-1]` even when an earlier command was
        # the cause, and would have already lost its captured output
        # by the time we got here.
        dirty = _worktree_dirty_files(worktree)
        if dirty:
            return VerifyResult(
                status="dirty",
                command=command,
                exit_code=proc.returncode,
                output=_truncate_verify_output(combined),
                dirty_files=tuple(dirty),
            )
        # HEAD-movement check. A verify command that `git commit`s its
        # own auto-fix leaves `git status` clean and exits 0 -- looking
        # identical to a passing gate -- yet the squash-on-approval +
        # force-push that follows would publish that unreviewed commit.
        # Fail the gate so the operator decides whether the auto-commit
        # belongs in the PR (re-spawn the reviewer) or should be
        # reverted before re-trying.
        head_after = _head_sha(worktree)
        if head_after != head_before:
            return VerifyResult(
                status="head_changed",
                command=command,
                exit_code=proc.returncode,
                output=_truncate_verify_output(combined),
                head_before=head_before,
                head_after=head_after,
            )
    return VerifyResult(status="ok")


def _truncate_verify_output(text: str) -> str:
    """Redact secrets, then keep the tail within `_VERIFY_OUTPUT_BUDGET`.

    Redaction MUST happen before the truncation. `_redact_secrets` does a
    full-string `str.replace(value, "***")` against each candidate env
    value; if the truncation cut sliced a secret in half first, the
    surviving partial would no longer match the replace and would leak
    verbatim in the park comment. Redacting first collapses any matched
    secret to `***` before its bytes can straddle the cut.

    The tail typically carries the actual failure (stack trace, assertion
    diff, linter summary); the head is build noise. Identical convention
    to `_format_stderr_diagnostics`.
    """
    if not text:
        return ""
    redacted = _redact_secrets(text)
    if len(redacted) <= _VERIFY_OUTPUT_BUDGET:
        return redacted
    return redacted[-_VERIFY_OUTPUT_BUDGET:]


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


def _git_hardened(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """`_git` plus the agent-hostile-environment hardening from `_push_branch`.

    Used for local git operations inside a worktree the agent can write to: a
    planted `core.hooksPath`, `core.fsmonitor`, or url rewrite rule in
    the worktree's `.git/config` (or in `~/.gitconfig`) would otherwise
    execute attacker code mid-operation or redirect a transient fetch to an
    attacker-controlled host. Drops global/system git config so url
    `insteadOf` rewrites and host-wide hooks cannot apply, and disables
    repo-local hooks / fsmonitor / credential helpers / commit signing via
    `-c` overrides. No askpass is wired in -- this helper is for local-only
    operations (rebase, diff, rev-parse); push remains the only call site
    that handles GIT_TOKEN.

    Injects `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env vars (matching the
    agent spawn's `_agent_env`) so a `git rebase` that needs to replay
    commits doesn't fail with "Committer identity unknown" -- stripping
    global config also strips any `user.name` / `user.email` set there,
    and env vars take precedence over config.
    """
    git_prefix = [
        "git",
        "-c", "core.hooksPath=/dev/null",
        "-c", "credential.helper=",
        "-c", "core.fsmonitor=",
        "-c", "commit.gpgsign=false",
        "-c", "rebase.autoStash=false",
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

    The fetch updates the parent clone's `refs/remotes/<remote>/...`
    namespace from inside an agent-writable worktree, which means it
    grabs the parent's ref-update lock under `<git-dir>/packed-refs.lock`
    and `<git-dir>/refs/remotes/<remote>/<branch>.lock`. Two concurrent
    `_authed_fetch` calls from different worktrees of the same
    `target_root` (the common shape during fan-out of multiple
    `resolving_conflict` issues) race those lock files and one fails
    with `Unable to create '...': File exists.`, parking the issue.
    The actual subprocess call is therefore held under the
    per-target_root lock; the pre-flight URL-rewrite check stays
    outside the lock since it only reads the worktree's own
    `.git/config`.
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
        with _target_root_lock(spec.target_root):
            return subprocess.run(
                [*git_prefix, "fetch", "--quiet", auth_url, refspec],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=env,
            )


def _authed_target_fetch(
    spec: RepoSpec, branch: str
) -> subprocess.CompletedProcess:
    """Authed `git fetch` into `spec.target_root` using the per-spec token.

    Replaces the plain `git fetch <remote_name> <branch>` invocations the
    worktree creators (`_ensure_worktree` / `_ensure_pr_worktree` /
    `_ensure_decompose_worktree`) and the per-tick base refresh
    (`_refresh_base_and_worktrees`) used to run. The plain form relied on
    git's ambient credential helper or session state, which fails under
    systemd (`GIT_TERMINAL_PROMPT=0` disables the fallback prompt) and
    has no way to pick a per-repo token when the local clone has several
    GitHub-pointing remotes whose `slug` differs from the
    `~/.config/<owner>/<repo>/token` of the configured `REPO`.

    The `spec.remote_name` field selects the local remote namespace --
    refs land under `refs/remotes/<spec.remote_name>/<branch>` -- while
    `spec.slug` selects which GitHub repo / token to authenticate with.
    Without this split, a `REPOS` row like
    `geserdugarov/lance-private|...|private-cache|private` would try to
    use the cached single-repo `config.GITHUB_TOKEN` (looked up once for
    `config.REPO`) and fail to fetch even with a correct per-spec token
    file in place.

    An explicit refspec `+refs/heads/<branch>:refs/remotes/<remote_name>/<branch>`
    is used so single-branch / narrowed clones still update the
    remote-tracking ref instead of leaving the fetched payload only in
    FETCH_HEAD -- the worktree creators then anchor `git worktree add`
    on `<remote>/<branch>` without surprise.

    Same security envelope as `_push_branch` / `_authed_fetch`: token
    delivered via GIT_ASKPASS (never argv), global/system git config
    detached so url-rewrite rules planted in `~/.gitconfig` cannot
    redirect the fetch to an attacker-controlled host, hooks /
    fsmonitor / credential helpers blocked via `-c` overrides. The
    target_root is normally operator-owned, but a linked worktree
    (which the agent does write) can still mutate the parent clone's
    local config via `git config --local`, and local config still
    applies even with GIT_CONFIG_GLOBAL/SYSTEM detached. Mirror the
    `_authed_fetch` / `_push_branch` pre-flight refusal: bail out if
    `target_root`'s local config carries any
    `url.<host>.(insteadOf|pushInsteadOf)` rule that could redirect
    the token-bearing fetch to an attacker-controlled host.

    Serialized via `_target_root_lock` (`RLock` so a caller already
    holding it -- the worktree creators -- re-enters cleanly) for the
    same `.git/config.lock` reason described on `_ensure_worktree`.
    """
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
        cwd=str(spec.target_root), capture_output=True, text=True,
    )
    if rewrite.returncode == 0 and rewrite.stdout.strip():
        log.error(
            "refusing to fetch into %s: target_root .git/config has url "
            "rewrite rules: %s", spec.target_root, rewrite.stdout.strip(),
        )
        return subprocess.CompletedProcess(
            args=["git", "fetch"], returncode=1, stdout="",
            stderr="url rewrite rules in target_root .git/config",
        )
    auth_url = f"https://x-access-token@github.com/{spec.slug}.git"
    refspec = (
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}"
    )
    with tempfile.TemporaryDirectory(prefix="orch-askpass-") as td:
        askpass = Path(td) / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf %s "$GIT_TOKEN"\n')
        askpass.chmod(0o700)
        env = {
            **os.environ,
            **_GIT_NO_PROMPT_ENV,
            "GIT_ASKPASS": str(askpass),
            "GIT_TOKEN": token,
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
        with _target_root_lock(spec.target_root):
            return subprocess.run(
                [*git_prefix, "fetch", "--quiet", auth_url, refspec],
                cwd=str(spec.target_root),
                capture_output=True,
                text=True,
                env=env,
            )


def _refresh_base_and_worktrees(gh: GitHubClient, spec: RepoSpec) -> None:
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
# for AUTO_MERGE / human merge, and fixing is between in_review and
# validating while a PR feedback round is being addressed. Documenting
# only checks ahead/behind vs. the PR branch (not the base) itself, so
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
    two. Works under both AUTO_MERGE on (replaces the unmergeable
    trigger) and AUTO_MERGE off (the only auto-rebase path under that
    mode -- `_handle_in_review`'s AUTO_MERGE-off path otherwise just
    sits in `in_review`).

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
      the documented `AUTO_MERGE=off` unmergeable park path. The park
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
