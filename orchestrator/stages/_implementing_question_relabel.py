# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Implementing question relabel."""
from __future__ import annotations

from orchestrator.stages import _implement_state as _state
from orchestrator.stages import implementing as _owner

GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass
_AWAITING_HUMAN = _state._AWAITING_HUMAN
_LAST_ACTION_COMMENT_ID = _state._LAST_ACTION_COMMENT_ID
_PARK_REASON = _state._PARK_REASON


def _handle_stale_question_park(
    gh: GitHubClient, spec: config.RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """Clear a stale question-stage park left by a `question` -> `implementing`
    relabel, or refuse the relabel when it would ship question-agent work.

    `_handle_question` parks with `awaiting_human=True` and
    `park_reason="question_*"` so its own next tick can resume the locked
    question-agent session; those flags are opaque to implementing's resume
    path and would mis-fire it. When no such park is present this is a no-op
    returning False.

    The clear must check the actual worktree, NOT just the park reason. The
    question agent is supposed to be read-only, but a misbehaving run can park
    as `question_commits` / `question_dirty` (or `question_timeout` that
    committed before being killed) with unreviewed code state on the per-issue
    branch. Silently dropping the park would let the fresh-spawn branch's
    recovered-worktree shortcut (`_has_new_commits` -> push) publish the
    question agent's commits as if a dev session had authored them, violating
    the read-only contract.

    Returns True when the caller must return this tick: the unsafe relabel was
    re-parked as `question_unsafe_relabel` and pinned state written here. The
    branch check covers the case where the worktree was removed
    (`_cleanup_question_worktree` ran on a safe park, or the operator deleted
    the dir) but the local `orchestrator/<slug>/issue-N` branch survived with
    question-agent commits: `_ensure_worktree` would otherwise silently restore
    it and the recovered-worktree shortcut would ship those commits as a dev
    PR. The re-park is idempotent -- once `park_reason` is already
    `question_unsafe_relabel`, subsequent ticks stay silent until the state is
    cleaned or the operator relabels elsewhere.

    Returns False otherwise: either no question-stage park is present, or the
    worktree and branch are both clean so the relabel IS the unblock signal --
    the park flags are dropped and `last_action_comment_id` ratcheted past the
    question agent's answer comment (so the eventual validating->in_review
    watermark seed cannot replay it as fresh PR feedback) before the caller
    falls through to the fresh-spawn path.
    """
    park_reason = state.get(_PARK_REASON)
    if not (
        state.get(_AWAITING_HUMAN)
        and isinstance(park_reason, str)
        and park_reason.startswith("question_")
    ):
        return False
    hazard = _owner._question_relabel_hazard(spec, issue, state)
    if hazard is not None:
        if park_reason != "question_unsafe_relabel":
            _owner._park_unsafe_question_relabel(
                gh, issue, state, str(park_reason), hazard,
            )
        gh.write_pinned_state(issue, state)
        return True
    _owner._clear_stale_question_park(gh, issue, state)
    return False


@dataclass(frozen=True)
class _QuestionRelabelHazard:
    branch: str
    trigger: str


def _question_relabel_hazard(
    spec: config.RepoSpec, issue: Issue, state: PinnedState,
) -> Optional[_QuestionRelabelHazard]:
    from orchestrator import workflow as _wf

    worktree = _wf._worktree_path(spec, issue.number)
    dirty = worktree.exists() and bool(_wf._worktree_dirty_files(worktree))
    unpushed = _wf._branch_has_unpushed_commits(spec, issue.number)
    if not dirty and not unpushed:
        return None
    branch = unpushed or _wf._resolve_branch_name(state, spec, issue.number)
    return _QuestionRelabelHazard(
        branch=branch,
        trigger=_owner._question_relabel_trigger(dirty, bool(unpushed), branch),
    )


def _question_relabel_trigger(dirty: bool, unpushed: bool, branch: str) -> str:
    if dirty and not unpushed:
        return "dirty edits in the per-issue worktree"
    if unpushed and not dirty:
        return f"unreviewed commits on the per-issue branch `{branch}`"
    return (
        f"unreviewed commits on the per-issue branch `{branch}` "
        "AND dirty edits in its worktree"
    )


def _park_unsafe_question_relabel(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    park_reason: str,
    hazard: _QuestionRelabelHazard,
) -> None:
    from orchestrator import workflow as _wf

    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} relabeled to `implementing`, "
        f"but the prior question-stage park (`{park_reason}`) left "
        f"{hazard.trigger}. The question agent must be read-only, so the "
        "orchestrator refuses to push that work as a dev implementation. "
        "Reset the worktree (e.g. `git -C <worktree> reset --hard "
        "origin/<base> && git -C <worktree> clean -fd`), or delete the "
        f"local branch (`git branch -D {hazard.branch}` in `target_root`), "
        "before re-relabeling so the dev agent starts from a clean base.",
        reason="question_unsafe_relabel",
    )
    state.set(_PARK_REASON, "question_unsafe_relabel")


def _clear_stale_question_park(
    gh: GitHubClient, issue: Issue, state: PinnedState,
) -> None:
    state.set(_AWAITING_HUMAN, False)
    state.set(_PARK_REASON, None)
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        prior = state.get(_LAST_ACTION_COMMENT_ID)
        if not isinstance(prior, int) or latest > prior:
            state.set(_LAST_ACTION_COMMENT_ID, latest)
