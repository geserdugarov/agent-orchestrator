# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing models."""
from __future__ import annotations

from orchestrator.stages import _fixing_state as _state
from orchestrator.stages import fixing as _owner

AgentResult = _owner.AgentResult
Any = _owner.Any
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass
_AWAITING_HUMAN = _state._AWAITING_HUMAN


@dataclass(frozen=True)
class _FixingFeedback:
    issue_space: list
    review_comments: list
    review_summaries: list
    all_items: list


@dataclass(frozen=True)
class _ParkedFixingDecision:
    stop: bool
    replay_batch: Optional[list] = None


@dataclass(frozen=True)
class _FixingContext:
    """The per-tick `fixing` invocation handles, bundled so the parked-dispatch,
    validating-recovery, continue-command, resume, and reconcile helpers thread
    them as a single value instead of five positional arguments (mirrors
    validating's `_RequestedChanges`). `pr` is the live PR `_fixing_preflight`
    fetched this tick; not every consumer reads it.
    """
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    pr: Any


@dataclass(frozen=True)
class _FixingResumeRun:
    """The outcome of one locked dev resume: the worktree it ran in, the agent
    result, whether an operator paused mid-run, and the HEAD before/after.
    """
    worktree: Path
    dev_result: AgentResult
    paused: bool
    before_sha: Optional[str]
    after_sha: Optional[str]


def _park_fixing_without_pr(gh: GitHubClient, issue: Issue, state) -> None:
    """Park a `fixing` issue that carries no pinned `pr_number`.

    `fixing` is only ever entered with a recorded PR (in_review holds the PR
    before routing), so reaching here means a manual relabel from outside that
    route. Park once and surface to a human -- the dev-resume path needs the
    PR to push a fix. A no-op when the issue is already awaiting human input.
    """
    from orchestrator import workflow as _wf

    if state.get(_AWAITING_HUMAN):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `fixing` without a pinned "
        "`pr_number`; manual relabeling suspected. Set the workflow "
        "label back to `in_review` (or `validating`) after attaching "
        "a PR.",
        reason="missing_pr_number",
    )
    gh.write_pinned_state(issue, state)


def _fixing_preflight(gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state):
    """Fetch the PR and run the pre-rescan guards shared with
    `_handle_in_review`: PR-state terminals, a closed issue with no
    resolvable PR, and a `fixing` label with no pinned `pr_number`.

    Returns the fetched PR to continue the fix loop on, or ``None`` when
    the tick is fully handled -- a terminal finalized, a closed issue was
    left alone, a missing-PR park was posted, or the PR fetch failed -- and
    the caller must return immediately.
    """
    from orchestrator import workflow as _wf

    pr_number = state.get("pr_number")
    # Bind `pr` up front so the post-terminal guard below can branch on
    # it even when `pr_number` is None (in which case the fetch is
    # skipped entirely).
    pr = None

    # PR-state terminals (mirrors `_handle_in_review`). Run BEFORE any
    # rescan / debounce so a closed-fixing issue with a merged PR
    # finalizes to `done` on this tick instead of sitting closed +
    # `fixing` forever, and an external merge on an open issue also
    # short-circuits the resume cycle.
    #
    # PyGithub failures here are typically transient (network blip, rate
    # limit, 5xx). Catch and bail with `pr=None` so the caller also
    # short-circuits -- the next tick re-fetches and picks up wherever we
    # left off; the watermarks are unchanged so no feedback is lost.
    if pr_number is not None:
        try:
            pr = gh.get_pr(int(pr_number))
        except Exception:
            _wf.log.exception(
                "issue=#%s could not fetch PR #%s in fixing terminal "
                "branch; falling through", issue.number, pr_number,
            )
            pr = None
        if _wf._drain_review_pr_terminals(
            gh, spec, issue, state, pr, stage="fixing",
        ):
            return None

    # Closed issue with no PR (or a PR lookup failure): nothing to
    # finalize via the PR-state arcs above. Leave alone rather than
    # parking a closed issue.
    if getattr(issue, "state", "open") == "closed":
        _wf.log.info(
            "repo=%s issue=#%s closed fixing issue with no resolvable PR; "
            "leaving alone (relabel manually to finalize)",
            spec.slug, issue.number,
        )
        return None

    if pr_number is None:
        _owner._park_fixing_without_pr(gh, issue, state)
        return None

    # `pr_number` was set but `gh.get_pr` raised above. The exception is
    # already logged; bail this tick so the caller's rescan does not
    # dereference `None`. PyGithub failures here are typically transient
    # (network blip, rate limit, 5xx), so the next tick re-fetches and
    # picks up wherever we left off; the watermarks are unchanged so no
    # feedback is lost.
    if pr is None:
        return None

    return pr
