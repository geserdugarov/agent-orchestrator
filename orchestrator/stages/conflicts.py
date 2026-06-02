# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Resolving-conflict stage handler and its rebase-loop primitives.

Owns `_handle_resolving_conflict` plus the conflict-loop-private helpers:
the shared post-agent disposition funnel (`_post_conflict_resolution_result`)
and the `conflict_round` audit-event emitter
(`_emit_conflict_round_incremented`).

ALL workflow-owned helpers (`_park_awaiting_human`, `_now_iso`, the
worktree plumbing, the drift / messaging helpers re-exported into
`workflow`, the validating-side `_post_user_content_change_result`, the
implementing-side `_resume_dev_with_text` / `_on_question` /
`_on_dirty_worktree`) are reached through the parent module via
`from .. import workflow as _wf` at call time. The compatibility surface
tests rely on -- `patch.object(workflow, "_foo")` -- has to keep working
from inside the stage module too, so the handler must NOT direct-import
these names from `workflow_drift` / `workflow_messages` / `worktrees`;
doing so would bind a stable reference that test patches against
`workflow.X` could not affect.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from github.Issue import Issue

from .. import config
from ..agents import AgentResult
from ..config import RepoSpec
from ..github import (
    BASE_SYNC_HOLD_LABEL,
    GitHubClient,
    PinnedState,
    issue_has_label,
)


def _emit_conflict_round_incremented(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    pr_number: int,
    new_round: int,
    outcome: str,
    sha: Optional[str] = None,
) -> None:
    """Record a `conflict_round` audit event when the counter ticks.

    Centralizes the bookkeeping so every increment site -- ahead-of-remote
    push recovery, up-to-date no-op flip, clean base-rebase push, agent-
    resolved conflict push, drift-pushed bounce -- emits the same shape.
    `outcome` distinguishes the increment cause so a tail of the JSONL sink
    can attribute rounds without re-reading the surrounding code.
    """
    gh.emit_event(
        "conflict_round",
        issue_number=issue.number,
        stage="resolving_conflict",
        pr_number=int(pr_number),
        sha=sha or None,
        action="incremented",
        conflict_round=int(new_round),
        outcome=outcome,
        review_round=int(state.get("review_round") or 0),
        retry_count=state.get("retry_count"),
    )


def _handle_resolving_conflict(
    gh: GitHubClient, spec: RepoSpec, issue: Issue
) -> None:
    """Drive an unmergeable PR back to mergeable.

    Rebase the per-issue branch onto `origin/<base>`. On a clean rebase
    that actually moved HEAD, push and flip to `validating` so the
    reviewer re-runs against the rebased tree; if the base hasn't moved
    (branch already up-to-date) skip the push and flip straight to
    `validating` too. On real content conflicts, resume the dev session
    on the locked backend with a conflict-resolution prompt, push the
    resolved commit, and likewise flip to `validating`. Docs do not run
    here: the single docs pass runs after the reviewer's final
    `VERDICT: APPROVED` handoff to `documenting` in
    `_handle_validating`, so every pushed conflict-resolution path
    targets `validating` directly. Cap loops via `MAX_CONFLICT_ROUNDS`
    (parks awaiting human on exhaustion). On agent timeout / dirty
    tree / push failure, park awaiting human and let the operator
    unstick.

    Rebasing rewrites commit SHAs, so every pushed rebase resets
    `review_round`; validation must re-approve the rebased branch before
    any merge gate can pass.
    """
    from .. import workflow as _wf

    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if pr_number is None:
        if state.get("awaiting_human"):
            return
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `resolving_conflict` without a pinned "
            "`pr_number`; manual relabeling suspected. Set the workflow "
            "label back to `validating` after fixing.",
            reason="missing_pr_number",
        )
        gh.write_pinned_state(issue, state)
        return

    pr = gh.get_pr(int(pr_number))

    # Drain the shared PR/issue terminal arcs (merged PR -> `done`,
    # closed PR -> `rejected`, open PR + manually-closed issue ->
    # `rejected` without branch cleanup). The merged branch fires for
    # both "human merged after resolving conflicts manually" and
    # "Resolves #N auto-closed the issue when the PR merged"; the
    # open-PR + closed-issue arc only fires for issues a human closed
    # directly.
    #
    # Caveat carried over from the inline version: once the helper
    # flips a manually-closed (PR-still-open) issue to `rejected`, the
    # dispatcher's terminal-label branch is a no-op AND
    # `list_pollable_issues` only sweeps closed issues still labeled
    # `in_review` / `resolving_conflict`. A later PR close is never
    # observed by the orchestrator, so the operator must clean up the
    # worktree, local branch, and remote branch manually for the
    # "close issue first, then close PR" ordering.
    if _wf._drain_review_pr_terminals(
        gh, spec, issue, state, pr, stage="resolving_conflict",
    ):
        return

    if issue_has_label(issue, BASE_SYNC_HOLD_LABEL):
        _wf.log.info(
            "issue=#%d has %r; pausing resolving_conflict base rebase",
            issue.number, BASE_SYNC_HOLD_LABEL,
        )
        return

    # User-content drift: a human edited the issue body while the dev
    # was resolving conflicts. Resuming with the new body+comments lets
    # the dev decide whether the edit affects the conflict resolution.
    # On a successful pushed fix we hand straight to `validating` so the
    # reviewer re-runs against the updated tree; the docs pass is
    # deferred to the single post-approval hop. On an ack (no commit
    # but a reply) we stay in `resolving_conflict` without parking so a
    # harmless clarification doesn't stall the rebase.
    new_hash = _wf._detect_user_content_change(gh, issue, state)
    if new_hash is not None:
        state.set("user_content_hash", new_hash)
        _wf._post_pr_comment(
            gh, int(pr_number), state,
            ":pencil2: issue body changed; resuming dev session.",
        )
        # Mark issue-thread comments as consumed: the dev sees the full
        # thread via `_recent_comments_text`, and the eventual
        # validating->in_review handoff (after a successful pushed
        # resolution flips back to validating) must not replay them.
        _wf._mark_drift_comments_consumed(gh, issue, state)
        wt = _wf._worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _wf._ensure_pr_worktree(spec, issue.number)
        before_sha = _wf._head_sha(wt)
        followup = _wf._build_user_content_change_prompt(
            issue, _wf._recent_comments_text(issue),
        )
        wt, result = _wf._resume_dev_with_text(gh, spec, issue, state, followup)
        state.set("last_agent_action_at", _wf._now_iso())
        outcome = _wf._post_user_content_change_result(
            gh, spec, issue, state, wt, result, before_sha,
        )
        if outcome == "pushed":
            conflict_round = int(state.get("conflict_round") or 0)
            state.set("review_round", 0)
            state.set("conflict_round", conflict_round + 1)
            state.set("last_conflict_resolved_at", _wf._now_iso())
            _emit_conflict_round_incremented(
                gh, issue, state,
                pr_number=int(pr_number),
                new_round=conflict_round + 1,
                outcome="drift_resolved",
                sha=_wf._head_sha(wt) or None,
            )
            # Pushed branch diff -> hand straight back to validating;
            # the single docs pass runs after final reviewer approval.
            gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    conflict_round = int(state.get("conflict_round") or 0)

    # Resume-on-human-reply: when parked awaiting human and a new
    # comment arrived, resume the dev session on the in-progress rebase
    # worktree with the human's text. Mirrors `_handle_implementing`'s
    # awaiting-human path so a `_on_question` / `_on_dirty_worktree`
    # park can be unstuck by a comment (the park messages explicitly
    # invite that flow). Without this branch, those parks would require
    # a manual relabel even though their HITL text says "reply with
    # guidance and the orchestrator will resume the session".
    if state.get("awaiting_human"):
        last_action_id = state.get("last_action_comment_id")
        new_comments = gh.comments_after(issue, last_action_id)
        if not new_comments:
            return  # no human reply yet
        consumed_max = max(c.id for c in new_comments)
        state.set("last_action_comment_id", consumed_max)
        followup = "\n\n".join(
            f"@{c.user.login if c.user else 'user'}: {c.body}"
            for c in new_comments if c.body
        )
        wt = _wf._worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _wf._ensure_pr_worktree(spec, issue.number)
        before_sha = _wf._head_sha(wt)
        wt, result = _wf._resume_dev_with_text(gh, spec, issue, state, followup)
        state.set("last_agent_action_at", _wf._now_iso())
        # No explicit lease here: resume worktrees may be mid-rebase or
        # ahead of the remote PR head, so `before_sha` is not necessarily
        # the remote SHA. Let `_push_branch` lease against live ls-remote.
        _post_conflict_resolution_result(
            gh, spec, issue, state, wt, result, before_sha, conflict_round,
        )
        return

    if conflict_round >= config.MAX_CONFLICT_ROUNDS:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} auto-conflict-resolution still failing "
            f"after {conflict_round} round(s) "
            f"(`MAX_CONFLICT_ROUNDS={config.MAX_CONFLICT_ROUNDS}`); manual "
            "intervention needed.",
            reason="conflict_cap",
        )
        gh.write_pinned_state(issue, state)
        return

    wt = _wf._worktree_path(spec, issue.number)
    if not wt.exists():
        # PR-aware variant: restores the local branch from
        # `origin/<branch>` if it has been pruned. `_ensure_worktree`
        # would rebuild from `origin/<base>` and silently discard the
        # PR's commits.
        wt = _wf._ensure_pr_worktree(spec, issue.number)

    # Refresh `<remote>/<branch>` (the PR branch's remote tip) via the
    # same hardened authenticated path `_push_branch` uses. We need a
    # current ref before the ahead/behind check below: a stale local
    # `<remote>/<branch>` would mis-classify a real "remote moved out from
    # under us" situation as in-sync.
    branch = _wf._branch_name(issue.number)
    fetch_branch = _wf._authed_fetch(
        spec,
        f"+refs/heads/{branch}:refs/remotes/{spec.remote_name}/{branch}",
        cwd=wt,
    )
    if fetch_branch.returncode != 0:
        _wf.log.error(
            "issue=#%d branch fetch failed in resolving_conflict: %s",
            issue.number, (fetch_branch.stderr or "").strip(),
        )
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `git fetch {spec.remote_name} {branch}` "
            "failed during conflict resolution; see orchestrator logs.",
            reason="fetch_failed",
        )
        gh.write_pinned_state(issue, state)
        return

    # Check the worktree against the freshly-fetched remote PR head.
    # Three outcomes:
    #   * `(0, 0)`: in sync -- proceed to the base rebase below.
    #   * `(>0, 0)`: HEAD has unpushed commits ahead of the remote PR
    #     head. This is the crash-recovery case: a previous tick committed
    #     a conflict resolution but crashed before `_push_branch` returned
    #     (or before the post-push state write landed). Without this
    #     branch the next tick's `git rebase` would be a no-op (HEAD
    #     already contains origin/<base>) and we would flip to validating
    #     with the dev's resolution still unpushed -- letting the reviewer
    #     vote on a SHA that is not on the PR. Mirrors the implementing
    #     handler's `_has_new_commits` recovery shortcut.
    #   * Anything with `behind > 0`: stale or diverged worktree. Force-
    #     pushing the local state would clobber the real PR head, and
    #     rebasing a stale branch onto origin/<base> then force-pushing
    #     would silently revert anything that landed on `origin/<branch>`
    #     out-of-band. Refuse and park.
    ahead, behind = _wf._branch_ahead_behind(spec, wt, branch)
    if behind > 0:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} worktree on `{branch}` is {ahead} "
            f"ahead and {behind} behind `{spec.remote_name}/{branch}` "
            f"(PR head `{pr.head.sha[:8]}`); refusing to rebase a stale "
            "or diverged branch -- force-pushing the local state would "
            "clobber the real PR head. Manual intervention needed.",
            reason="diverged_branch",
        )
        gh.write_pinned_state(issue, state)
        return
    if ahead > 0:
        # Dirty check before pushing recovered work: if the previous
        # tick crashed before its own dirty check ran, the worktree
        # may carry uncommitted edits that the unpushed commit does
        # NOT contain. Pushing in that state would publish a SHA that
        # silently omits those edits, and the reviewer at validating
        # would later run on a local tree that does not match the PR.
        # Mirror `_on_dirty_worktree`: park awaiting human, no flip.
        dirty = _wf._worktree_dirty_files(wt)
        if dirty:
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} worktree has {len(dirty)} "
                "uncommitted change(s) alongside recovered conflict "
                "resolution; refusing to push an incomplete branch. "
                "Resolve the dirty tree manually before resuming.",
                reason="dirty_worktree",
            )
            gh.write_pinned_state(issue, state)
            return
        _wf.log.info(
            "issue=#%d resolving_conflict: pushing %d recovered commit(s) "
            "ahead of %s/%s before attempting base rebase",
            issue.number, ahead, spec.remote_name, branch,
        )
        if not _wf._push_branch(spec, wt, branch):
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} git push of recovered conflict "
                "resolution failed; see orchestrator logs.",
                reason="push_failed",
            )
            gh.write_pinned_state(issue, state)
            return
        state.set("review_round", 0)
        state.set("agent_approved_sha", None)
        state.set("conflict_round", conflict_round + 1)
        state.set("last_conflict_resolved_at", _wf._now_iso())
        _emit_conflict_round_incremented(
            gh, issue, state,
            pr_number=int(pr_number),
            new_round=conflict_round + 1,
            outcome="recovered_push",
            sha=_wf._head_sha(wt) or None,
        )
        # Pushed branch diff -> hand straight back to validating; the
        # single docs pass runs after final reviewer approval.
        gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    # In sync. Refresh `<remote>/<base>` so the upcoming
    # `git rebase <remote>/<base>` sees the current base tip.
    fetch_base = _wf._authed_fetch(
        spec,
        f"+refs/heads/{spec.base_branch}:"
        f"refs/remotes/{spec.remote_name}/{spec.base_branch}",
        cwd=wt,
    )
    if fetch_base.returncode != 0:
        _wf.log.error(
            "issue=#%d base fetch failed in resolving_conflict: %s",
            issue.number, (fetch_base.stderr or "").strip(),
        )
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} "
            f"`git fetch {spec.remote_name} {spec.base_branch}` "
            "failed during conflict resolution; see orchestrator logs.",
            reason="fetch_failed",
        )
        gh.write_pinned_state(issue, state)
        return

    before_sha = _wf._head_sha(wt)
    succeeded, conflicted_files = _wf._rebase_base_into_worktree(spec, wt)
    gh.emit_event(
        "merge_attempt",
        issue_number=issue.number,
        stage="resolving_conflict",
        pr_number=int(pr_number),
        sha=before_sha or None,
        method="base_rebase",
        result="success" if succeeded else (
            "conflict" if conflicted_files else "failed"
        ),
        conflict_round=conflict_round,
        review_round=int(state.get("review_round") or 0),
        retry_count=state.get("retry_count"),
    )

    if succeeded:
        # Dirty check before EITHER clean-rebase exit (no-op flip OR
        # rebased-head push): a pre-existing uncommitted edit (left by a
        # previous tick that crashed before its own dirty check ran)
        # would otherwise survive a no-op flip into validating, where
        # the reviewer agent reads the worktree directly. The reviewer
        # would then vote on a tree that does NOT match the PR head;
        # AUTO_MERGE would later refuse the SHA mismatch but the agent
        # approval is already sitting against an incorrect SHA. Park
        # rather than push or flip in that state, mirroring
        # `_on_dirty_worktree`'s "refuse to publish an incomplete
        # branch" rule.
        dirty = _wf._worktree_dirty_files(wt)
        if dirty:
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} worktree has {len(dirty)} "
                f"uncommitted change(s) after `git rebase "
                f"{spec.remote_name}/{spec.base_branch}`; refusing to "
                "push or hand back to validating with a dirty tree.",
                reason="dirty_worktree",
            )
            gh.write_pinned_state(issue, state)
            return
        after_sha = _wf._head_sha(wt)
        if not after_sha or after_sha == before_sha:
            # Already up-to-date with base. Nothing to push -- just hand
            # back to validating and let AUTO_MERGE re-evaluate.
            #
            # Increment `conflict_round` even though no diff was applied:
            # if the PR is unmergeable purely due to branch protection /
            # required reviewers (PyGithub cannot distinguish those from a
            # content conflict), the no-op rebase would otherwise loop
            # in_review <-> resolving_conflict forever with the cap never
            # firing. Counting the no-op against the cap surfaces the
            # situation to the operator within `MAX_CONFLICT_ROUNDS` ticks.
            _wf.log.info(
                "issue=#%d resolving_conflict: branch already up-to-date "
                "with %s/%s", issue.number,
                spec.remote_name, spec.base_branch,
            )
            state.set("review_round", 0)
            state.set("conflict_round", conflict_round + 1)
            _emit_conflict_round_incremented(
                gh, issue, state,
                pr_number=int(pr_number),
                new_round=conflict_round + 1,
                outcome="base_up_to_date",
                sha=after_sha,
            )
            # No branch diff changed -- hand straight back to validating
            # so AUTO_MERGE can re-evaluate. Every other resolving_conflict
            # exit also targets validating now; the single docs pass is
            # deferred to the post-approval hop.
            gh.set_workflow_label(issue, "validating")
            gh.write_pinned_state(issue, state)
            return
        if not _wf._push_branch(
            spec, wt, _wf._branch_name(issue.number),
            force_with_lease=before_sha or None,
        ):
            _wf._park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} git push failed after auto-rebasing "
                f"`{spec.remote_name}/{spec.base_branch}`; "
                "see orchestrator logs.",
                reason="push_failed",
            )
            gh.write_pinned_state(issue, state)
            return
        state.set("review_round", 0)
        state.set("agent_approved_sha", None)
        state.set("conflict_round", conflict_round + 1)
        state.set("last_conflict_resolved_at", _wf._now_iso())
        _emit_conflict_round_incremented(
            gh, issue, state,
            pr_number=int(pr_number),
            new_round=conflict_round + 1,
            outcome="base_rebased_clean",
            sha=after_sha,
        )
        # Pushed branch diff -> hand straight back to validating; the
        # single docs pass runs after final reviewer approval.
        gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    if not conflicted_files:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} "
            f"`git rebase {spec.remote_name}/{spec.base_branch}` "
            "failed without listing conflicted files; manual intervention "
            "needed.",
            reason="rebase_failed_no_files",
        )
        gh.write_pinned_state(issue, state)
        return

    fix_prompt = _wf._build_conflict_resolution_prompt(
        f"{spec.remote_name}/{spec.base_branch}", conflicted_files,
    )
    wt, result = _wf._resume_dev_with_text(gh, spec, issue, state, fix_prompt)
    state.set("last_agent_action_at", _wf._now_iso())
    _post_conflict_resolution_result(
        gh, spec, issue, state, wt, result, before_sha, conflict_round,
        force_with_lease=before_sha or None,
    )


def _post_conflict_resolution_result(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    wt: Path,
    result: AgentResult,
    before_sha: str,
    conflict_round: int,
    *,
    force_with_lease: Optional[str] = None,
) -> None:
    """Common post-agent handling for both fresh conflict resolution
    and the awaiting-human resume path in `_handle_resolving_conflict`.

    Always calls `gh.write_pinned_state` before returning so the caller
    can return immediately after invoking this helper. Increments
    `conflict_round` only on the success path -- failure paths leave
    the counter alone so a human-reply resume that lands cleanly still
    consumes a slot, but a timeout/dirty/push-failure on the same
    counter does not. A successful push hands straight back to
    `validating` so the reviewer re-runs against the resolved branch;
    the single docs pass is deferred to the post-approval handoff to
    `documenting` in `_handle_validating`.
    """
    from .. import workflow as _wf

    if result.timed_out:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} dev agent timed out resolving rebase "
            f"conflicts after {config.AGENT_TIMEOUT}s; manual intervention "
            "needed.",
            reason="agent_timeout",
        )
        gh.write_pinned_state(issue, state)
        return

    if _wf._rebase_in_progress(wt):
        raw = result.last_message.strip()
        quoted = ""
        if raw:
            quoted = "\n\nAgent output:\n\n> " + raw.replace("\n", "\n> ")
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} rebase is still in progress after the "
            "dev agent returned; finish it manually or comment with "
            f"guidance to resume.{quoted}",
            reason="rebase_in_progress",
        )
        gh.write_pinned_state(issue, state)
        return

    after_sha = _wf._head_sha(wt)
    if not after_sha or after_sha == before_sha:
        # Agent did not finish the rebase. Treat as a question /
        # silence park, mirroring the implementing handler.
        _wf._on_question(gh, issue, state, result)
        gh.write_pinned_state(issue, state)
        return

    dirty = _wf._worktree_dirty_files(wt)
    if dirty:
        _wf._on_dirty_worktree(gh, issue, state, result, dirty)
        gh.write_pinned_state(issue, state)
        return

    branch = _wf._branch_name(issue.number)
    pushed = _wf._push_branch(
        spec, wt, branch,
        force_with_lease=force_with_lease,
    )
    if not pushed:
        _wf._park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed after conflict "
            "resolution; see orchestrator logs.",
            reason="push_failed",
        )
        gh.write_pinned_state(issue, state)
        return

    state.set("review_round", 0)
    state.set("agent_approved_sha", None)
    state.set("conflict_round", conflict_round + 1)
    state.set("last_conflict_resolved_at", _wf._now_iso())
    pr_number = state.get("pr_number")
    if pr_number is not None:
        _emit_conflict_round_incremented(
            gh, issue, state,
            pr_number=int(pr_number),
            new_round=conflict_round + 1,
            outcome="agent_resolved",
            sha=after_sha,
        )
    # Pushed branch diff (fresh conflict resolution OR awaiting-human
    # resume that landed a commit) -> hand straight back to validating;
    # the single docs pass runs after final reviewer approval.
    gh.set_workflow_label(issue, "validating")
    gh.write_pinned_state(issue, state)
