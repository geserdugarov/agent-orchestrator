# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Validating watermarks."""
from __future__ import annotations

from orchestrator.stages import validating as _owner

Any = _owner.Any
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
Tuple = _owner.Tuple
dataclass = _owner.dataclass


def _watermark_comment_pairs(
    issue_comments: list, pr_comments: list,
) -> list[Tuple[Any, bool]]:
    return sorted(
        [(comment, True) for comment in issue_comments]
        + [(comment, False) for comment in pr_comments],
        key=lambda pair: pair[0].id,
    )


def _is_orchestrator_comment(comment, orchestrator_ids: set[int]) -> bool:
    from orchestrator import workflow as _wf

    return (
        comment.id in orchestrator_ids
        or _wf._ORCH_COMMENT_MARKER in (getattr(comment, "body", None) or "")
    )


@dataclass
class _WatermarkWalker:
    orchestrator_ids: set[int]
    pickup_comment_id: int
    consumed_through: Optional[int]
    watermark: Optional[int] = None
    seen_self: bool = False

    def consume(self, comment, is_issue_thread: bool) -> bool:
        is_self = _owner._is_orchestrator_comment(comment, self.orchestrator_ids)
        already_consumed = (
            is_issue_thread
            and self.consumed_through is not None
            and comment.id <= self.consumed_through
        )
        if is_self:
            self.watermark = comment.id
            self.seen_self = True
        elif not self.seen_self and comment.id < self.pickup_comment_id:
            self.watermark = comment.id
        elif already_consumed:
            self.watermark = comment.id
        else:
            return False
        return True


def _seed_watermark_past_self(
    issue_thread_comments: list,
    pr_conversation_comments: list,
    orchestrator_ids: set[int],
    pickup_comment_id: Optional[int],
    consumed_through: Optional[int] = None,
) -> Optional[int]:
    """Seed the in_review handoff watermark.

    Walk comments oldest-to-newest across both surfaces (issue thread and
    PR conversation share the IssueComment id space, so a single watermark
    covers both). The pickup comment is the boundary: everything before
    `pickup_comment_id` is pre-pickup chatter the dev agent already saw at
    spawn, so it can be advanced past. From the pickup forward, advance
    through the contiguous run of orchestrator-authored comments AND
    through any ISSUE-THREAD comment with id <= `consumed_through` (already
    fed to the dev agent via a prior `_resume_developer_on_human_reply`
    call during implementing/validating), stopping at the first
    not-yet-consumed non-orchestrator comment. This preserves human
    feedback posted during validating that the dev has not yet seen while
    NOT replaying feedback the dev has already consumed.

    `consumed_through` is intentionally NOT applied to PR-conversation
    comments. `last_action_comment_id` only records issue-thread ids fed
    via `_resume_developer_on_human_reply` (validating/implementing watch
    the issue thread only); a PR-conversation comment whose id happens to
    be <= a later-consumed issue-thread reply has NOT been seen by the dev
    and must surface on the next in_review tick. Folding both surfaces
    under one `c.id <= consumed_through` check would let the in_review
    HITL ready-ping advertise the PR as ready for human merge over
    unread PR-conversation feedback.

    Identification of orchestrator-authored content is by exact comment id
    (recorded when the orchestrator posted the comment) OR by the hidden
    body marker `_ORCH_COMMENT_MARKER` -- mirroring the in_review feedback
    filter. The id-only check would mis-treat a bot comment whose id was
    evicted from the bounded `orchestrator_comment_ids` cap (or never
    persisted due to a state-write race) as a human comment, stopping the
    walker early and stranding the watermark at a low value: the next
    in_review tick would then re-scan the same orchestrator content on
    every poll (the in_review filter still drops it via the marker, but
    the walker should not amplify that cost), and once a real human
    comment lands ABOVE the orchestrator backlog the seed walker would
    keep yielding a stale watermark indefinitely. The login-based check
    would also drop comments authored by a human reviewer who shares the
    PAT's GitHub account -- a common deployment shape -- causing real
    review feedback to be silently dropped and the PR to be pinged ready
    for human merge over it.

    Returns None when the pickup id is unknown (legacy state from a deploy
    that pre-dates pickup-id tracking, or a manually-relabeled issue) or
    when the surface has no orchestrator-authored content. The caller then
    defaults the watermark to 0 so the in_review legacy migration cannot
    advance past historical content; the orchestrator_comment_ids id-set
    filter in `_handle_in_review` drops recorded bot comments at scan time.
    """
    if pickup_comment_id is None:
        # Legacy state without a pickup anchor: refuse to advance. We
        # cannot tell pre-pickup chatter (safe to skip) from human feedback
        # posted during implementing/validating (must preserve), and
        # dropping a human comment is the unsafe direction.
        return None
    # Tag each comment with its surface so the walk below can apply
    # `consumed_through` to the issue thread only.
    comment_pairs = _owner._watermark_comment_pairs(
        issue_thread_comments, pr_conversation_comments,
    )
    if not any(
        _owner._is_orchestrator_comment(comment, orchestrator_ids)
        for comment, _ in comment_pairs
    ):
        return None
    walker = _WatermarkWalker(
        orchestrator_ids, pickup_comment_id, consumed_through,
    )
    for comment, is_issue_thread in comment_pairs:
        if not walker.consume(comment, is_issue_thread):
            break
    return walker.watermark


def _latest_pr_comment_ids(
    gh: GitHubClient, issue: Issue, pr, state: PinnedState
) -> Tuple[Optional[int], Optional[int]]:
    """Return (issue-comment watermark, review-comment watermark) seeded only
    past leading orchestrator-authored comments on the issue thread + PR.

    The second value is always None: the orchestrator never posts inline PR
    review comments, so there is no leading self-run to advance past on
    that surface, and `orchestrator_comment_ids` records IDs in the
    IssueComment namespace only -- feeding it to `_seed_watermark_past_self`
    against the PullRequestComment namespace would falsely treat a human
    inline comment whose numeric id collides with a recorded bot id as
    self-authored, advancing the watermark past the human's feedback. The
    `_handle_validating` caller defaults the inline-review watermark to 0
    when this returns None so the in_review legacy migration cannot then
    advance past human inline feedback either.
    """
    from orchestrator import workflow as _wf

    orchestrator_ids = _wf._orchestrator_ids(state)
    # `last_action_comment_id` doubles as a "consumed through" marker:
    # both park comments and post-resume bumps land here, so any issue
    # comment with id <= this value has either been posted by the
    # orchestrator (filtered by `orchestrator_comment_ids`) or already
    # been fed to the dev session (must not replay).
    # Keep the surfaces separate -- `consumed_through` only applies to the
    # issue thread (the surface `_resume_developer_on_human_reply` watches
    # during implementing/validating). Folding both into one list and
    # applying `c.id <= consumed_through` uniformly would silently advance
    # the watermark past unread PR-conversation feedback whose id happens
    # to be lower than a later-consumed issue-thread reply, letting the
    # in_review HITL ready-ping advertise the PR as ready for human
    # merge over the human's PR comment.
    issue_thread = list(gh.comments_after(issue, None))
    pr_conversation = list(gh.pr_conversation_comments_after(pr, None))
    return (
        _owner._seed_watermark_past_self(
            issue_thread, pr_conversation,
            orchestrator_ids, _owner._state_int(state, "pickup_comment_id"),
            consumed_through=_owner._state_int(state, "last_action_comment_id"),
        ),
        None,
    )


def _state_int(state: PinnedState, key: str) -> Optional[int]:
    state_value = state.get(key)
    return state_value if isinstance(state_value, int) else None
