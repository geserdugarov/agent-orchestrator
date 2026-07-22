# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Workflow community."""
from __future__ import annotations

from orchestrator import _workflow_state as _state
from orchestrator import workflow as _owner

COMMUNITY_CONTRIBUTION_LABEL = _owner.COMMUNITY_CONTRIBUTION_LABEL
GitHubClient = _owner.GitHubClient
Optional = _owner.Optional
config = _owner.config
dataclass = _owner.dataclass
log = _state.log


@dataclass(frozen=True)
class _CommunityContribution:
    author: str


def _community_contribution_for_pr(
    gh: GitHubClient, pr, allowed_lower: set[str],
) -> Optional[_CommunityContribution]:
    user = getattr(pr, "user", None)
    if getattr(user, "type", None) == "Bot":
        return None
    author = getattr(user, "login", None) or ""
    if author.lower() in allowed_lower:
        return None
    if gh.pr_has_label(pr, COMMUNITY_CONTRIBUTION_LABEL):
        return None
    return _CommunityContribution(author)


def _label_community_contribution(
    gh: GitHubClient,
    spec: config.RepoSpec,
    pr,
    contribution: _CommunityContribution,
) -> None:
    # The label is the dedup marker, so the ping must land first. A label
    # failure may repeat a ping; a comment failure must not suppress one.
    author = contribution.author or "unknown"
    gh.pr_comment(
        pr.number,
        f"{config.HITL_MENTIONS} community contribution from "
        f"@{author} -- please review this PR.",
    )
    gh.add_pr_label(pr, COMMUNITY_CONTRIBUTION_LABEL)
    log.info(
        "repo=%s pr=#%s author=%r pinged HITL and labeled %r",
        spec.slug, pr.number, contribution.author, COMMUNITY_CONTRIBUTION_LABEL,
    )


def _sweep_pr_contribution(
    gh: GitHubClient, spec: config.RepoSpec, pr, allowed_lower: set,
) -> None:
    """Label one open PR when its author is an outside community contributor."""
    contribution = _owner._community_contribution_for_pr(gh, pr, allowed_lower)
    if contribution is not None:
        _owner._label_community_contribution(gh, spec, pr, contribution)


def _sweep_community_contribution_prs(
    gh: GitHubClient, spec: config.RepoSpec
) -> None:
    """Label open PRs from authors outside ALLOWED_ISSUE_AUTHORS and ping HITL.

    No-op when ALLOWED_ISSUE_AUTHORS is empty (the default) so a single-user
    deployment keeps the legacy "anyone is trusted" behavior. When the list
    is populated, every open PR whose author is not in it earns the
    `community_contribution` label and a one-shot HITL ping comment; the
    label is idempotent (already-labeled PRs are skipped) so the comment
    fires exactly once per PR.

    Bot-authored PRs (Dependabot, Renovate, CI bots) are skipped by
    GitHub's `user.type == "Bot"` flag -- they open PRs structurally and
    are not community contributions, so they never earn the label or ping.

    All errors are caught and logged: a PyGithub lazy-load failure on one
    PR must not abort the rest of the sweep, and the sweep itself must not
    abort the polling tick.
    """
    allowed = config.ALLOWED_ISSUE_AUTHORS
    if not allowed:
        return
    allowed_lower = {github_handle.lower() for github_handle in allowed}
    try:
        prs = list(gh.iter_open_prs())
    except Exception:
        log.exception(
            "repo=%s community-contribution sweep: open-PR enumeration failed",
            spec.slug,
        )
        return
    for pr in prs:
        try:
            _owner._sweep_pr_contribution(gh, spec, pr, allowed_lower)
        except Exception:
            log.exception(
                "repo=%s pr=#%s community-contribution sweep step failed; continuing",
                spec.slug, getattr(pr, "number", "?"),
            )
