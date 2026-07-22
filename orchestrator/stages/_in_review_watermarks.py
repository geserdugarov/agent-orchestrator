# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""In review watermarks."""
from __future__ import annotations

from orchestrator.stages import _in_review_state as _state
from orchestrator.stages import in_review as _owner

Any = _owner.Any
GitHubClient = _owner.GitHubClient
Issue = _owner.Issue
Optional = _owner.Optional
PinnedState = _owner.PinnedState
config = _owner.config
dataclass = _owner.dataclass
datetime = _owner.datetime
timezone = _owner.timezone
_PR_LAST_COMMENT_ID = _state._PR_LAST_COMMENT_ID


@dataclass(frozen=True)
class _InReviewContext:
    """The per-tick `in_review` invocation handles, bundled so the fresh-feedback
    scan, fixing-route, drift, and mergeability sub-handlers thread them as a
    single value instead of five/six positional arguments (mirrors fixing's
    `_FixingContext`). `pr` is the live PR fetched this tick; `pr_number` is the
    pinned PR number `_handle_in_review` already validated as present.
    """
    gh: GitHubClient
    spec: config.RepoSpec
    issue: Issue
    state: PinnedState
    pr: Any
    pr_number: Any


@dataclass(frozen=True)
class _DriftResume:
    """Outcome of the drift dev-resume: the (possibly recreated) worktree, the
    agent result, whether an operator paused mid-run, and the pre-resume HEAD
    used to tell a pushed fix from a no-commit ack.
    """
    worktree: Any
    dev_result: Any
    paused: bool
    before_sha: Any


def _comment_created_at(comment) -> Optional[datetime]:
    """Return a tz-aware UTC datetime for a comment, or None if unavailable.

    Real PyGithub `IssueComment.created_at` is always set, but the fakes used
    in tests can leave it None when the test doesn't care about debounce.
    PullRequestReview surfaces its timestamp as `submitted_at` rather than
    `created_at`, so the in_review debounce reads either. Naive datetimes are
    interpreted as UTC (PyGithub returns naive UTC).
    """
    ca = getattr(comment, "created_at", None)
    if ca is None:
        ca = getattr(comment, "submitted_at", None)
    if ca is None:
        return None
    if ca.tzinfo is None:
        return ca.replace(tzinfo=timezone.utc)
    return ca


def _bump_in_review_watermarks(
    ctx: _InReviewContext, *, issue_space_new: Optional[list] = None,
) -> None:
    """Push the in_review issue-side watermark (`pr_last_comment_id`) past
    everything seen so far AND past any park comment just written on the issue
    thread.

    Without this, a park-and-write at in_review (unmergeable PR, failed dev fix)
    leaves `pr_last_comment_id` lagging behind the orchestrator park message it
    just posted; the next tick scans the issue thread from the older watermark
    and routes the orchestrator's own HITL ping as fresh PR feedback to
    `fixing`. The ratchet is one-way (only ever increases), so callers pass
    just-consumed comments or omit them and let `latest_comment_id` carry it.

    Only the issue-side watermark moves here. The inline-review and
    review-summary watermarks belong to the `fixing` handler, which advances
    them when it consumes that feedback; in_review never consumes review-surface
    comments itself (it routes them to `fixing`), so there is nothing to ratchet
    past on those surfaces.
    """
    candidates: list[int] = []
    cur_issue_wm = ctx.state.get(_PR_LAST_COMMENT_ID)
    if isinstance(cur_issue_wm, int):
        candidates.append(cur_issue_wm)
    last_action = ctx.state.get("last_action_comment_id")
    if isinstance(last_action, int):
        candidates.append(last_action)
    latest = ctx.gh.latest_comment_id(ctx.issue)
    if isinstance(latest, int):
        candidates.append(latest)
    if issue_space_new:
        candidates.extend(comment.id for comment in issue_space_new)
    if candidates:
        ctx.state.set(_PR_LAST_COMMENT_ID, max(candidates))


def _seed_missing_watermark(state: PinnedState, key: str, fetch) -> bool:
    """Seed a single missing review-surface watermark past the latest id
    currently visible on that surface, or 0 when it is empty. Returns whether
    a seed was written (so the caller knows a persist is needed).

    `fetch` is a thunk so the surface is only queried when `key` is unset --
    an already-seeded watermark must not trigger a redundant GitHub read.
    Persisting 0 for an empty surface (rather than leaving the key unset) is
    what stops the migration from re-firing next tick and swallowing the
    first human review added in between; see `_seed_legacy_in_review_watermarks`.
    """
    if state.get(key) is not None:
        return False
    surface_comments = list(fetch())
    state.set(
        key,
        max(comment.id for comment in surface_comments)
        if surface_comments
        else 0,
    )
    return True


def _seed_legacy_in_review_watermarks(
    gh: GitHubClient, issue: Issue, pr, state: PinnedState,
) -> None:
    """First-tick migration: seed any missing in_review watermark past every
    comment currently visible on its surface, and record the seed in pinned
    state immediately.

    Issues that reached `in_review` before the validating handoff started
    seeding watermarks (or that were manually relabeled, or whose handoff
    failed to snapshot the PR) sit on `_handle_in_review` with
    `pr_last_comment_id`/`pr_last_review_comment_id`/`pr_last_review_summary_id`
    all unset. Without this seed, the next tick would call
    `comments_after(..., None)` on each surface and treat every historical
    comment -- including the orchestrator's own pickup / PR-opened / approval
    messages -- as fresh PR feedback once the debounce expires, routing the
    issue to `fixing` over its own historical messages.

    Tests that want to drive `_handle_in_review` against pre-existing comments
    seed the relevant watermark explicitly so this helper is a no-op for them.
    """
    # Each missing watermark is persisted on this tick -- 0 if the surface
    # currently has no content, otherwise the latest visible id. Persisting
    # 0 in the empty case is what stops the migration from re-firing on the
    # next tick: if we left the watermark unset, the FIRST human inline /
    # summary review added afterward would be consumed by a re-run of this
    # seed before `_handle_in_review` builds `new_comments`, so the fresh
    # feedback route would silently swallow that first review.
    seeded = False
    if (
        state.get(_PR_LAST_COMMENT_ID) is None
        and state.get("last_action_comment_id") is None
    ):
        candidates: list[int] = []
        issue_latest = gh.latest_comment_id(issue)
        if isinstance(issue_latest, int):
            candidates.append(issue_latest)
        pr_conv = list(gh.pr_conversation_comments_after(pr, None))
        if pr_conv:
            candidates.append(max(comment.id for comment in pr_conv))
        state.set(_PR_LAST_COMMENT_ID, max(candidates) if candidates else 0)
        seeded = True

    if _owner._seed_missing_watermark(
        state, "pr_last_review_comment_id",
        lambda: gh.pr_inline_comments_after(pr, None),
    ):
        seeded = True
    if _owner._seed_missing_watermark(
        state, "pr_last_review_summary_id",
        lambda: gh.pr_reviews_after(pr, None),
    ):
        seeded = True

    if seeded:
        gh.write_pinned_state(issue, state)
