# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow comments."""
from __future__ import annotations

from orchestrator import _workflow_messages_state as _state
from orchestrator import workflow_messages as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
PinnedState = _owner.PinnedState
config = _owner.config
_ORCH_COMMENT_ID_CAP = _state._ORCH_COMMENT_ID_CAP
_ORCH_COMMENT_MARKER = _state._ORCH_COMMENT_MARKER
_TRACKED_REPOS_CAP = _state._TRACKED_REPOS_CAP


def _build_tracked_repos_context(
    current: config.RepoSpec, specs: list[config.RepoSpec]
) -> str:
    """Render the 'other tracked repos' awareness block, or '' when there is
    nothing useful to say.

    Returns '' when `EXPOSE_TRACKED_REPOS` is off or there is at most one
    tracked repo -- so the default single-repo deployment sees zero added
    tokens and zero behavior change. For a multi-repo deployment it lists each
    *other* repo (the `current` one is excluded from the list) on one line with
    its slug, durable `target_root` checkout, and base branch, capped at
    `_TRACKED_REPOS_CAP` with an `… and N more` overflow line.

    The framing is deliberately stage-neutral: it says only that the sibling
    checkouts are read-only references and says nothing about whether the agent
    may write in its own working directory -- that grant (or withholding) is
    owned by the surrounding stage prompt, not by this list. No secrets are
    disclosed: only operator-configured slugs, base branches, and paths the
    agent could already read; never tokens or remote URLs.
    """
    if not config.EXPOSE_TRACKED_REPOS or len(specs) <= 1:
        return ""
    others = [repo_spec for repo_spec in specs if repo_spec.slug != current.slug]
    if not others:
        return ""
    lines = [
        f"- {repo_spec.slug} — source at {repo_spec.target_root} "
        f"(base `{repo_spec.base_branch}`)"
        for repo_spec in others[:_TRACKED_REPOS_CAP]
    ]
    overflow = len(others) - _TRACKED_REPOS_CAP
    if overflow > 0:
        lines.append(f"- … and {overflow} more")
    listing = "\n".join(lines)
    return (
        "This orchestrator also tracks the repositories below. Their source is "
        "checked out locally for cross-repo reference only -- treat every path "
        "listed here as read-only and do NOT modify, commit, or push in any of "
        "them. (Whether you may write in your own working directory is governed "
        "by the rest of this prompt, not by this list.) Your task is on "
        f"`{current.slug}`.\n\n{listing}"
    )


def _orchestrator_ids(state: PinnedState) -> set[int]:
    """Set of comment ids the orchestrator itself posted on this issue/PR.
    Used to filter the orchestrator's own messages out of "new feedback"
    scans without falling back to author-login matching -- a PAT shared
    with a human reviewer's GitHub account would otherwise have its real
    review comments swallowed as bot noise (and the PR pinged ready for
    human merge over them).
    """
    raw = state.get("orchestrator_comment_ids") or []
    return {int(comment_id) for comment_id in raw}


def _track_orchestrator_comment(state: PinnedState, comment_id: int) -> None:
    raw = state.get("orchestrator_comment_ids")
    ids = list(raw) if isinstance(raw, list) else []
    ids.append(int(comment_id))
    if len(ids) > _ORCH_COMMENT_ID_CAP:
        ids = ids[-_ORCH_COMMENT_ID_CAP:]
    state.set("orchestrator_comment_ids", ids)


def _with_orch_marker(body: str) -> str:
    """Append the hidden orchestrator-comment marker to `body` (idempotent).

    Every orchestrator-posted comment carries this marker so the
    user-content hash can identify bot comments even after their id has
    been evicted from the bounded `orchestrator_comment_ids` cap. The
    marker is an HTML comment, invisible in rendered Markdown.
    """
    if _ORCH_COMMENT_MARKER in body:
        return body
    return f"{body}\n\n{_ORCH_COMMENT_MARKER}"


def _post_issue_comment(
    gh: GitHubClient, issue: Issue, state: PinnedState, body: str,
):
    """Post an issue comment AND record its id in pinned state so future
    `_handle_in_review` ticks recognize it as orchestrator-authored even when
    the PAT login is shared with a human reviewer. Caller is still responsible
    for `gh.write_pinned_state` -- this only mutates the in-memory state.

    The body is augmented with `_ORCH_COMMENT_MARKER` so the user-content
    hash can identify bot comments by marker (id-cap-resistant) in
    addition to by id (works for tracked-and-not-yet-evicted comments).
    """
    issue_comment = gh.comment(issue, _owner._with_orch_marker(body))
    cid = getattr(issue_comment, "id", None)
    if cid is not None:
        _owner._track_orchestrator_comment(state, int(cid))
    return issue_comment


def _post_pr_comment(
    gh: GitHubClient, pr_number: int, state: PinnedState, body: str,
):
    """PR-conversation comment counterpart to `_post_issue_comment`. Both
    surfaces share the IssueComment id namespace, so a single id list covers
    them. Inline review comments and PR review summaries live in different id
    spaces but the orchestrator never posts to those, so they need no entry.

    The body is augmented with `_ORCH_COMMENT_MARKER` for the same reason
    as `_post_issue_comment`: the user-content hash needs to identify
    bot comments even after their id has been evicted from the bounded
    `orchestrator_comment_ids` cap. PR-conversation comments do not feed
    into `_compute_user_content_hash` directly (the hash reads
    `issue.get_comments()`, not the PR's), but marker symmetry across
    surfaces keeps the filter rules uniform and avoids accidental
    inconsistency when a future tweak does start reading PR comments.
    """
    pr_comment = gh.pr_comment(pr_number, _owner._with_orch_marker(body))
    cid = getattr(pr_comment, "id", None)
    if cid is not None:
        _owner._track_orchestrator_comment(state, int(cid))
    return pr_comment
