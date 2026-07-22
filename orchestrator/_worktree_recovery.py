# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree recovery."""
from __future__ import annotations

from orchestrator import worktree_lifecycle as _owner

Optional = _owner.Optional
config = _owner.config


def _branch_has_unpushed_commits(
    spec: config.RepoSpec, issue_number: int,
) -> Optional[str]:
    """Return the per-issue branch carrying unpushed commits, or None.

    Probes BOTH the slug-namespaced branch and the legacy
    `orchestrator/issue-<n>` form. A pre-slug-namespacing
    `question_commits` park (or any in-flight question worktree
    created before this code shipped) holds its commits on the
    legacy ref and never persisted `state["branch"]`, so probing
    only the slug-namespaced form would let the
    `_handle_implementing` question-relabel guard clear the park,
    `_ensure_worktree` reuse the on-disk worktree (still checked
    out on the legacy branch), and the recovered-worktree
    shortcut push those read-only question commits through as
    fresh dev work. Returning the offending branch lets the
    caller name it in the operator message so the cleanup hint
    (`git branch -D <name>`) points at the right ref.

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

    Returns None when:

    * neither candidate branch exists (no state to inspect);
    * a candidate branch exists at exactly `<remote>/<base>` (a
      fresh-from-base reset) AND no other candidate carries
      commits;
    * the `rev-list` itself errors (transient git failure -- the
      caller's later steps will surface the underlying problem if
      it persists).

    Returns the name of the first candidate branch that has at
    least one commit not in `<remote>/<base>`, which is the exact
    condition the recovered-worktree shortcut would treat as
    "unpushed dev work" -- the read-only-violation we are trying
    to prevent.

    Serialized via `_target_root_lock` for the same
    `.git/config.lock` reason described on `_ensure_worktree`;
    `RLock` re-entry keeps callers that already hold the lock
    safe.
    """
    candidates = _owner._candidate_issue_branches(spec, issue_number)
    base_ref = f"refs/remotes/{spec.remote_name}/{spec.base_branch}"
    with _owner._target_root_lock(spec.target_root):
        for branch in candidates:
            count = _owner._branch_commit_count(spec, branch, base_ref)
            if count > 0:
                return branch
    return None


def _candidate_issue_branches(
    spec: config.RepoSpec, issue_number: int,
) -> tuple[str, ...]:
    """Return namespaced then legacy branch candidates without duplicates."""
    namespaced = _owner._branch_name(spec, issue_number)
    legacy = f"orchestrator/issue-{issue_number}"
    if legacy == namespaced:
        return (namespaced,)
    return namespaced, legacy


def _branch_commit_count(
    spec: config.RepoSpec, branch: str, base_ref: str,
) -> int:
    """Return commits unique to a local branch, or zero on probe failure."""
    local_ref = f"refs/heads/{branch}"
    have_local = _owner._git(
        "rev-parse", "--verify", "--quiet", local_ref,
        cwd=spec.target_root,
    ).returncode == 0
    if not have_local:
        return 0
    commit_count_result = _owner._git(
        "rev-list", "--count", f"{base_ref}..{local_ref}",
        cwd=spec.target_root,
    )
    if commit_count_result.returncode != 0:
        return 0
    try:
        return _owner._commit_count_from_stdout(commit_count_result)
    except ValueError:
        return 0
