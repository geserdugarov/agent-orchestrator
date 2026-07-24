# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Verify models."""
from __future__ import annotations

from orchestrator import verify as _owner

Optional = _owner.Optional
Path = _owner.Path
dataclass = _owner.dataclass


def _head_sha(worktree: Path) -> str:
    """HEAD commit SHA of the worktree, or '' if it cannot be read.

    Used by the validating handler to detect whether a dev-fix codex run
    produced a new commit. _has_new_commits compares against origin/<base>,
    which is already true throughout validating, so we need an absolute SHA
    snapshot instead.
    """
    head_result = _owner._git("rev-parse", "HEAD", cwd=worktree)
    if head_result.returncode != 0:
        return ""
    return (head_result.stdout or "").strip()


def _worktree_dirty_files(worktree: Path) -> list[str]:
    """Paths git considers modified or untracked in the worktree.

    Used to refuse opening a PR when codex committed only part of its work and
    left other modifications behind -- the push would publish an incomplete
    branch. The orchestrator's own scratch (codex's `-o` file) lives outside
    the worktree (a per-spawn tempfile in `codex.run_codex`), so it never
    surfaces here regardless of the target repo's .gitignore.

    Hardened unconditionally: `git status --porcelain` refreshes the index,
    which spawns a configured `core.fsmonitor` helper -- and the agent can
    plant one in the worktree's `.git/config` or in `~/.gitconfig` (same OS
    user), so a plain probe would execute it with the orchestrator's process
    environment (ambient secrets) attached. Every call site is an
    agent-writable worktree, so there is no trusted caller that would want
    the unhardened form. Detaching global/system config also drops a global
    `core.excludesFile` from the untracked filter; the repo's own tracked
    `.gitignore` still applies, which is the intended trust boundary.
    """
    status_result = _owner._git_hardened("status", "--porcelain", cwd=worktree)
    if status_result.returncode != 0:
        return []
    paths: list[str] = []
    for line in (status_result.stdout or "").splitlines():
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
                            to in_review would advertise the PR as ready for
                            human merge with state the dev never committed.
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
