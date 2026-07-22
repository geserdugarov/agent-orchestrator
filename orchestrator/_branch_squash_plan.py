# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Branch squash plan."""
from __future__ import annotations

from orchestrator import branch_publication as _owner

Issue = _owner.Issue
Path = _owner.Path
config = _owner.config
dataclass = _owner.dataclass


class _SquashPreparationError(RuntimeError):
    """A pre-rewrite probe failed while the original branch was intact."""


@dataclass(frozen=True)
class _SquashPlan:
    """Inputs that remain stable across the destructive squash rewrite."""

    base_sha: str
    original_head: str
    subjects: tuple[str, ...]
    message: str


def _squash_base_sha(spec: config.RepoSpec, worktree: Path) -> str:
    """Return the topic branch merge base or raise a preparation error."""
    base_ref = f"{spec.remote_name}/{spec.base_branch}"
    merge_base_result = _owner._git("merge-base", base_ref, "HEAD", cwd=worktree)
    if merge_base_result.returncode != 0:
        detail = (merge_base_result.stderr or "").strip()
        raise _SquashPreparationError(f"merge-base failed: {detail}")
    base_sha = (merge_base_result.stdout or "").strip()
    if not base_sha:
        raise _SquashPreparationError("merge-base returned empty")
    return base_sha


def _squash_subjects(worktree: Path, base_sha: str) -> tuple[str, ...]:
    """Return ordered topic-commit subjects or raise on an unreadable log."""
    log_result = _owner._git(
        "log", "--reverse", "--pretty=%s", f"{base_sha}..HEAD",
        cwd=worktree,
    )
    if log_result.returncode != 0:
        detail = (log_result.stderr or "").strip()
        raise _SquashPreparationError(f"git log failed: {detail}")
    return tuple(
        output_line
        for output_line in (log_result.stdout or "").splitlines()
        if output_line.strip()
    )


def _squash_message(
    spec: config.RepoSpec,
    worktree: Path,
    issue: Issue,
    subjects: tuple[str, ...],
) -> str:
    """Build the subject-only message for a multi-commit squash."""
    first_subject = subjects[0]
    if _owner._is_prefixed_subject(first_subject):
        return f"{first_subject}\n"
    fallback_prefix = _owner._infer_subject_prefix(spec, worktree, issue)
    subject = _owner._pr_title_from_commit_or_issue(
        issue, first_subject, fallback_prefix,
    )
    return f"{subject}\n"


def _prepare_squash(
    spec: config.RepoSpec, worktree: Path, issue: Issue,
) -> _SquashPlan:
    """Collect every precondition before the branch rewrite begins."""
    base_sha = _owner._squash_base_sha(spec, worktree)
    original_head = _owner._head_sha(worktree)
    if not original_head:
        raise _SquashPreparationError("could not read original HEAD")
    if _owner._worktree_dirty_files(worktree):
        raise _SquashPreparationError("worktree has uncommitted changes")
    subjects = _owner._squash_subjects(worktree, base_sha)
    message = (
        _owner._squash_message(spec, worktree, issue, subjects)
        if len(subjects) > 1
        else ""
    )
    return _SquashPlan(base_sha, original_head, subjects, message)
