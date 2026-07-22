# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Worktree paths."""
from __future__ import annotations

from orchestrator import _worktree_lifecycle_state as _state
from orchestrator import worktree_lifecycle as _owner

Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
hashlib = _owner.hashlib
re = _owner.re
_SAFE_CHAR = _state._SAFE_CHAR
_SLUG_DIGEST_LEN = _state._SLUG_DIGEST_LEN
_SLUG_SAFE_RE = _state._SLUG_SAFE_RE


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
    sanitized_path = _owner._sanitize_slug(slug)
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
    digest = _owner._slug_digest(slug)
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
        f"orchestrator/{_owner._sanitize_branch_segment(spec.slug)}"
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
    return _owner._branch_name(spec, issue_number)


def _repo_worktrees_root(spec: config.RepoSpec) -> Path:
    """Per-repo subdirectory under WORKTREES_DIR for this spec.

    Two specs with the same issue number must not collide on disk, so the
    issue-N / decompose-N segments live inside a sanitized-slug parent
    instead of directly under WORKTREES_DIR.
    """
    return config.WORKTREES_DIR / _owner._sanitize_slug(spec.slug)


def _worktree_path(spec: config.RepoSpec, issue_number: int) -> Path:
    return _owner._repo_worktrees_root(spec) / f"issue-{issue_number}"
