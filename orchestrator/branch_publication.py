# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""PR branch publication helpers.

Owns the small set of commit-style / branch-state probes and the
squash-and-force-push rewrite path that together drive how the
orchestrator publishes a PR branch:

* `_CONVENTIONAL_RE` / `_is_conventional_subject` -- regex + predicate
  matching the Conventional Commits `<type>[(scope)][!]: <subject>`
  allowlist (`feat`, `fix`, ...). Used to classify whether an inferred
  history prefix is a Conventional type or a repo-local one.
* `_PREFIXED_RE` / `_is_prefixed_subject` / `_subject_prefix` -- the
  broader `<token>[(scope)][!]: <subject>` shape that also matches
  repo-local prefixes such as `event:` or `career:`. This is what
  decides whether a subject is reusable verbatim, so a repo that does
  not use Conventional Commits still keeps its own first-commit
  subjects on PR titles and squash commits.
* `_first_commit_subject` -- oldest commit subject in
  `<remote>/<base>..HEAD` for a worktree, or `''` on git error.
* `_recent_base_subjects` / `_infer_subject_prefix` -- read recent
  base-branch history and pick the fallback `<type>` prefix for an
  orchestrator-synthesized subject: a repo-local prefix when one
  dominates the history, otherwise `fix` for bug-labelled issues and
  `feat` everywhere else.
* `_pr_title_from_commit_or_issue` -- pick a PR title (also reused as
  the squash subject), preferring a reusable first commit subject and
  falling back to `<inferred-prefix>: <issue title>`.
* `_branch_ahead_behind` -- `(ahead, behind)` commit counts for HEAD
  relative to `<remote>/<branch>` in a worktree.
* `_squash_and_force_push` -- squash every commit since `<remote>/<base>`
  into one and force-push with a pinned lease.

Imports the hardened git subprocess layer (`_git`, `_git_hardened`,
`_push_branch`, `_GIT_NO_PROMPT_ENV`) from `git_plumbing.py` and the
worktree-state probes (`_head_sha`, `_worktree_dirty_files`) from
`verify.py`. The per-tick base refresh / rebase routing
(`_refresh_base_and_worktrees`, `_sync_worktree_with_base`,
`_rebase_base_into_worktree`, `_rebase_in_progress`) lives in
`base_sync.py`; the worktree naming / layout / creation / cleanup
helpers live in `worktree_lifecycle.py`. `worktrees.py` itself is now
a compatibility re-export hub for all four sibling modules.

`worktrees.py` re-exports every name below under its original name so
existing imports and `patch.object(worktrees, "_foo", ...)` test
patches that resolve the symbol against the worktrees module keep
working without touching this new module -- but the call graph lives
here, so test patches that need to INTERCEPT a call from inside
`_squash_and_force_push` / `_first_commit_subject` /
`_branch_ahead_behind` (e.g. patching `_push_branch`, `_git`, or
`_head_sha` to stub them out) must target `branch_publication`
directly. The leading underscore convention is preserved because
these helpers remain module-internal contracts -- the public surface
is the stage handlers in `orchestrator/stages/` driven by
`workflow.py`.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import List, Optional, Tuple

from github.Issue import Issue

from . import config
from .config import RepoSpec
from .git_plumbing import _GIT_NO_PROMPT_ENV, _git, _git_hardened, _push_branch
from .verify import _head_sha, _worktree_dirty_files

log = logging.getLogger(__name__)

# Conventional Commits type allowlist: the ones the implement/fix prompts
# teach plus the broader Conventional Commits set, so an agent that picks
# `perf:` or `ci:` still gets classified as conventional.
_CONVENTIONAL_TYPES = (
    "feat", "fix", "chore", "docs", "refactor",
    "test", "perf", "build", "ci", "style", "revert",
)

# Conventional Commits subject: `<type>[(scope)][!]: <subject>` restricted
# to the allowlist above.
_CONVENTIONAL_RE = re.compile(
    r"^(?:" + "|".join(_CONVENTIONAL_TYPES) + r")"
    r"(?:\([^)]+\))?!?:\s+\S",
)

# Broader "prefixed subject" shape: `<token>[(scope)][!]: <subject>` where
# `<token>` is any lowercase identifier, not just a Conventional type. This
# is what decides whether the agent's first commit subject is reusable
# verbatim, so a repo that prefixes commits with `event:` / `career:` keeps
# its own style on PR titles and squash commits instead of being forced into
# a Conventional `feat:` / `fix:`. The capturing variant pulls the bare
# token (scope / `!` stripped) for history inference. The leading-lowercase
# anchor keeps prose like `Note: ...` or a bare `TODO:` from matching.
_PREFIXED_RE = re.compile(r"^[a-z][a-z0-9-]*(?:\([^)]+\))?!?:\s+\S")
_PREFIX_TOKEN_RE = re.compile(r"^([a-z][a-z0-9-]*)(?:\([^)]+\))?!?:\s+\S")


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

    Used by `_on_commits` to derive a PR title from what the agent actually
    wrote, so the PR title matches the commit history when the subject is
    reusable. Reads the base branch from the spec so a multi-repo deployment
    with mixed default branches (e.g. one repo on `main`, another on
    `master`) compares against the right remote.
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


def _is_prefixed_subject(subject: str) -> bool:
    """True if `subject` is a reusable `<token>: <subject>` line.

    Broader than `_is_conventional_subject`: any lowercase prefix counts,
    so a repo-local `event:` / `career:` subject is reused verbatim rather
    than discarded for a synthesized `feat:`.
    """
    return bool(_PREFIXED_RE.match(subject or ""))


def _subject_prefix(subject: str) -> Optional[str]:
    """Bare prefix token of a `<token>[(scope)][!]: ...` subject, or None."""
    m = _PREFIX_TOKEN_RE.match(subject or "")
    return m.group(1) if m else None


def _recent_base_subjects(
    spec: RepoSpec, worktree: Path, limit: int = 30
) -> List[str]:
    """Subjects of the most recent non-merge base-branch commits (newest
    first), or `[]` on git error.

    Reads `<remote>/<base>` so the sample reflects the repo's own commit
    history rather than the topic branch under construction. Merge commits
    are excluded so their `Merge pull request #...` subjects don't drown
    out the real prefix style.
    """
    r = _git(
        "log", "--no-merges", f"--max-count={limit}", "--format=%s",
        f"{spec.remote_name}/{spec.base_branch}",
        cwd=worktree,
    )
    if r.returncode != 0:
        return []
    return [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]


def _infer_subject_prefix(
    spec: RepoSpec, worktree: Path, issue: Issue
) -> str:
    """Fallback `<type>` prefix for an orchestrator-synthesized subject.

    Called only when neither the agent's first commit subject nor the issue
    title already carries a reusable `<prefix>:` form. When a repo-local
    prefix (one outside the Conventional Commits allowlist, e.g. `event:` /
    `career:`) dominates recent base-branch history, reuse it so the
    synthesized subject matches the repo's own style instead of blindly
    defaulting to `feat:`. Otherwise fall back to `fix` for bug-labelled
    issues and `feat` everywhere else.
    """
    counts: Counter[str] = Counter()
    for subject in _recent_base_subjects(spec, worktree):
        prefix = _subject_prefix(subject)
        if prefix:
            counts[prefix] += 1
    if counts:
        # `most_common` breaks ties by first insertion; subjects arrive
        # newest-first, so the most recent of any tied prefixes wins.
        dominant = counts.most_common(1)[0][0]
        if dominant not in _CONVENTIONAL_TYPES:
            return dominant
    label_names = {(getattr(l, "name", "") or "").lower() for l in (issue.labels or [])}
    if {"bug", "fix"} & label_names:
        return "fix"
    return "feat"


def _pr_title_from_commit_or_issue(
    issue: Issue, first_subject: str, fallback_prefix: str = "feat",
) -> str:
    """Pick a PR title (also reused as the squash subject).

    Prefer the agent's first commit subject when it already carries a
    reusable `<prefix>:` form (so the PR title matches the commit history),
    then the issue title when it does, and only otherwise synthesize a
    `<fallback_prefix>: <issue title>` -- `fallback_prefix` comes from
    `_infer_subject_prefix`, so the synthesized form honors the repo's own
    style. Traceability is preserved by the `Resolves #<n>` line in the PR
    body, so the title stays clean.
    """
    subject = (first_subject or "").strip()
    if _is_prefixed_subject(subject):
        return subject
    issue_title = (issue.title or "").strip()
    if _is_prefixed_subject(issue_title):
        return issue_title
    body = issue_title or f"address issue #{issue.number}"
    return f"{fallback_prefix}: {body}"


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
    already carries a reusable `<prefix>:` form (Conventional or repo-local,
    so an `event:` / `career:` subject survives); otherwise it builds one
    from the issue title with `_infer_subject_prefix` -- a repo-local prefix
    when recent base history uses one, else `fix`/`feat`. The message is
    subject-only -- no body, no trailers -- so the orchestrator-authored
    squash matches the repo's subject-only commit rule. The commit is
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

    if _is_prefixed_subject(subjects[0]):
        subject = subjects[0]
    else:
        fallback_prefix = _infer_subject_prefix(spec, worktree, issue)
        subject = _pr_title_from_commit_or_issue(
            issue, subjects[0], fallback_prefix
        )

    # Subject-only message: the repo's Conventional Commits rule
    # forbids bodies and trailers on orchestrator-authored commits
    # (see CLAUDE.md). The per-step commit subjects this squash
    # replaces are still visible via `git log <branch>@{1}` until the
    # local ref is reaped, and the squashed PR carries the same
    # context in its description; aggregating them into the commit
    # body would just trip the next reviewer's commit-style check.
    message = subject + "\n"

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
