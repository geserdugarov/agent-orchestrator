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
parent clone's `.git/config.lock`. The PR branch publication helpers
(`_CONVENTIONAL_RE`, `_is_conventional_subject`, `_is_prefixed_subject`,
`_first_commit_subject`, `_recent_base_subjects`,
`_infer_subject_prefix`, `_pr_title_from_commit_or_issue`,
`_branch_ahead_behind`, `_squash_and_force_push`) live in
`branch_publication.py`; the local-verify runner and its
worktree-state probes (`VerifyResult`, `_run_verify_commands`,
`_truncate_verify_output`, `_head_sha`, `_worktree_dirty_files`) live
in `verify.py`; the per-tick base refresh and rebase routing
(`_rebase_base_into_worktree`, `_merge_base_into_worktree`,
`_rebase_in_progress`, `_refresh_base_and_worktrees`,
`_PR_REFRESH_DETOUR_LABELS`, `_sync_worktree_with_base`,
`_sync_pr_worktree_to_base`, `_route_pr_worktree_to_resolving_conflict`)
live in `base_sync.py`.
All those modules' names are re-exported from `worktrees.py`.
`worktrees.py` also re-exports every name below under its original
name so existing imports and
`patch.object(worktrees, "_foo", ...)` test patches keep working.

Each helper preserves the existing security hardening and crash-recovery
semantics; downstream behavior is unchanged by this extraction. Helpers
remain prefixed with `_` because they are module-internal contracts --
the public surface (the dispatcher entry points and the stage handlers
they route to) still lives in `workflow.py` and `orchestrator/stages/`.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from orchestrator import config
from orchestrator.git_plumbing import _authed_target_fetch, _git, _target_root_lock
from orchestrator.github import GitHubClient, PinnedState

log = logging.getLogger(__name__)


# Allowed characters in a worktree directory segment: alphanumerics plus
# `_`, `.`, `-`. `/` is excluded so the slug stays a single path segment;
# anything else is replaced with `_`. A leading `.` is also escaped so the
# per-repo subdir is never a hidden directory.
_SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
# Character every filesystem-unsafe slug byte (and an empty slug) collapses
# to when building a worktree path segment.
_SAFE_CHAR = "_"
# Hex length of the short SHA-1 digest used for lossy ref rewrites.
_SLUG_DIGEST_LEN = 16


def _sanitize_slug(slug: str) -> str:
    """Turn an `owner/name` repo slug into a single filesystem-safe segment.

    `/` collapses to `__` so two repos with the same issue number cannot
    share a worktree path. Any other character outside `[A-Za-z0-9_.-]`
    becomes `_`. A leading `.` is escaped to `_.` so the per-repo subdir
    is never a dotfile-hidden directory. An empty/all-stripped input
    falls back to `_` rather than collapsing into the bare WORKTREES_DIR.
    """
    cleaned = _SLUG_SAFE_RE.sub(_SAFE_CHAR, (slug or "").replace("/", "__"))
    if not cleaned:
        return _SAFE_CHAR
    if cleaned.startswith("."):
        cleaned = _SAFE_CHAR + cleaned
    return cleaned


def _slug_digest(slug: str) -> str:
    """Return the short content digest used for lossy ref rewrites."""
    encoded_slug = (slug or "").encode("utf-8")
    slug_hash = hashlib.sha1(encoded_slug)
    return slug_hash.hexdigest()[:_SLUG_DIGEST_LEN]


def _sanitize_branch_segment(slug: str) -> str:
    """Make a slug safe for use as a single git branch-name segment.

    Layers on top of `_sanitize_slug` (filesystem-safe single segment)
    the additional `git check-ref-format` rules so a branch like
    `orchestrator/<segment>/issue-N` is accepted by git. Without this,
    a configured `REPOS` slug whose name contains `.lock`, `..`, or a
    trailing `.` would yield a ref name git rejects, breaking every
    push for that repo before the PR even exists -- the
    filesystem-only sanitizer happily produces `owner__foo.lock` /
    `owner__foo..bar` / `owner__foo.` even though `git
    check-ref-format` flags them.

    Rules applied beyond `_sanitize_slug`:

    * Collapse any run of two or more dots to a single `_` so the
      segment never carries the forbidden `..` sequence. A single
      `.` mid-segment is allowed by git and left alone.
    * Replace a trailing `.lock` with `_lock` -- git rejects any
      slash-separated component ending in `.lock` (reserved for
      git's own lock files).
    * Replace any trailing `.` with `_` -- git rejects refs ending
      in `.`.

    Any of those three rewrites is information-lossy: `foo.lock` and
    `foo_lock` would both collapse to `foo_lock`, so two `REPOS`
    entries sharing a `target_root` could still collide on the same
    branch and defeat slug-namespacing. To stay injective, whenever
    the ref-only rewrites change the filesystem-safe form, the
    segment carries a `__h<digest>` suffix derived from the
    untransformed slug. Distinct slugs therefore always hash to
    distinct branches; the suffix only appears on the rare
    pathological inputs, so common slugs keep their readable
    `<owner>__<name>` form. (The hash is 64 bits; an exact-match
    collision would require an attacker-crafted REPOS entry, which
    is not in our threat model.)

    Path layout (`_repo_worktrees_root`) keeps the filesystem-only
    `_sanitize_slug` because directory names tolerate `.lock` /
    trailing-dot just fine; the branch segment is the stricter
    surface, so it gets its own sanitizer rather than tightening the
    filesystem one and uglifying every common slug's worktree path.
    """
    sanitized_path = _sanitize_slug(slug)
    sanitized_segment = sanitized_path
    # `..` anywhere is forbidden. Collapse any run of dots to a single
    # `_` so a segment cannot smuggle the sequence past the trailing-
    # dot / `.lock` checks below.
    sanitized_segment = re.sub(r"\.{2,}", _SAFE_CHAR, sanitized_segment)
    # `.lock` suffix on a component is reserved by git.
    if sanitized_segment.endswith(".lock"):
        trimmed = sanitized_segment[: -len(".lock")]
        sanitized_segment = f"{trimmed}_lock"
    # Trailing `.` on a component is rejected. Loop so any
    # follow-on dot revealed by the trim is also handled.
    while sanitized_segment.endswith("."):
        sanitized_segment = sanitized_segment[:-1] + _SAFE_CHAR
    # Defensive fallbacks: the substitutions above could in principle
    # produce an empty / leading-dot string (e.g. an input of `.` or
    # `..` collapses far enough that the leading-dot escape from
    # `_sanitize_slug` no longer covers the result).
    if not sanitized_segment:
        sanitized_segment = _SAFE_CHAR
    if sanitized_segment.startswith("."):
        sanitized_segment = _SAFE_CHAR + sanitized_segment
    if sanitized_segment == sanitized_path:
        return sanitized_segment
    # Ref-only rewrites changed the filesystem form. Append a
    # content-derived hash of the ORIGINAL slug so two distinct
    # inputs that collapsed to the same `s` (e.g. `owner/foo.lock`
    # and `owner/foo_lock`) stay distinct on the branch ref --
    # without this, two `REPOS` entries sharing a `target_root`
    # would collide on the same branch and the slug-namespacing
    # fix would silently regress for those slug shapes.
    digest = _slug_digest(slug)
    return f"{sanitized_segment}__h{digest}"


def _branch_name(spec: config.RepoSpec, issue_number: int) -> str:
    """Per-issue branch name namespaced by the spec's git-ref-safe slug.

    Two RepoSpecs that share the same `target_root` (a single local clone
    with multiple remotes, e.g. `lance-open-source` and `lance-private`)
    would otherwise collide on `orchestrator/issue-<n>` because git
    refuses to check the same branch out in two worktrees of one repo.
    Including the sanitized slug keeps each spec's worktree on its own
    branch. The `orchestrator/` prefix is preserved so
    `_cleanup_terminal_branch`'s "orchestrator-owned namespace"
    invariant still holds.

    Uses `_sanitize_branch_segment` rather than the filesystem-only
    `_sanitize_slug` so a slug like `owner/foo.lock` or
    `owner/foo..bar` does not produce a branch name `git
    check-ref-format` rejects.
    """
    return (
        f"orchestrator/{_sanitize_branch_segment(spec.slug)}"
        f"/issue-{issue_number}"
    )


def _resolve_branch_name(
    state: PinnedState, spec: config.RepoSpec, issue_number: int,
) -> str:
    """Branch to use for this issue, preferring an already-pinned value.

    Issues that were already in flight when slug-namespacing landed
    have a live PR open against the legacy `orchestrator/issue-<n>`
    ref. If we recomputed via `_branch_name(spec, n)` we would (a)
    fail to find the existing PR on lookup, (b) push to a brand-new
    slug-namespaced branch, and (c) leave the legacy branch + PR
    orphaned. The resolver therefore prefers, in order:

    1. `state["branch"]` when it names a value in the orchestrator-
       owned `orchestrator/...` namespace (the post-slug-namespacing
       persistence path; also covers in-flight PRs that recorded the
       legacy form before this code shipped).
    2. The legacy `orchestrator/issue-<n>` ref when `state["pr_number"]`
       is set but `state["branch"]` is not. Pre-slug-namespacing
       handlers were inconsistent about persisting `branch`, so a
       legacy in-flight PR can carry `pr_number` without `branch`.
       The PR's head is the legacy ref by construction (the only
       form the orchestrator ever produced before this change), so
       inferring `orchestrator/issue-<n>` keeps us anchored on the
       existing PR instead of opening a duplicate on the namespaced
       branch.
    3. The slug-namespaced `_branch_name(spec, n)` form for fresh
       issues with no PR yet.

    The pinned value is only honored when it is in the orchestrator-
    owned `orchestrator/...` namespace so a corrupted / foreign pinned
    state cannot redirect us at an arbitrary branch.
    """
    pinned = state.get("branch")
    if isinstance(pinned, str) and pinned.startswith("orchestrator/"):
        return pinned
    if state.get("pr_number") is not None:
        # Legacy in-flight PR: branch was not persisted, but a PR
        # was opened. The pre-slug-namespacing branch name was always
        # `orchestrator/issue-<n>`, so the live PR head is on that
        # ref. Targeting it keeps the orchestrator anchored on the
        # existing PR rather than orphaning it on the new namespaced
        # branch.
        return f"orchestrator/issue-{issue_number}"
    return _branch_name(spec, issue_number)


def _repo_worktrees_root(spec: config.RepoSpec) -> Path:
    """Per-repo subdirectory under WORKTREES_DIR for this spec.

    Two specs with the same issue number must not collide on disk, so the
    issue-N / decompose-N segments live inside a sanitized-slug parent
    instead of directly under WORKTREES_DIR.
    """
    return config.WORKTREES_DIR / _sanitize_slug(spec.slug)


def _worktree_path(spec: config.RepoSpec, issue_number: int) -> Path:
    return _repo_worktrees_root(spec) / f"issue-{issue_number}"


def _ensure_worktree(
    spec: config.RepoSpec, issue_number: int, *, branch: str | None = None,
) -> Path:
    """Return a worktree on a per-issue branch, reusing one with unpushed work.

    The reuse is what lets the orchestrator survive a crash between codex
    committing and the orchestrator pushing -- without it, the next tick would
    wipe the worktree and we'd burn another codex run on the same prompt.

    `branch` overrides the default `_branch_name(spec, issue_number)`
    derivation so callers can anchor on an already-pinned branch (e.g.
    a legacy `orchestrator/issue-<n>` ref kept in pinned state when
    slug-namespacing landed) instead of forcing the issue onto a new
    branch and orphaning its existing PR.

    All git operations target `spec.target_root` and therefore mutate the
    parent clone's `.git/config`. The per-target_root lock (see
    `_target_root_lock`) serializes concurrent workers so two tick fan-out
    threads cannot collide on `.git/config.lock`. The lock is released
    before the caller starts the long-running agent run.
    """
    with _target_root_lock(spec.target_root):
        _repo_worktrees_root(spec).mkdir(parents=True, exist_ok=True)
        wt = _worktree_path(spec, issue_number)
        if branch is None:
            branch = _branch_name(spec, issue_number)

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
            worktree_result = _git(
                "worktree", "add", str(wt), branch, cwd=spec.target_root,
            )
        else:
            worktree_result = _git(
                "worktree", "add", "-b", branch, str(wt),
                f"{spec.remote_name}/{spec.base_branch}",
                cwd=spec.target_root,
            )
        if worktree_result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {worktree_result.stderr}"
            )
        return wt


def _ensure_pr_worktree(
    spec: config.RepoSpec, issue_number: int, *, branch: str | None = None,
) -> Path:
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
        if branch is None:
            branch = _branch_name(spec, issue_number)

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
            worktree_result = _git(
                "worktree", "add", str(wt), branch, cwd=spec.target_root,
            )
        else:
            # Restore the local branch from the PR's remote head, NOT
            # from `<remote>/<base>` -- the dev's commits live on
            # `<remote>/<branch>` and rebuilding from base would discard
            # them.
            worktree_result = _git(
                "worktree", "add", "-b", branch, str(wt),
                f"{spec.remote_name}/{branch}",
                cwd=spec.target_root,
            )
        if worktree_result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {worktree_result.stderr}"
            )
        return wt


def _commit_count_from_stdout(count_result: subprocess.CompletedProcess) -> int:
    """Parse a `git rev-list --count` result, treating empty output as zero."""
    return int((count_result.stdout or "0").strip() or "0")


def _has_new_commits(spec: config.RepoSpec, worktree: Path) -> bool:
    commit_count_result = _git(
        "rev-list", "--count",
        f"{spec.remote_name}/{spec.base_branch}..HEAD",
        cwd=worktree,
    )
    if commit_count_result.returncode != 0:
        return False
    return _commit_count_from_stdout(commit_count_result) > 0


# The decomposer needs a working directory to run `git ls-files` / `wc -l`
# against, but it must never touch the implementer's per-issue branch. If
# it shared `_worktree_path(issue)`, the local `orchestrator/<slug>/issue-<n>`
# branch would get anchored at whatever `origin/<base>` snapshot the
# decomposer saw -- and a `split` decision parks the parent on `blocked`
# for the duration of its children's lifecycle. By the time the parent
# flips to `ready` and the implementer takes over, `origin/<base>` has
# advanced (children's PRs merged), but `_ensure_worktree` would re-add
# the worktree pointing at that stale branch and the implementer would
# commit on the old base. A separate detached-HEAD checkout sidesteps the
# problem entirely: the implementer's `_ensure_worktree` always sees a
# fresh per-issue branch created from the current `origin/<base>`.
def _decompose_worktree_path(spec: config.RepoSpec, issue_number: int) -> Path:
    return _repo_worktrees_root(spec) / f"decompose-{issue_number}"


def _ensure_decompose_worktree(spec: config.RepoSpec, issue_number: int) -> Path:
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
        worktree_result = _git(
            "worktree", "add", "--detach", str(wt),
            f"{spec.remote_name}/{spec.base_branch}",
            cwd=spec.target_root,
        )
        if worktree_result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {worktree_result.stderr}"
            )
        return wt


def _run_decompose_worktree_removal(spec: config.RepoSpec, issue_number: int) -> None:
    """Force-remove the decomposer worktree under the parent lock if present."""
    wt = _decompose_worktree_path(spec, issue_number)
    if wt.exists():
        with _target_root_lock(spec.target_root):
            _git(
                "worktree", "remove", "--force", str(wt),
                cwd=spec.target_root,
            )


def _cleanup_decompose_worktree(spec: config.RepoSpec, issue_number: int) -> None:
    """Remove the decomposer's worktree if it exists.

    Called at every `_handle_decomposing` exit except the dirty/commits
    park (where the operator may want to inspect before resuming). Every
    step -- including path resolution -- rides the best-effort guard so a
    failure is logged but never raised: cleanup must not mask the real exit.

    Serialized by the per-target_root lock because `worktree remove`
    rewrites the parent clone's `.git/config` and its `worktrees/<name>/`
    metadata directory; without it, a concurrent worker doing
    `_ensure_worktree` against the same target_root can collide on
    `.git/config.lock`.
    """
    try:
        _run_decompose_worktree_removal(spec, issue_number)
    except Exception:
        log.exception(
            "issue=#%d failed to clean up decomposer worktree", issue_number,
        )


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
    candidates = _candidate_issue_branches(spec, issue_number)
    base_ref = f"refs/remotes/{spec.remote_name}/{spec.base_branch}"
    with _target_root_lock(spec.target_root):
        for branch in candidates:
            count = _branch_commit_count(spec, branch, base_ref)
            if count > 0:
                return branch
    return None


def _candidate_issue_branches(
    spec: config.RepoSpec, issue_number: int,
) -> tuple[str, ...]:
    """Return namespaced then legacy branch candidates without duplicates."""
    namespaced = _branch_name(spec, issue_number)
    legacy = f"orchestrator/issue-{issue_number}"
    if legacy == namespaced:
        return (namespaced,)
    return namespaced, legacy


def _branch_commit_count(
    spec: config.RepoSpec, branch: str, base_ref: str,
) -> int:
    """Return commits unique to a local branch, or zero on probe failure."""
    local_ref = f"refs/heads/{branch}"
    have_local = _git(
        "rev-parse", "--verify", "--quiet", local_ref,
        cwd=spec.target_root,
    ).returncode == 0
    if not have_local:
        return 0
    commit_count_result = _git(
        "rev-list", "--count", f"{base_ref}..{local_ref}",
        cwd=spec.target_root,
    )
    if commit_count_result.returncode != 0:
        return 0
    try:
        return _commit_count_from_stdout(commit_count_result)
    except ValueError:
        return 0


def _run_issue_worktree_removal(
    spec: config.RepoSpec, issue_number: int, log_prefix: str,
) -> None:
    """Force-remove one issue worktree under the parent lock, logging a
    non-zero git result."""
    worktree = _worktree_path(spec, issue_number)
    if not worktree.exists():
        return
    with _target_root_lock(spec.target_root):
        remove_result = _git(
            "worktree", "remove", "--force", str(worktree),
            cwd=spec.target_root,
        )
    if remove_result.returncode != 0:
        log.warning(
            "issue=#%d %sworktree remove failed: %s",
            issue_number,
            log_prefix,
            (remove_result.stderr or "").strip(),
        )


def _remove_issue_worktree(
    spec: config.RepoSpec, issue_number: int, *, log_prefix: str = "",
) -> None:
    """Best-effort removal of one issue worktree under the parent lock."""
    try:
        _run_issue_worktree_removal(spec, issue_number, log_prefix)
    except Exception:
        log.exception(
            "issue=#%d %sworktree remove raised", issue_number, log_prefix,
        )


def _run_local_branch_deletion(
    spec: config.RepoSpec, issue_number: int, branch: str, log_prefix: str,
) -> None:
    """Delete one local issue branch under the parent lock (no-op when the
    branch is absent), logging a non-zero git result."""
    with _target_root_lock(spec.target_root):
        have_local = _git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
            cwd=spec.target_root,
        ).returncode == 0
        if not have_local:
            return
        delete_result = _git(
            "branch", "-D", branch, cwd=spec.target_root,
        )
    if delete_result.returncode != 0:
        log.warning(
            "issue=#%d %slocal branch %r delete failed: %s",
            issue_number,
            log_prefix,
            branch,
            (delete_result.stderr or "").strip(),
        )


def _delete_local_issue_branch(
    spec: config.RepoSpec,
    issue_number: int,
    branch: str,
    *,
    log_prefix: str = "",
) -> None:
    """Best-effort deletion of one local issue branch under the parent lock."""
    try:
        _run_local_branch_deletion(spec, issue_number, branch, log_prefix)
    except Exception:
        log.exception(
            "issue=#%d %slocal branch %r delete raised",
            issue_number,
            log_prefix,
            branch,
        )


def _cleanup_question_worktree(
    spec: config.RepoSpec, issue_number: int, *, branch: str | None = None,
) -> None:
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
    if branch is None:
        branch = _branch_name(spec, issue_number)
    _remove_issue_worktree(spec, issue_number, log_prefix="question ")
    _delete_local_issue_branch(
        spec, issue_number, branch, log_prefix="question ",
    )


def _cleanup_terminal_branch(
    gh: GitHubClient,
    spec: config.RepoSpec,
    issue_number: int,
    *,
    branch: str | None = None,
) -> None:
    """Remove the per-issue worktree and delete the local + remote branches.

    Called after the PR for `issue_number` reached a terminal state -- either
    merged externally by a human (the orchestrator is permanently manual-
    merge-only and never calls `gh.merge_pr`) or closed without merge.
    Best-effort: each step swallows its own error so a leftover
    worktree or branch never raises out of the terminal handler -- by the
    time we reach here the issue has already flipped to `done` or
    `rejected`, and a stale ref is tidiness, not correctness.

    `branch` overrides the default `_branch_name(spec, issue_number)`
    so terminal cleanup of an in-flight issue that was opened on the
    legacy `orchestrator/issue-<n>` ref reaps that branch (not the new
    namespaced one that was never pushed).

    The branch name is constrained to the orchestrator-owned
    `orchestrator/...` namespace (verified via the
    `orchestrator/`-prefix check in `_resolve_branch_name` upstream),
    so this cleanup cannot touch an arbitrary branch.

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
    if branch is None:
        branch = _branch_name(spec, issue_number)

    # Each helper contains its own exception boundary so a local failure
    # cannot skip the next cleanup surface.
    _remove_issue_worktree(spec, issue_number)
    _delete_local_issue_branch(spec, issue_number, branch)

    try:
        gh.delete_remote_branch(branch)
    except Exception:
        log.exception(
            "issue=#%d remote branch %r delete raised", issue_number, branch,
        )
