# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Fixing stage handler (stub).

Registers the `fixing` workflow label as a routable stage between
`in_review` and `validating`. `_handle_in_review` flips the label to
`fixing` the moment fresh PR feedback (issue-thread, PR-conversation,
inline-review, or review-summary) is detected, instead of silently
waiting through the debounce window or spawning the dev agent itself.
Real dev-resume + fix-loop behaviour is added under parent #137; until
then the handler parks awaiting human so a manually-applied (or routed)
`fixing` issue surfaces to the HITL handles instead of sitting silent.

The handler intentionally PARKS rather than logging-and-returning: a
silent stub would leave an operator (or the upstream `in_review` route)
waiting forever for the orchestrator to advance the issue, and a future
fix-loop handler that silently skipped would be indistinguishable from a
bug. Parking surfaces the situation immediately.

Critically, the stub also ratchets the three in_review watermarks past
the recorded `pending_fix_*_max_id` bookmarks (and the just-posted park
comment) BEFORE writing pinned state. The route in `_handle_in_review`
deliberately leaves those watermarks behind so the future real fix-loop
handler can read the triggering comments for its dev-resume prompt; the
stub does NOT have a dev-resume prompt to feed, so leaving the
watermarks unadvanced would have a manual relabel back to `in_review`
re-detect the same comments and immediately route the issue back to
`fixing` -- the documented manual recovery path would loop. Advancing
the watermarks here makes the recovery path land. The bookmarks remain
in pinned state so the real handler can still locate the original
feedback when it lands.

Closed `fixing` issues are surfaced by the closed-issue sweep so an
external manual merge with `Resolves #N` can finalize to `done`. The
stub mirrors `_handle_in_review`'s PR-state terminal branches so the
PR-merged / PR-closed-without-merge / issue-closed-with-open-PR arcs all
flip the label to `done` / `rejected`, stamp the terminal timestamp, and
run branch cleanup -- without these arcs, the issue would sit closed +
`fixing` forever and never get `done` / `merged_at` / cleanup. The real
fix-loop handler will keep the same terminal contract.

Open `fixing` issues touch only their own pinned state and worktree, so
the label is deliberately NOT listed in `workflow._FAMILY_AWARE_LABELS`
and `tick()` routes it through the fan-out bucket.
"""
from __future__ import annotations

from github.Issue import Issue

from .. import config
from ..config import RepoSpec
from ..github import GitHubClient


def _handle_fixing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    # PR-state terminals (mirrors `_handle_in_review`). Run BEFORE the
    # park branch so a closed-fixing issue with a merged PR finalizes to
    # `done` instead of sitting closed + `fixing` forever, and so an open
    # issue with an externally-merged PR also finalizes on this tick
    # rather than re-parking. Skipped when `pr_number` is unset (a
    # manually-relabeled `fixing` with no recorded PR; nothing to look
    # up).
    if pr_number is not None:
        try:
            pr = gh.get_pr(int(pr_number))
        except Exception:
            _wf.log.exception(
                "issue=#%s could not fetch PR #%s in fixing terminal "
                "branch; falling through to park", issue.number, pr_number,
            )
            pr = None
        if pr is not None:
            pr_status = gh.pr_state(pr)
            if pr_status == "merged":
                state.set("merged_at", _wf._now_iso())
                gh.set_workflow_label(issue, "done")
                gh.write_pinned_state(issue, state)
                gh.emit_event(
                    "pr_merged",
                    issue_number=issue.number,
                    stage="fixing",
                    pr_number=int(pr_number),
                    sha=getattr(pr.head, "sha", None) or None,
                    merge_method="external",
                    review_round=int(state.get("review_round") or 0),
                    conflict_round=state.get("conflict_round"),
                    retry_count=state.get("retry_count"),
                )
                try:
                    issue.edit(state="closed")
                except Exception:
                    _wf.log.exception(
                        "issue=#%s could not close after merge",
                        issue.number,
                    )
                _wf._cleanup_terminal_branch(gh, spec, issue.number)
                return
            if pr_status == "closed":
                state.set("closed_without_merge_at", _wf._now_iso())
                gh.set_workflow_label(issue, "rejected")
                gh.write_pinned_state(issue, state)
                gh.emit_event(
                    "pr_closed_without_merge",
                    issue_number=issue.number,
                    stage="fixing",
                    pr_number=int(pr_number),
                    sha=getattr(pr.head, "sha", None) or None,
                    review_round=int(state.get("review_round") or 0),
                    conflict_round=state.get("conflict_round"),
                    retry_count=state.get("retry_count"),
                )
                try:
                    issue.edit(state="closed")
                except Exception:
                    _wf.log.exception(
                        "issue=#%s could not close after reject",
                        issue.number,
                    )
                _wf._cleanup_terminal_branch(gh, spec, issue.number)
                return
            # PR is open BUT the issue was closed manually (the
            # closed-issue sweep yielded it). Mirrors the in_review
            # branch: flip to `rejected` without branch cleanup so the
            # operator can salvage the still-open PR.
            if getattr(issue, "state", "open") == "closed":
                state.set("closed_without_merge_at", _wf._now_iso())
                gh.set_workflow_label(issue, "rejected")
                gh.write_pinned_state(issue, state)
                return

    # Closed issue with no `pr_number` (or a `pr_number` lookup failure):
    # we cannot finalize via the PR-state arcs above and the stub has no
    # other work to do. Leave alone rather than parking a closed issue.
    if getattr(issue, "state", "open") == "closed":
        _wf.log.info(
            "repo=%s issue=#%s closed fixing issue with no resolvable PR; "
            "leaving alone (relabel manually to finalize)",
            spec.slug, issue.number,
        )
        return

    if state.get("awaiting_human"):
        return
    _wf._park_awaiting_human(
        gh, issue, state,
        f"{config.HITL_MENTIONS} `fixing` was applied but the fixing "
        "stage handler is not implemented yet (parent #137). The pending "
        "PR feedback was recorded; relabel the issue back to `in_review` "
        "(or another appropriate workflow stage) once the fix has been "
        "addressed, or wait for the real handler to land. Do NOT remove "
        "the workflow label entirely -- the orchestrator would then treat "
        "the issue as a fresh unlabeled pickup and could restart "
        "decomposition / implementation.",
        reason="fixing_stub",
    )
    # Ratchet the in_review watermarks past the recorded `pending_fix_*_max_id`
    # bookmarks (and the just-posted park comment) so a manual relabel back to
    # `in_review` does NOT replay the same feedback and bounce the issue back
    # into `fixing` on the next tick. The route from `_handle_in_review`
    # deliberately leaves the watermarks behind so the future real fixing
    # handler can read the triggering comments for its dev-resume prompt;
    # since the stub is parking instead of consuming them, the only safe way
    # to make the documented manual-recovery path land is to advance the
    # watermarks ourselves at park time. The `pending_fix_*_max_id` keys are
    # kept in pinned state as bookmarks (a hint for the operator and for the
    # eventual real handler), separate from the advanced watermarks.
    _bump_in_review_watermarks_for_stub_park(gh, issue, state)
    gh.write_pinned_state(issue, state)


def _bump_in_review_watermarks_for_stub_park(
    gh: GitHubClient, issue: Issue, state,
) -> None:
    """One-way ratchet over the three in_review watermarks for the stub park.

    Mirrors `stages.in_review._bump_in_review_watermarks` but reads the
    just-recorded `pending_fix_*_max_id` bookmarks rather than taking the
    consumed-comments lists as arguments. Re-implemented here instead of
    re-exporting the in_review helper to keep the fixing stub self-contained
    while it is the only call site; when the real fix-loop handler lands
    under parent #137 the helper can be shared at that point.
    """
    candidates: list[int] = []
    cur_issue_wm = state.get("pr_last_comment_id")
    if isinstance(cur_issue_wm, int):
        candidates.append(cur_issue_wm)
    last_action = state.get("last_action_comment_id")
    if isinstance(last_action, int):
        candidates.append(last_action)
    pending_issue = state.get("pending_fix_issue_max_id")
    if isinstance(pending_issue, int):
        candidates.append(pending_issue)
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        candidates.append(latest)
    if candidates:
        state.set("pr_last_comment_id", max(candidates))

    review_candidates: list[int] = []
    cur_review_wm = state.get("pr_last_review_comment_id")
    if isinstance(cur_review_wm, int):
        review_candidates.append(cur_review_wm)
    pending_review = state.get("pending_fix_review_max_id")
    if isinstance(pending_review, int):
        review_candidates.append(pending_review)
    if review_candidates:
        state.set("pr_last_review_comment_id", max(review_candidates))

    summary_candidates: list[int] = []
    cur_summary_wm = state.get("pr_last_review_summary_id")
    if isinstance(cur_summary_wm, int):
        summary_candidates.append(cur_summary_wm)
    pending_summary = state.get("pending_fix_review_summary_max_id")
    if isinstance(pending_summary, int):
        summary_candidates.append(pending_summary)
    if summary_candidates:
        state.set("pr_last_review_summary_id", max(summary_candidates))
