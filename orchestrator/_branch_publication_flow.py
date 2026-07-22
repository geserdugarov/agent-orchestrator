# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Branch publication flow."""
from __future__ import annotations

from orchestrator import _branch_publication_state as _state
from orchestrator import branch_publication as _owner

_SquashPreparationError = _owner._SquashPreparationError
Counter = _owner.Counter
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
Tuple = _owner.Tuple
config = _owner.config
_CONVENTIONAL_TYPES = _state._CONVENTIONAL_TYPES


def _infer_subject_prefix(
    spec: config.RepoSpec, worktree: Path, issue: Issue
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
    for subject in _owner._recent_base_subjects(spec, worktree):
        prefix = _owner._subject_prefix(subject)
        if prefix:
            counts[prefix] += 1
    if counts:
        # `most_common` breaks ties by first insertion; subjects arrive
        # newest-first, so the most recent of any tied prefixes wins.
        dominant = counts.most_common(1)[0][0]
        if dominant not in _CONVENTIONAL_TYPES:
            return dominant
    label_names = {
        (getattr(issue_label, "name", "") or "").lower()
        for issue_label in (issue.labels or [])
    }
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
    if _owner._is_prefixed_subject(subject):
        return subject
    issue_title = (issue.title or "").strip()
    if _owner._is_prefixed_subject(issue_title):
        return issue_title
    body = issue_title or f"address issue #{issue.number}"
    return f"{fallback_prefix}: {body}"


def _squash_and_force_push(
    spec: config.RepoSpec, worktree: Path, branch: str, issue: Issue,
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
    try:
        plan = _owner._prepare_squash(spec, worktree, issue)
    except _SquashPreparationError as error:
        return _owner._squash_failure(str(error))
    if len(plan.subjects) <= 1:
        return True, plan.original_head, 0, None
    return _owner._rewrite_squash(spec, worktree, branch, issue, plan)
