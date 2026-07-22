# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating models."""
from __future__ import annotations

from orchestrator.stages import _validating_state as _state
from orchestrator.stages import validating as _owner

AgentResult = _owner.AgentResult
Any = _owner.Any
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
Path = _owner.Path
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass
_ADD_REVIEW_ROUNDS_RE = _state._ADD_REVIEW_ROUNDS_RE
_ReviewRoundsCommand = _state._ReviewRoundsCommand


@dataclass(frozen=True)
class _ReviewerRun:
    wt: Path
    round_n: int
    pr_number: Any
    agent_result: AgentResult


@dataclass(frozen=True)
class _ReviewerDecision:
    run: _ReviewerRun
    verdict: str
    body: str

    @property
    def feedback(self) -> str:
        return (
            self.body.strip()
            or (self.run.agent_result.last_message or "").strip()
        )


@dataclass(frozen=True)
class _DevFixRun:
    worktree: Path
    agent_result: AgentResult
    before_sha: str
    after_sha: Optional[str] = None


@dataclass(frozen=True)
class _RequestedChanges:
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    decision: _ReviewerDecision


def _dev_fix_run(context_args: tuple, fields: dict) -> tuple[PinnedState, _DevFixRun]:
    if len(context_args) != 4:
        raise TypeError("expected state, worktree, result, and before_sha")
    state, worktree, agent_result, before_sha = context_args
    unknown = set(fields) - {"after_sha"}
    if unknown:
        raise TypeError(f"unexpected fix-result option(s): {sorted(unknown)!r}")
    return state, _DevFixRun(
        worktree, agent_result, before_sha, fields.get("after_sha"),
    )


def _parse_add_review_rounds(
    comments: list,
) -> Optional[_ReviewRoundsCommand]:
    """Find the latest `/orchestrator add-review-rounds N` command across
    `comments`.

    Returns ``(n, None)`` for a valid positive `N`; ``(n, reason)`` when
    the latest match has an invalid argument (caller posts `reason` and
    stays parked); ``None`` when no comment carries the command. Walks
    newest-first so a corrected command supersedes a stale one posted
    earlier in the same batch.
    """
    for comment in reversed(comments):
        body = comment.body or ""
        command_match = _ADD_REVIEW_ROUNDS_RE.search(body)
        if not command_match:
            continue
        additional_rounds = int(command_match.group(1))
        if additional_rounds <= 0:
            return (
                additional_rounds,
                f"expected a positive integer (got `{additional_rounds}`)",
            )
        return (additional_rounds, None)
    return None
